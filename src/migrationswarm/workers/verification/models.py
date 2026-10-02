"""Contracts for verification queue items and worker results."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from migrationswarm.core.agents.models import AgentResult
from migrationswarm.core.verification import VerificationDecision, VerificationEvidence
from migrationswarm.workers.models import WorkerStatus


class VerificationQueueItem(BaseModel):
    """A transient verification queue item."""

    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    enqueued_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("enqueued_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("enqueued_at must be timezone-aware")
        return value.astimezone(UTC)


class EvidenceLoadResult(BaseModel):
    """Evidence lookup outcome, including safe missing/corrupt states."""

    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    evidence: VerificationEvidence | None = None
    source_path: str | None = None
    reason: str | None = None

    @property
    def available(self) -> bool:
        return self.evidence is not None


class VerificationDispatchResult(BaseModel):
    """Summary of one verification scheduling tick."""

    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    verifying_task_ids: list[UUID] = Field(default_factory=list)
    enqueued_task_ids: list[UUID] = Field(default_factory=list)
    eligible_count: int = Field(default=0, ge=0)
    verifying_count: int = Field(default=0, ge=0)

    @property
    def enqueued_count(self) -> int:
        return len(self.enqueued_task_ids)


class VerificationWorkerResult(BaseModel):
    """Outcome of one verification dequeue/decision attempt."""

    model_config = ConfigDict(extra="forbid")

    worker_id: str = Field(min_length=1)
    task_id: UUID | None = None
    status: WorkerStatus
    decision: VerificationDecision | None = None
    decision_artifact: str | None = None
    agent_result: AgentResult | None = None
    executed: bool = False
    skipped_reason: str | None = None
    error_type: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
