"""Separate Redis queue for explicit DEBUG tasks."""

from uuid import UUID

from migrationswarm.persistence.redis.queue import ReadyTaskQueue

DEBUG_QUEUE_KEY = "migrationswarm:debug-tasks"


class DebugQueue(ReadyTaskQueue):
    """Duplicate-suppressing debug queue using the shared Redis primitive."""

    def __init__(self, client: object) -> None:
        super().__init__(client, key=DEBUG_QUEUE_KEY)

    def enqueue(self, task_id: UUID) -> bool:
        return super().enqueue(task_id)


__all__ = ["DEBUG_QUEUE_KEY", "DebugQueue"]
