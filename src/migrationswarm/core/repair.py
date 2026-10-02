"""Provider-independent durable repair-attempt contract."""

from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RepairAttemptStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    EXHAUSTED = "exhausted"
    HUMAN_REVIEW = "human_review"


class RepairAttempt(BaseModel):
    """Secret-free durable summary of one explicit DEBUG task."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    id: UUID = Field(default_factory=uuid4)
    original_task_id: UUID
    debug_task_id: UUID
    attempt_number: int = Field(ge=1, le=3)
    failure_category: str = Field(min_length=1)
    status: RepairAttemptStatus = RepairAttemptStatus.PENDING
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    verification_artifact_before: str | None = None
    verification_artifact_after: str | None = None
    debug_artifact: str | None = None
    model_provider: str | None = None
    model_name: str | None = None

    @field_validator("started_at", "completed_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("repair timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_time_order(self) -> "RepairAttempt":
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("completed_at must not be before started_at")
        return self


__all__ = ["RepairAttempt", "RepairAttemptStatus"]
