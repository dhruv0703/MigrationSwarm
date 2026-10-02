"""Synchronous worker for one explicit bounded repair task."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from threading import Event
from time import monotonic
from uuid import UUID, uuid4

import structlog

from migrationswarm.agents.build_verification import BuildVerificationAgent
from migrationswarm.core.agents import AgentContext, AgentRegistry, AgentResult, WorkerRuntime
from migrationswarm.core.scheduler import TaskGraph, TaskScheduler
from migrationswarm.core.tasks import Task, TaskStateMachine, TaskStatus, TaskType
from migrationswarm.persistence.db.repositories import (
    AgentExecutionRepository,
    RepairAttemptRepository,
    TaskRepository,
)
from migrationswarm.persistence.redis.heartbeat import AgentHeartbeat
from migrationswarm.persistence.redis.locks import TaskLock
from migrationswarm.persistence.service import MigrationStateService
from migrationswarm.workers.debugging.models import (
    DebugWorkerResult,
    RepairAttempt,
    RepairAttemptStatus,
)
from migrationswarm.workers.debugging.queue import DebugQueue
from migrationswarm.workers.models import WorkerConfig, WorkerSnapshot, WorkerStatus
from migrationswarm.workers.verification.queue import VerificationQueue

logger = structlog.get_logger(__name__)


class DebugWorker:
    """Claim, execute, and reverify one explicit DEBUG task."""

    def __init__(
        self,
        task_repository: TaskRepository,
        execution_repository: AgentExecutionRepository,
        repair_repository: RepairAttemptRepository,
        queue: DebugQueue,
        verification_queue: VerificationQueue,
        lock: TaskLock,
        heartbeat: AgentHeartbeat,
        registry: AgentRegistry,
        *,
        config: WorkerConfig | None = None,
        workspace_path: str | None = None,
        verification_agent: object | None = None,
        state_service: MigrationStateService | None = None,
        state_machine: type[TaskStateMachine] = TaskStateMachine,
    ) -> None:
        self.task_repository = task_repository
        self.execution_repository = execution_repository
        self.repair_repository = repair_repository
        self.queue = queue
        self.verification_queue = verification_queue
        self.lock = lock
        self.heartbeat = heartbeat
        self.registry = registry
        self.config = config or WorkerConfig()
        self.workspace_path = workspace_path
        self.verification_agent = verification_agent or BuildVerificationAgent()
        self.state_service = state_service
        self.state_machine = state_machine
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
        self._stop_event.set()

    def run_once(self) -> DebugWorkerResult:
        task_id = self.queue.dequeue()
        if task_id is None:
            return self._result(status=WorkerStatus.IDLE)
        self.current_task_id = task_id
        self.status = WorkerStatus.CLAIMING
        self._heartbeat(WorkerStatus.CLAIMING, task_id, force=True)
        owner = self.lock.acquire(
            task_id,
            owner_token=f"{self.worker_id}:{uuid4()}",
            ttl_seconds=self.config.lock_ttl_seconds,
        )
        if owner is None:
            self.current_task_id = None
            self._heartbeat(WorkerStatus.IDLE, force=True)
            return self._result(
                task_id=task_id, status=WorkerStatus.IDLE, skipped_reason="lock_contended"
            )
        try:
            task = self.task_repository.get(task_id)
            if task is None:
                return self._result(
                    task_id=task_id, status=WorkerStatus.IDLE, skipped_reason="task_missing"
                )
            if task.status is not TaskStatus.READY or task.task_type is not TaskType.DEBUG:
                return self._result(
                    task_id=task_id,
                    status=WorkerStatus.IDLE,
                    skipped_reason=f"not_ready_debug_task_{task.status.value}",
                )
            original_id = self._original_id(task)
            original = self.task_repository.get(original_id)
            attempt = self._attempt(task)
            if original is None or attempt is None:
                return self._result(
                    task_id=task_id,
                    status=WorkerStatus.IDLE,
                    skipped_reason="repair_link_missing",
                    original_task_id=original_id,
                )
            self._set_attempt(attempt, RepairAttemptStatus.RUNNING)
            self.status = WorkerStatus.RUNNING
            self._heartbeat(WorkerStatus.RUNNING, task_id, force=True)
            try:
                agent_result = self._execute_debug(task)
            except Exception as error:
                self._mark_debug_failure(task, attempt, error)
                self.failures += 1
                return self._result(
                    task_id=task_id,
                    status=WorkerStatus.ERROR,
                    executed=True,
                    success=False,
                    original_task_id=original_id,
                    repair_attempt=attempt,
                    error_type=type(error).__name__,
                )
            self._persist_task(task)
            self._persist_execution(agent_result)
            self.tasks_executed += 1
            attempt.debug_artifact = self._artifact(agent_result)
            provider = agent_result.metadata.get("model_provider")
            model = agent_result.metadata.get("model_name")
            attempt.model_provider = provider if isinstance(provider, str) else None
            attempt.model_name = model if isinstance(model, str) else None
            if not agent_result.success:
                self._set_attempt(attempt, RepairAttemptStatus.FAILED)
                self.failures += 1
                return self._result(
                    task_id=task_id,
                    status=WorkerStatus.RUNNING,
                    executed=True,
                    success=False,
                    original_task_id=original_id,
                    repair_attempt=attempt,
                    agent_result=agent_result,
                )
            self._complete_debug_task(task)
            try:
                reverify_result = self._reverify(original, task)
            except Exception as error:
                self._set_attempt(attempt, RepairAttemptStatus.FAILED)
                self.failures += 1
                return self._result(
                    task_id=task_id,
                    status=WorkerStatus.ERROR,
                    executed=True,
                    success=False,
                    original_task_id=original_id,
                    repair_attempt=attempt,
                    agent_result=agent_result,
                    error_type=type(error).__name__,
                )
            self._persist_execution(reverify_result)
            if reverify_result.success:
                attempt.verification_artifact_after = self._artifact(reverify_result)
                self._set_attempt(attempt, RepairAttemptStatus.PASSED)
                self.verification_queue.enqueue(original.id)
            else:
                self._set_attempt(attempt, RepairAttemptStatus.FAILED)
                self.failures += 1
            return self._result(
                task_id=task_id,
                status=WorkerStatus.RUNNING,
                executed=True,
                success=reverify_result.success,
                original_task_id=original_id,
                repair_attempt=attempt,
                agent_result=agent_result,
                metadata={"reverification_success": reverify_result.success},
            )
        finally:
            self.lock.release(task_id, owner)
            self.current_task_id = None
            self.status = WorkerStatus.IDLE
            self._heartbeat(WorkerStatus.IDLE, force=True)

    def run(self, stop_event: Event | None = None) -> None:
        event = stop_event or self._stop_event
        try:
            while not event.is_set():
                self.run_once()
                event.wait(self.config.poll_interval_seconds)
        except Exception as error:
            self.status = WorkerStatus.ERROR
            self.error_type = type(error).__name__
            logger.error("debug_worker_stopped_after_error", error_type=self.error_type)
        finally:
            if self.status is not WorkerStatus.ERROR:
                self.status = WorkerStatus.STOPPED
            self.current_task_id = None
            self._heartbeat(self.status, force=True)

    def _execute_debug(self, task: Task) -> AgentResult:
        graph = TaskGraph(self.task_repository.list(project_id=task.project_id))
        registry = self.registry
        runtime = WorkerRuntime(
            TaskScheduler(graph, self.state_machine), registry, self.state_machine
        )
        metadata = dict(task.metadata)
        return runtime.execute(
            task,
            AgentContext(
                project_id=task.project_id,
                task=task,
                workspace_path=self._workspace(task),
                metadata=metadata,
            ),
        )

    def _reverify(self, original: Task, debug_task: Task) -> AgentResult:
        self._transition(original, TaskStatus.READY)
        self._transition(original, TaskStatus.RUNNING)
        reverify = original.model_copy(
            deep=True,
            update={
                "task_type": TaskType.VERIFY,
                "assigned_agent": "build_verification",
                "status": TaskStatus.READY,
                "dependencies": [],
            },
        )
        verifier_registry = AgentRegistry()
        verifier_registry.register(self.verification_agent)  # type: ignore[arg-type]
        runtime = WorkerRuntime(
            TaskScheduler(TaskGraph([reverify]), self.state_machine),
            verifier_registry,
            self.state_machine,
        )
        context = AgentContext(
            project_id=original.project_id,
            task=reverify,
            workspace_path=self._workspace(debug_task),
            metadata={
                "worktree_task_id": str(original.id),
                "verification_root": debug_task.metadata.get("target_service_directory", "."),
            },
        )
        try:
            result = runtime.execute(reverify, context)
        except Exception:
            self._transition(original, TaskStatus.FAILED)
            raise
        self._transition(original, TaskStatus.VERIFYING if result.success else TaskStatus.FAILED)
        return result

    def _complete_debug_task(self, task: Task) -> None:
        self._transition(task, TaskStatus.COMPLETED)

    def _mark_debug_failure(self, task: Task, attempt: RepairAttempt, error: Exception) -> None:
        if task.status is TaskStatus.RUNNING:
            self._transition(task, TaskStatus.FAILED)
        self._persist_task(task)
        now = datetime.now(UTC)
        failure = AgentResult(
            task_id=task.id,
            agent_name=task.assigned_agent or "debug",
            success=False,
            summary="Debug agent execution raised an exception.",
            metadata={"error_type": type(error).__name__},
            started_at=now,
            completed_at=now,
        )
        self._persist_execution(failure)
        self._set_attempt(attempt, RepairAttemptStatus.FAILED)

    def _transition(self, task: Task, target: TaskStatus) -> None:
        if self.state_service is not None:
            self.state_service.transition_task(task, target)
        else:
            self.state_machine.transition(task, target)
            self.task_repository.update(task)

    def _persist_task(self, task: Task) -> None:
        if self.state_service is not None:
            self.state_service.persist_task(task)
        else:
            self.task_repository.update(task)

    def _persist_execution(self, result: AgentResult) -> None:
        if self.state_service is not None:
            self.state_service.persist_agent_result(result)
        else:
            self.execution_repository.create(result)

    def _set_attempt(self, attempt: RepairAttempt, status: RepairAttemptStatus) -> None:
        attempt.status = status
        if status is not RepairAttemptStatus.PENDING:
            attempt.started_at = min(attempt.started_at, datetime.now(UTC))
        if status in {
            RepairAttemptStatus.PASSED,
            RepairAttemptStatus.FAILED,
            RepairAttemptStatus.EXHAUSTED,
            RepairAttemptStatus.HUMAN_REVIEW,
        }:
            attempt.completed_at = datetime.now(UTC)
        if self.state_service is not None:
            self.state_service.persist_repair_attempt(attempt)
        else:
            existing = self.repair_repository.get(attempt.id)
            if existing is None:
                self.repair_repository.create(attempt)
            else:
                self.repair_repository.update(attempt)

    @staticmethod
    def _original_id(task: Task) -> UUID:
        return UUID(str(task.metadata["original_task_id"]))

    def _attempt(self, task: Task) -> RepairAttempt | None:
        value = task.metadata.get("repair_attempt_id")
        if not isinstance(value, str):
            return None
        return self.repair_repository.get(UUID(value))

    @staticmethod
    def _artifact(result: AgentResult) -> str | None:
        return result.artifacts[0] if result.artifacts else None

    def _workspace(self, task: Task) -> str | None:
        value = task.metadata.get("worktree_path") or self.workspace_path
        return str(value) if value else None

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
        *,
        status: WorkerStatus,
        task_id: UUID | None = None,
        executed: bool = False,
        success: bool | None = None,
        original_task_id: UUID | None = None,
        repair_attempt: RepairAttempt | None = None,
        agent_result: AgentResult | None = None,
        skipped_reason: str | None = None,
        error_type: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> DebugWorkerResult:
        return DebugWorkerResult(
            worker_id=self.worker_id,
            task_id=task_id,
            status=status,
            executed=executed,
            success=success,
            original_task_id=original_task_id,
            repair_attempt=repair_attempt,
            agent_result=agent_result,
            skipped_reason=skipped_reason,
            error_type=error_type,
            metadata=metadata or {},
        )


__all__ = ["DebugWorker"]
