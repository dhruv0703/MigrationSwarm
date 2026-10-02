"""Finite synchronous swarm coordination."""

from pathlib import Path
from threading import Event
from uuid import UUID

from migrationswarm.core.agents import AgentContext, AgentRegistry
from migrationswarm.core.tasks import Task, TaskStatus
from migrationswarm.core.verification import VerificationCoordinator
from migrationswarm.persistence.db.repositories import (
    AgentExecutionRepository,
    RepairAttemptRepository,
    TaskRepository,
)
from migrationswarm.persistence.redis.heartbeat import AgentHeartbeat
from migrationswarm.persistence.redis.locks import TaskLock
from migrationswarm.persistence.redis.queue import ReadyTaskQueue
from migrationswarm.persistence.service import MigrationStateService
from migrationswarm.workers.debugging import (
    DebugDispatcher,
    DebugDispatchResult,
    DebugQueue,
    DebugWorker,
    DebugWorkerManager,
)
from migrationswarm.workers.dispatcher import TaskDispatcher
from migrationswarm.workers.exceptions import WorkerExecutionError
from migrationswarm.workers.manager import WorkerManager
from migrationswarm.workers.models import (
    DispatchResult,
    SwarmRunResult,
    WorkerConfig,
)
from migrationswarm.workers.verification import (
    VerificationDispatcher,
    VerificationDispatchResult,
    VerificationEvidenceLoader,
    VerificationQueue,
    VerificationWorker,
    VerificationWorkerManager,
)
from migrationswarm.workers.worker import Worker


class SwarmCoordinator:
    """Coordinate scheduler ticks and bounded worker threads for one project."""

    def __init__(
        self,
        task_repository: TaskRepository,
        execution_repository: AgentExecutionRepository,
        queue: ReadyTaskQueue,
        lock: TaskLock,
        heartbeat: AgentHeartbeat,
        registry: AgentRegistry,
        *,
        max_workers: int = 2,
        verification_workers: int = 1,
        worker_config: WorkerConfig | None = None,
        workspace_path: Path | None = None,
        state_service: MigrationStateService | None = None,
        repair_repository: RepairAttemptRepository | None = None,
        debug_workers: int = 1,
        max_debug_attempts: int = 2,
    ) -> None:
        self.dispatcher = TaskDispatcher(task_repository, queue)
        self.execution_repository = execution_repository
        self.task_repository = task_repository
        self.queue = queue
        self.lock = lock
        self.heartbeat = heartbeat
        self.registry = registry
        self.max_workers = max_workers
        if not 1 <= verification_workers <= 2:
            raise ValueError("verification_workers must be between 1 and 2")
        self.verification_workers = verification_workers
        self.worker_config = worker_config
        self.workspace_path = workspace_path
        self.state_service = state_service
        self.repair_repository = repair_repository
        if not 1 <= debug_workers <= 2:
            raise ValueError("debug_workers must be between 1 and 2")
        self.debug_workers = debug_workers
        self.verification_queue = VerificationQueue(queue.client)
        self.verification_dispatcher = VerificationDispatcher(
            task_repository, self.verification_queue
        )
        self.verification_lock = TaskLock(
            queue.client, prefix="migrationswarm:verification-lock"
        )
        self.verification_root = workspace_path or Path.cwd()
        self.debug_queue = DebugQueue(queue.client)
        self.debug_dispatcher = (
            DebugDispatcher(
                task_repository,
                repair_repository,
                self.debug_queue,
                repository_root=self.verification_root,
                execution_repository=execution_repository,
                max_attempts=max_debug_attempts,
            )
            if repair_repository is not None
            else None
        )
        self.debug_lock = TaskLock(queue.client, prefix="migrationswarm:debug-lock")

    def dry_run(self, project_id: UUID) -> SwarmRunResult:
        """Return durable counts without promoting, enqueueing, or executing."""
        inspection = self.dispatcher.inspect(project_id)
        verification = self.verification_dispatcher.inspect(project_id)
        debug = self.debug_dispatcher.inspect(project_id) if self.debug_dispatcher else None
        tasks = self.task_repository.list(project_id=project_id)
        return SwarmRunResult(
            project_id=project_id,
            dispatches=[inspection],
            verification_dispatches=[verification],
            debug_dispatches=[debug] if debug else [],
            pending_task_ids=[task.id for task in tasks if task.status is TaskStatus.PENDING],
            ready_task_ids=[task.id for task in tasks if task.status is TaskStatus.READY],
            verifying_task_ids=[task.id for task in tasks if task.status is TaskStatus.VERIFYING],
            completed_task_ids=[task.id for task in tasks if task.status is TaskStatus.COMPLETED],
            failed_task_ids=[task.id for task in tasks if task.status is TaskStatus.FAILED],
            dry_run=True,
        )

    def run(
        self,
        project_id: UUID,
        *,
        poll_interval_seconds: float = 0.05,
        max_idle_ticks: int = 4,
    ) -> SwarmRunResult:
        """Run until no queued/running work remains or the finite idle bound is hit."""
        if poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must not be negative")
        if max_idle_ticks < 1:
            raise ValueError("max_idle_ticks must be positive")

        manager = WorkerManager(
            self._worker_factory,
            max_workers=self.max_workers,
        )
        dispatches: list[DispatchResult] = [
            self.dispatcher.dispatch(project_id, include_existing_ready=True)
        ]
        verification_dispatches = [
            self.verification_dispatcher.dispatch(project_id, include_existing=True)
        ]
        debug_dispatches = [
            self.debug_dispatcher.dispatch(project_id)
        ] if self.debug_dispatcher else []
        verification_manager = VerificationWorkerManager(
            self._verification_worker_factory,
            max_workers=self.verification_workers,
        )
        debug_manager = (
            DebugWorkerManager(self._debug_worker_factory, max_workers=self.debug_workers)
            if self.debug_dispatcher
            else None
        )
        manager.start()
        verification_manager.start()
        if debug_manager:
            debug_manager.start()
        idle_ticks = 0
        try:
            while idle_ticks < max_idle_ticks:
                dispatch = self.dispatcher.dispatch(project_id, include_existing_ready=False)
                dispatches.append(dispatch)
                verification_dispatch = self.verification_dispatcher.dispatch(
                    project_id, include_existing=False
                )
                verification_dispatches.append(verification_dispatch)
                debug_dispatch = (
                    self.debug_dispatcher.dispatch(project_id)
                    if self.debug_dispatcher
                    else None
                )
                if debug_dispatch is not None:
                    debug_dispatches.append(debug_dispatch)
                if (
                    dispatch.enqueued_count
                    or manager.active_count
                    or self.queue.size()
                    or verification_dispatch.enqueued_count
                    or verification_manager.active_count
                    or self.verification_queue.size()
                    or (debug_manager is not None and debug_manager.active_count)
                    or self.debug_queue.size()
                ):
                    idle_ticks = 0
                else:
                    idle_ticks += 1
                Event().wait(poll_interval_seconds)
        finally:
            manager.stop()
            manager.join(timeout=max(1.0, poll_interval_seconds * 20))
            verification_manager.stop()
            verification_manager.join(timeout=max(1.0, poll_interval_seconds * 20))
            if debug_manager:
                debug_manager.stop()
                debug_manager.join(timeout=max(1.0, poll_interval_seconds * 20))

        errors = manager.errors + verification_manager.errors
        if debug_manager:
            errors += debug_manager.errors
        if errors:
            raise WorkerExecutionError("Worker persistence error: " + ", ".join(errors))
        return self._aggregate(
            project_id,
            dispatches,
            verification_dispatches,
            debug_dispatches,
            manager,
            verification_manager,
            debug_manager,
        )

    def _worker_factory(self, config: WorkerConfig) -> Worker:
        if self.worker_config is not None:
            config = self.worker_config.model_copy(update={"worker_id": config.worker_id})

        def context_factory(task: Task) -> AgentContext:
            project_id = task.project_id
            return AgentContext(
                project_id=project_id,
                task=task,
                workspace_path=str(self.workspace_path) if self.workspace_path else None,
            )

        return Worker(
            self.task_repository,
            self.execution_repository,
            self.queue,
            self.lock,
            self.heartbeat,
            self.registry,
            config=config,
            context_factory=context_factory,
        )

    def _verification_worker_factory(self, config: WorkerConfig) -> VerificationWorker:
        if self.worker_config is not None:
            config = self.worker_config.model_copy(
                update={"worker_id": f"verification-{config.worker_id}"}
            )
        return VerificationWorker(
            self.task_repository,
            self.execution_repository,
            self.verification_queue,
            self.verification_lock,
            self.heartbeat,
            self._verification_coordinator(),
            VerificationEvidenceLoader(self.verification_root, self.execution_repository),
            config=config,
            state_service=self.state_service,
        )

    def _verification_coordinator(self) -> VerificationCoordinator:
        return VerificationCoordinator(self.verification_root)

    def _debug_worker_factory(self, config: WorkerConfig) -> DebugWorker:
        if self.worker_config is not None:
            config = self.worker_config.model_copy(
                update={"worker_id": f"debug-{config.worker_id}"}
            )
        assert self.repair_repository is not None
        return DebugWorker(
            self.task_repository,
            self.execution_repository,
            self.repair_repository,
            self.debug_queue,
            self.verification_queue,
            self.debug_lock,
            self.heartbeat,
            self.registry,
            config=config,
            workspace_path=str(self.workspace_path) if self.workspace_path else None,
            state_service=self.state_service,
        )

    def _aggregate(
        self,
        project_id: UUID,
        dispatches: list[DispatchResult],
        verification_dispatches: list[VerificationDispatchResult],
        debug_dispatches: list[DebugDispatchResult],
        manager: WorkerManager,
        verification_manager: VerificationWorkerManager,
        debug_manager: DebugWorkerManager | None,
    ) -> SwarmRunResult:
        tasks = self.task_repository.list(project_id=project_id)
        return SwarmRunResult(
            project_id=project_id,
            dispatches=dispatches,
            verification_dispatches=verification_dispatches,
            workers=list(manager.snapshots()),
            verification_workers=list(verification_manager.snapshots()),
            debug_dispatches=debug_dispatches,
            debug_workers=list(debug_manager.snapshots()) if debug_manager else [],
            completed_task_ids=[task.id for task in tasks if task.status is TaskStatus.COMPLETED],
            failed_task_ids=[task.id for task in tasks if task.status is TaskStatus.FAILED],
            verifying_task_ids=[task.id for task in tasks if task.status is TaskStatus.VERIFYING],
            pending_task_ids=[task.id for task in tasks if task.status is TaskStatus.PENDING],
            ready_task_ids=[task.id for task in tasks if task.status is TaskStatus.READY],
        )


__all__ = ["SwarmCoordinator"]
