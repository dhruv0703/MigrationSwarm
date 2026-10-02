"""Small Redis client wrapper used only for transient coordination."""

from __future__ import annotations

from typing import Any

import redis

from migrationswarm.config import Settings
from migrationswarm.persistence.exceptions import RedisCoordinationError


class RedisClient:
    """Create and health-check a synchronous Redis client."""

    def __init__(self, url: str, *, client: Any | None = None) -> None:
        self.url = url
        self.client = client or redis.Redis.from_url(url, decode_responses=True)

    @classmethod
    def from_settings(cls, settings: Settings) -> RedisClient:
        return cls(settings.redis_url)

    def ping(self) -> bool:
        try:
            return bool(self.client.ping())
        except redis.RedisError as error:
            raise RedisCoordinationError("Redis connectivity check failed") from error

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if close is not None:
            close()


__all__ = ["RedisClient"]
