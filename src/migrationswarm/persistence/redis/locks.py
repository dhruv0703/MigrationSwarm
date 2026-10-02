"""Owner-checked transient task locks."""

from __future__ import annotations

from uuid import UUID, uuid4

import redis

from migrationswarm.persistence.exceptions import RedisCoordinationError


class TaskLock:
    """Acquire task-scoped locks with TTL and owner-token verification."""

    def __init__(self, client: object, *, prefix: str = "migrationswarm:task-lock") -> None:
        self.client = client
        self.prefix = prefix

    def _key(self, task_id: UUID) -> str:
        return f"{self.prefix}:{task_id}"

    def acquire(
        self, task_id: UUID, *, owner_token: str | None = None, ttl_seconds: int = 30
    ) -> str | None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        token = owner_token or str(uuid4())
        try:
            acquired = self.client.set(  # type: ignore[attr-defined]
                self._key(task_id), token, nx=True, ex=ttl_seconds
            )
            return token if acquired else None
        except redis.RedisError as error:
            raise RedisCoordinationError("Could not acquire task lock") from error

    def release(self, task_id: UUID, owner_token: str) -> bool:
        """Release only when the stored token belongs to the caller."""
        try:
            key = self._key(task_id)
            evaluator = getattr(self.client, "eval", None)
            if evaluator is not None:
                script = (
                    "if redis.call('get', KEYS[1]) == ARGV[1] "
                    "then return redis.call('del', KEYS[1]) else return 0 end"
                )
                try:
                    return bool(evaluator(script, 1, key, owner_token))
                except redis.ResponseError:
                    # Some lightweight fakes and restricted Redis deployments do
                    # not expose scripting; retain owner verification as a fallback.
                    pass
            if self.client.get(key) != owner_token:  # type: ignore[attr-defined]
                return False
            return bool(self.client.delete(key))  # type: ignore[attr-defined]
        except redis.RedisError as error:
            raise RedisCoordinationError("Could not release task lock") from error

    def ttl(self, task_id: UUID) -> int:
        try:
            return int(self.client.ttl(self._key(task_id)))  # type: ignore[attr-defined]
        except redis.RedisError as error:
            raise RedisCoordinationError("Could not inspect task lock TTL") from error


__all__ = ["TaskLock"]
