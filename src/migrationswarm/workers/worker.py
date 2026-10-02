"""One synchronous, durable-state-aware worker."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from threading import Event
from time import monotonic
from uuid import UUID, uuid4

import structlog

from migrationswarm.core.agents import AgentContext, AgentRegistry, AgentResult, WorkerRuntime
from migrationswarm.core.scheduler import TaskGraph, TaskScheduler
from migrationswarm.core.tasks import Task, TaskStateMachine, TaskStatus
from migrationswarm.persistence.db.repositories import AgentExecutionRepository, TaskRepository
from migrationswarm.persistence.redis.heartbeat import AgentHeartbeat
from migrationswarm.persistence.redis.locks import TaskLock
from migrationswarm.persistence.redis.queue import ReadyTaskQueue
from migrationswarm.workers.models import (
    TaskClaim,
    WorkerConfig,
    WorkerResult,
    WorkerSnapshot,
    WorkerStatus,
)

logger = structlog.get_logger(__name__)


class Worker:
    """Dequeue, claim, execute, and persist one task at a time."""

    def __init__(
        self,
        task_repository: TaskRepository,
        execution_repository: AgentExecutionRepository,
        queue: ReadyTaskQueue,
        lock: TaskLock,
        heartbeat: AgentHeartbeat,
        registry: AgentRegistry,
        *,
        config: WorkerConfig | None = None,
        state_machine: type[TaskStateMachine] = TaskStateMachine,
        context_factory: Callable[[Task], AgentContext] | None = None,
    ) -> None:
        self.task_repository = task_repository
        self.execution_repository = execution_repository
        self.queue = queue
        self.lock = lock
        self.heartbeat = heartbeat
        self.registry = registry
        self.config = config or WorkerConfig()
        self.state_machine = state_machine
        self.context_factory = context_factory
        self.status = WorkerStatus.IDLE
        self.current_task_id: UUID | None = None
        self.started_at = datetime.now(UTC)
        self.last_heartbeat: datetime | None = None
        self.tasks_executed = 0
        self.failures = 0
        self.error_type: str | None = None
        self._last_heartbeat_monotonic = 0.0
        self._stop_event = Event()

    @property
    def worker_id(self) -> str:
        return self.config.worker_id

    def snapshot(self) -> WorkerSnapshot:
        return WorkerSnapshot(
            worker_id=self.worker_id,
            process_id=os.getpid(),
            status=self.status,
            current_task_id=self.current_task_id,
            started_at=self.started_at,
            last_heartbeat=self.last_heartbeat,
            tasks_executed=self.tasks_executed,
            failures=self.failures,
            error_type=self.error_type,
        )

    def stop(self) -> None:
        """Request that a running worker leave its polling loop."""
        self._stop_event.set()

    def run_once(self) -> WorkerResult:
        """Process at most one queue item; an empty queue is not an error."""
        started = datetime.now(UTC)
        self._heartbeat(WorkerStatus.IDLE)
        task_id = self.queue.dequeue()
        if task_id is None:
            return self._result(started, executed=False, skipped_reason="queue_empty")

        self.status = WorkerStatus.CLAIMING
        self.current_task_id = task_id
        self._heartbeat(WorkerStatus.CLAIMING, task_id)
        owner_token = f"{self.worker_id}:{uuid4()}"
        acquired = self.lock.acquire(
            task_id, owner_token=owner_token, ttl_seconds=self.config.lock_ttl_seconds
        )
        if acquired is None:
            self.current_task_id = None
            self._heartbeat(WorkerStatus.IDLE)
            return self._result(
                started, task_id=task_id, executed=False, skipped_reason="lock_contended"
            )

        claim = TaskClaim(task_id=task_id, worker_id=self.worker_id, owner_token=acquired)
        try:
            task = self.task_repository.get(claim.task_id)
            if task is None:
                return self._result(
                    started, task_id=task_id, executed=False, skipped_reason="task_missing"
                )
            if task.status is not TaskStatus.READY:
                return self._result(
                    started,
                    task_id=task_id,
                    executed=False,
                    skipped_reason=f"database_status_{task.status.value}",
                )

            self.status = WorkerStatus.RUNNING
            self._heartbeat(WorkerStatus.RUNNING, task_id, force=True)
            graph = TaskGraph(self.task_repository.list(project_id=task.project_id))
            runtime = WorkerRuntime(TaskScheduler(graph, self.state_machine), self.registry)
            context = (
                self.context_factory(task)
                if self.context_factory is not None
                else AgentContext(project_id=task.project_id, task=task)
            )
            agent_result: AgentResult | None = None
            try:
                agent_result = runtime.execute(task, context)
            except Exception as error:
                self.failures += 1
                failure_result: AgentResult | None = None
                if task.status.value in {TaskStatus.RUNNING.value, TaskStatus.FAILED.value}:
                    failure_result = self._persist_task_and_failure(task, error)
                return self._result(
                    started,
                    task_id=task_id,
                    executed=task.status.value == TaskStatus.FAILED.value,
                    success=False,
                    agent_result=failure_result,
                    summary="Agent execution raised an exception.",
                    error_type=type(error).__name__,
                )

            self.task_repository.update(task)
            self.execution_repository.create(agent_result)
            self.tasks_executed += 1
            if not agent_result.success:
                self.failures += 1
            return self._result(
                started,
                task_id=task_id,
                executed=True,
                success=agent_result.success,
                agent_result=agent_result,
                summary=agent_result.summary,
            )
        finally:
            self.lock.release(task_id, claim.owner_token)
            self.current_task_id = None
            self.status = WorkerStatus.IDLE
            self._heartbeat(WorkerStatus.IDLE, force=True)

    def run(self, stop_event: Event | None = None) -> None:
        """Poll synchronously until stopped; exceptions are exposed as ERROR state."""
        event = stop_event or self._stop_event
        try:
            while not event.is_set():
                self.run_once()
                event.wait(self.config.poll_interval_seconds)
        except Exception as error:
            self.status = WorkerStatus.ERROR
            self.error_type = type(error).__name__
            logger.error(
                "worker_stopped_after_error",
                worker_id=self.worker_id,
                error_type=self.error_type,
            )
        finally:
            if self.status is not WorkerStatus.ERROR:
                self.status = WorkerStatus.STOPPED
            self.current_task_id = None
            self._heartbeat(self.status, force=True)

    def _persist_task_and_failure(self, task: Task, error: Exception) -> AgentResult:
        self.task_repository.update(task)
        now = datetime.now(UTC)
        failure = AgentResult(
            task_id=task.id,
            agent_name=task.assigned_agent or "unassigned",
            success=False,
            summary="Agent execution raised an exception.",
            started_at=now,
            completed_at=now,
            metadata={"error_type": type(error).__name__},
        )
        self.execution_repository.create(failure)
        return failure

    def _heartbeat(
        self,
        status: WorkerStatus,
        task_id: UUID | None = None,
        *,
        force: bool = False,
    ) -> None:
        now = monotonic()
        if (
            not force
            and now - self._last_heartbeat_monotonic
            < self.config.heartbeat_interval_seconds
        ):
            return
        self.status = status
        self.last_heartbeat = datetime.now(UTC)
        self.heartbeat.set_heartbeat(
            self.worker_id,
            task_id=task_id,
            status=status.value,
            process_id=os.getpid(),
            ttl_seconds=self.config.heartbeat_ttl_seconds,
        )
        self._last_heartbeat_monotonic = now

    def _result(
        self,
        started: datetime,
        *,
        task_id: UUID | None = None,
        executed: bool,
        success: bool | None = None,
        agent_result: AgentResult | None = None,
        summary: str = "",
        skipped_reason: str | None = None,
        error_type: str | None = None,
    ) -> WorkerResult:
        return WorkerResult(
            worker_id=self.worker_id,
            task_id=task_id,
            status=self.status,
            executed=executed,
            success=success,
            agent_result=agent_result,
            summary=summary,
            skipped_reason=skipped_reason,
            error_type=error_type,
            started_at=started,
            completed_at=datetime.now(UTC),
        )


__all__ = ["Worker"]
