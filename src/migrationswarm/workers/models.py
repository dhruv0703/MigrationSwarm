"""Small Pydantic contracts for local worker coordination."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from migrationswarm.core.agents.models import AgentResult


def _utc_now() -> datetime:
    return datetime.now(UTC)


class WorkerStatus(StrEnum):
    """Observable worker lifecycle state."""

    IDLE = "idle"
    CLAIMING = "claiming"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"


class WorkerConfig(BaseModel):
    """Bounded settings for one synchronous worker thread."""

    model_config = ConfigDict(extra="forbid")

    worker_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    heartbeat_interval_seconds: float = Field(default=5.0, gt=0)
    heartbeat_ttl_seconds: int = Field(default=30, gt=0)
    lock_ttl_seconds: int = Field(default=60, gt=0)
    poll_interval_seconds: float = Field(default=0.1, ge=0)


class TaskClaim(BaseModel):
    """A task lock ownership record kept by a worker in memory."""

    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    worker_id: str = Field(min_length=1)
    owner_token: str = Field(min_length=1)
    claimed_at: datetime = Field(default_factory=_utc_now)

    @field_validator("claimed_at")
    @classmethod
    def require_aware_claim_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("claimed_at must be timezone-aware")
        return value.astimezone(UTC)


class WorkerResult(BaseModel):
    """Bounded outcome of one dequeue/claim/execute attempt."""

    model_config = ConfigDict(extra="forbid")

    worker_id: str = Field(min_length=1)
    task_id: UUID | None = None
    status: WorkerStatus
    executed: bool = False
    success: bool | None = None
    agent_result: AgentResult | None = None
    summary: str = ""
    skipped_reason: str | None = None
    error_type: str | None = None
    started_at: datetime = Field(default_factory=_utc_now)
    completed_at: datetime = Field(default_factory=_utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("started_at", "completed_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("worker timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_time_order(self) -> "WorkerResult":
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not be before started_at")
        return self


class WorkerSnapshot(BaseModel):
    """Point-in-time worker information for the local manager and CLI."""

    model_config = ConfigDict(extra="forbid")

    worker_id: str = Field(min_length=1)
    process_id: int
    status: WorkerStatus
    current_task_id: UUID | None = None
    started_at: datetime
    last_heartbeat: datetime | None = None
    tasks_executed: int = Field(default=0, ge=0)
    failures: int = Field(default=0, ge=0)
    error_type: str | None = None


class DispatchResult(BaseModel):
    """Summary of one scheduler/queue dispatch tick."""

    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    queue_name: str = "execution"
    promoted_task_ids: list[UUID] = Field(default_factory=list)
    ready_task_ids: list[UUID] = Field(default_factory=list)
    enqueued_task_ids: list[UUID] = Field(default_factory=list)
    pending_count: int = Field(default=0, ge=0)
    ready_count: int = Field(default=0, ge=0)
    blocked_pending_count: int = Field(default=0, ge=0)

    @property
    def enqueued_count(self) -> int:
        return len(self.enqueued_task_ids)


class SwarmRunResult(BaseModel):
    """Aggregate result of one finite coordinator run."""

    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    dispatches: list[DispatchResult] = Field(default_factory=list)
    verification_dispatches: list[Any] = Field(default_factory=list)
    workers: list[WorkerSnapshot] = Field(default_factory=list)
    verification_workers: list[WorkerSnapshot] = Field(default_factory=list)
    debug_dispatches: list[Any] = Field(default_factory=list)
    debug_workers: list[WorkerSnapshot] = Field(default_factory=list)
    completed_task_ids: list[UUID] = Field(default_factory=list)
    failed_task_ids: list[UUID] = Field(default_factory=list)
    verifying_task_ids: list[UUID] = Field(default_factory=list)
    pending_task_ids: list[UUID] = Field(default_factory=list)
    ready_task_ids: list[UUID] = Field(default_factory=list)
    dry_run: bool = False
