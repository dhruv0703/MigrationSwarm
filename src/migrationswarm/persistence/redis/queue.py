"""Redis-backed ready-task queue with best-effort duplicate suppression."""

from __future__ import annotations

from uuid import UUID

import redis

from migrationswarm.persistence.exceptions import RedisCoordinationError


class ReadyTaskQueue:
    """Transient queue for task IDs; PostgreSQL remains the source of truth."""

    def __init__(self, client: object, *, key: str = "migrationswarm:ready-tasks") -> None:
        self.client = client
        self.key = key
        self.members_key = f"{key}:members"

    def enqueue(self, task_id: UUID) -> bool:
        """Enqueue once and return whether a new entry was added."""
        try:
            added = int(self.client.sadd(self.members_key, str(task_id)))  # type: ignore[attr-defined]
            if added:
                self.client.rpush(self.key, str(task_id))  # type: ignore[attr-defined]
                return True
            return False
        except redis.RedisError as error:
            raise RedisCoordinationError("Could not enqueue ready task") from error

    def dequeue(self) -> UUID | None:
        """Remove and return the next task ID, if one is available."""
        try:
            value = self.client.lpop(self.key)  # type: ignore[attr-defined]
            if value is None:
                return None
            self.client.srem(self.members_key, value)  # type: ignore[attr-defined]
            return UUID(str(value))
        except (redis.RedisError, ValueError) as error:
            raise RedisCoordinationError("Could not dequeue ready task") from error

    def size(self) -> int:
        try:
            return int(self.client.llen(self.key))  # type: ignore[attr-defined]
        except redis.RedisError as error:
            raise RedisCoordinationError("Could not inspect ready queue") from error


__all__ = ["ReadyTaskQueue"]
