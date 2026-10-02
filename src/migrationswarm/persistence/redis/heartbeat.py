"""Expiring agent heartbeat records."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import redis
from pydantic import BaseModel, ConfigDict, Field

from migrationswarm.persistence.exceptions import RedisCoordinationError


class Heartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_name: str = Field(min_length=1)
    worker_id: str | None = None
    process_id: int | None = None
    status: str = "idle"
    project_id: UUID | None = None
    task_id: UUID | None = None
    recorded_at: datetime


class AgentHeartbeat:
    """Store only short-lived liveness state in Redis."""

    def __init__(self, client: object, *, prefix: str = "migrationswarm:heartbeat") -> None:
        self.client = client
        self.prefix = prefix

    def _key(self, agent_name: str) -> str:
        return f"{self.prefix}:{agent_name}"

    def set_heartbeat(
        self,
        agent_name: str,
        *,
        task_id: UUID | None = None,
        project_id: UUID | None = None,
        status: str = "idle",
        process_id: int | None = None,
        ttl_seconds: int = 30,
    ) -> Heartbeat:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        heartbeat = Heartbeat(
            agent_name=agent_name,
            worker_id=agent_name,
            process_id=process_id,
            status=status,
            project_id=project_id,
            task_id=task_id,
            recorded_at=datetime.now(UTC),
        )
        try:
            self.client.set(  # type: ignore[attr-defined]
                self._key(agent_name), heartbeat.model_dump_json(), ex=ttl_seconds
            )
            return heartbeat
        except redis.RedisError as error:
            raise RedisCoordinationError("Could not set agent heartbeat") from error

    def get_heartbeat(self, agent_name: str) -> Heartbeat | None:
        try:
            value = self.client.get(self._key(agent_name))  # type: ignore[attr-defined]
            return Heartbeat.model_validate_json(value) if value else None
        except (redis.RedisError, ValueError, TypeError) as error:
            raise RedisCoordinationError("Could not retrieve agent heartbeat") from error

    def ttl(self, agent_name: str) -> int:
        try:
            return int(self.client.ttl(self._key(agent_name)))  # type: ignore[attr-defined]
        except redis.RedisError as error:
            raise RedisCoordinationError("Could not inspect heartbeat TTL") from error

    def list_heartbeats(self) -> tuple[Heartbeat, ...]:
        """Return currently live heartbeat records in deterministic order."""
        try:
            keys = self.client.scan_iter(match=f"{self.prefix}:*")  # type: ignore[attr-defined]
            values: list[Heartbeat] = []
            for key in sorted(str(value) for value in keys):
                raw = self.client.get(key)  # type: ignore[attr-defined]
                if raw:
                    values.append(Heartbeat.model_validate_json(raw))
            return tuple(values)
        except (redis.RedisError, ValueError, TypeError) as error:
            raise RedisCoordinationError("Could not list worker heartbeats") from error

    set = set_heartbeat
    get = get_heartbeat


__all__ = ["AgentHeartbeat", "Heartbeat"]
