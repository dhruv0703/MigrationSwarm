"""A Redis namespace dedicated to verification work."""

from uuid import UUID

from migrationswarm.persistence.redis.queue import ReadyTaskQueue
from migrationswarm.workers.verification.models import VerificationQueueItem

VERIFICATION_QUEUE_KEY = "migrationswarm:verification-tasks"


class VerificationQueue:
    """Use the existing duplicate-suppressing queue under a separate key."""

    def __init__(self, client: object, *, key: str = VERIFICATION_QUEUE_KEY) -> None:
        self._queue = ReadyTaskQueue(client, key=key)
        self.key = key

    @property
    def client(self) -> object:
        return self._queue.client

    def enqueue(self, task_id: UUID) -> bool:
        return self._queue.enqueue(task_id)

    def dequeue(self) -> UUID | None:
        return self._queue.dequeue()

    def size(self) -> int:
        return self._queue.size()

    def item(self, task_id: UUID) -> VerificationQueueItem:
        return VerificationQueueItem(task_id=task_id)


__all__ = ["VERIFICATION_QUEUE_KEY", "VerificationQueue"]
