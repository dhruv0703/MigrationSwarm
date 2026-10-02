"""Dispatch persisted VERIFYING tasks to the separate verification queue."""

from uuid import UUID

from migrationswarm.core.tasks import TaskStatus
from migrationswarm.persistence.db.repositories import TaskRepository
from migrationswarm.workers.verification.models import VerificationDispatchResult
from migrationswarm.workers.verification.queue import VerificationQueue


class VerificationDispatcher:
    """Observe durable VERIFYING state without mutating it."""

    def __init__(self, task_repository: TaskRepository, queue: VerificationQueue) -> None:
        self.task_repository = task_repository
        self.queue = queue
        self._seen_verifying: set[UUID] = set()

    def inspect(self, project_id: UUID) -> VerificationDispatchResult:
        tasks = self.task_repository.list(project_id=project_id, status=TaskStatus.VERIFYING)
        return VerificationDispatchResult(
            project_id=project_id,
            verifying_task_ids=[task.id for task in tasks],
            eligible_count=len(tasks),
            verifying_count=len(tasks),
        )

    def dispatch(
        self,
        project_id: UUID,
        *,
        include_existing: bool = True,
    ) -> VerificationDispatchResult:
        tasks = self.task_repository.list(project_id=project_id, status=TaskStatus.VERIFYING)
        candidates = [
            task for task in tasks if include_existing or task.id not in self._seen_verifying
        ]
        enqueued: list[UUID] = []
        for task in sorted(candidates, key=lambda item: str(item.id)):
            self._seen_verifying.add(task.id)
            if self.queue.enqueue(task.id):
                enqueued.append(task.id)
        return VerificationDispatchResult(
            project_id=project_id,
            verifying_task_ids=[task.id for task in tasks],
            enqueued_task_ids=enqueued,
            eligible_count=len(candidates),
            verifying_count=len(tasks),
        )


__all__ = ["VerificationDispatcher"]
