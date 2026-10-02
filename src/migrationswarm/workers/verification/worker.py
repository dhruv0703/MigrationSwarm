"""Bounded deterministic verification worker."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from threading import Event
from time import monotonic
from uuid import UUID, uuid4

import structlog

from migrationswarm.core.agents import AgentResult
from migrationswarm.core.tasks import TaskStatus
from migrationswarm.core.verification import (
    VerificationCoordinator,
    VerificationDecision,
)
from migrationswarm.persistence.db.repositories import AgentExecutionRepository, TaskRepository
from migrationswarm.persistence.redis.heartbeat import AgentHeartbeat
from migrationswarm.persistence.redis.locks import TaskLock
from migrationswarm.persistence.service import MigrationStateService
from migrationswarm.workers.models import WorkerConfig, WorkerSnapshot, WorkerStatus
from migrationswarm.workers.verification.evidence import VerificationEvidenceLoader
from migrationswarm.workers.verification.models import VerificationWorkerResult
from migrationswarm.workers.verification.queue import VerificationQueue

logger = structlog.get_logger(__name__)


class VerificationWorker:
    """Decide VERIFYING tasks and persist lifecycle/decision evidence."""

    def __init__(
        self,
        task_repository: TaskRepository,
        execution_repository: AgentExecutionRepository,
        queue: VerificationQueue,
        lock: TaskLock,
        heartbeat: AgentHeartbeat,
        coordinator: VerificationCoordinator,
        evidence_loader: VerificationEvidenceLoader,
        *,
        config: WorkerConfig | None = None,
        state_service: MigrationStateService | None = None,
    ) -> None:
        self.task_repository = task_repository
        self.execution_repository = execution_repository
        self.queue = queue
        self.lock = lock
        self.heartbeat = heartbeat
        self.coordinator = coordinator
        self.evidence_loader = evidence_loader
        self.state_service = state_service
        self.config = config or WorkerConfig()
        self.status = WorkerStatus.IDLE
        self.current_task_id: UUID | None = None
        self.started_at = datetime.now(UTC)
        self.last_heartbeat: datetime | None = None
        self.tasks_verified = 0
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
            tasks_executed=self.tasks_verified,
            failures=self.failures,
            error_type=self.error_type,
        )

    def stop(self) -> None:
        self._stop_event.set()

    def run_once(self) -> VerificationWorkerResult:
        started = datetime.now(UTC)
        self._heartbeat(WorkerStatus.IDLE)
        task_id = self.queue.dequeue()
        if task_id is None:
            return self._result(task_id=None, status=WorkerStatus.IDLE)
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
                task_id=task_id,
                status=WorkerStatus.IDLE,
                skipped_reason="lock_contended",
            )

        try:
            task = self.task_repository.get(task_id)
            if task is None:
                return self._result(
                    task_id=task_id,
                    status=WorkerStatus.IDLE,
                    skipped_reason="task_missing",
                )
            if task.status is not TaskStatus.VERIFYING:
                return self._result(
                    task_id=task_id,
                    status=WorkerStatus.IDLE,
                    skipped_reason=f"database_status_{task.status.value}",
                )
            self.status = WorkerStatus.RUNNING
            self._heartbeat(WorkerStatus.RUNNING, task_id, force=True)
            loaded = self.evidence_loader.load(task_id)
            if loaded.evidence is None:
                decision = self.coordinator.record_insufficient_evidence(
                    task, [loaded.reason or "Verification evidence is insufficient."]
                )
            else:
                decision = self.coordinator.decide(task, loaded.evidence)
            if self.state_service is not None:
                self.state_service.persist_task(task)
            else:
                self.task_repository.update(task)
            agent_result = AgentResult(
                task_id=task.id,
                agent_name="verification-worker",
                success=decision.decision is VerificationDecision.PASSED,
                summary=f"Verification decision: {decision.decision.value}.",
                artifacts=[decision.artifact_path] if decision.artifact_path else [],
                metadata={
                    "decision": decision.decision.value,
                    "reasons": decision.reasons,
                    "warnings": decision.warnings,
                },
                started_at=started,
                completed_at=datetime.now(UTC),
            )
            if self.state_service is not None:
                self.state_service.persist_agent_result(agent_result)
            else:
                self.execution_repository.create(agent_result)
            self.tasks_verified += 1
            if decision.decision is VerificationDecision.FAILED:
                self.failures += 1
            return self._result(
                task_id=task_id,
                status=WorkerStatus.RUNNING,
                decision=decision.decision,
                decision_artifact=decision.artifact_path,
                agent_result=agent_result,
                executed=True,
            )
        except Exception as error:
            self.failures += 1
            logger.error(
                "verification_worker_task_error",
                worker_id=self.worker_id,
                error_type=type(error).__name__,
            )
            return self._result(
                task_id=task_id,
                status=WorkerStatus.ERROR,
                error_type=type(error).__name__,
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
            logger.error(
                "verification_worker_stopped_after_error",
                worker_id=self.worker_id,
                error_type=self.error_type,
            )
        finally:
            if self.status is not WorkerStatus.ERROR:
                self.status = WorkerStatus.STOPPED
            self.current_task_id = None
            self._heartbeat(self.status, force=True)

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
        task_id: UUID | None,
        status: WorkerStatus,
        decision: VerificationDecision | None = None,
        decision_artifact: str | None = None,
        agent_result: AgentResult | None = None,
        executed: bool = False,
        skipped_reason: str | None = None,
        error_type: str | None = None,
    ) -> VerificationWorkerResult:
        return VerificationWorkerResult(
            worker_id=self.worker_id,
            task_id=task_id,
            status=status,
            decision=decision,
            decision_artifact=decision_artifact,
            agent_result=agent_result,
            executed=executed,
            skipped_reason=skipped_reason,
            error_type=error_type,
        )


__all__ = ["VerificationWorker"]
