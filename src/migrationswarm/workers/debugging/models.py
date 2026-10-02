"""Contracts for bounded repair dispatch and durable attempt state."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from migrationswarm.core.agents import AgentResult
from migrationswarm.core.repair import RepairAttempt, RepairAttemptStatus
from migrationswarm.workers.models import WorkerStatus


class DebugQueueItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    enqueued_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("enqueued_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("enqueued_at must be timezone-aware")
        return value.astimezone(UTC)


class DebugDispatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    eligible_task_ids: list[UUID] = Field(default_factory=list)
    debug_task_ids: list[UUID] = Field(default_factory=list)
    enqueued_task_ids: list[UUID] = Field(default_factory=list)
    skipped_task_ids: list[UUID] = Field(default_factory=list)
    eligible_count: int = Field(default=0, ge=0)
    repair_pending_count: int = Field(default=0, ge=0)

    @property
    def enqueued_count(self) -> int:
        return len(self.enqueued_task_ids)


class DebugWorkerResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_id: str = Field(min_length=1)
    task_id: UUID | None = None
    status: WorkerStatus
    executed: bool = False
    success: bool | None = None
    original_task_id: UUID | None = None
    repair_attempt: RepairAttempt | None = None
    agent_result: AgentResult | None = None
    skipped_reason: str | None = None
    error_type: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "DebugDispatchResult",
    "DebugQueueItem",
    "DebugWorkerResult",
    "RepairAttempt",
    "RepairAttemptStatus",
]
