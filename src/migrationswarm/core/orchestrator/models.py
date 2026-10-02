"""Models for one controlled single-service migration run."""

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from migrationswarm.agents.build_verification import BuildVerificationResult
from migrationswarm.core.tasks import Task, TaskStatus
from migrationswarm.core.verification import (
    VerificationDecisionResult,
)


def _utc_now() -> datetime:
    """Return the current timezone-aware UTC timestamp."""
    return datetime.now(UTC)


class MigrationRunStatus(StrEnum):
    """Lifecycle of the overall single-service workflow."""

    PENDING = "pending"
    PREPARING_WORKSPACE = "preparing_workspace"
    EXTRACTING = "extracting"
    VERIFYING_BUILD = "verifying_build"
    DECIDING = "deciding"
    COMPLETED = "completed"
    FAILED = "failed"
    HUMAN_REVIEW = "human_review"


class MigrationRunEventType(StrEnum):
    """Small, auditable events emitted by the orchestrator."""

    WORKTREE_CREATED = "worktree_created"
    EXTRACTION_STARTED = "extraction_started"
    EXTRACTION_COMPLETED = "extraction_completed"
    BUILD_STARTED = "build_started"
    BUILD_PASSED = "build_passed"
    BUILD_FAILED = "build_failed"
    DEBUG_STARTED = "debug_started"
    DEBUG_APPLIED = "debug_applied"
    DEBUG_FAILED = "debug_failed"
    REVERIFY_STARTED = "reverify_started"
    REVERIFY_PASSED = "reverify_passed"
    REVERIFY_FAILED = "reverify_failed"
    DEBUG_ATTEMPTS_EXHAUSTED = "debug_attempts_exhausted"
    VERIFICATION_PASSED = "verification_passed"
    VERIFICATION_FAILED = "verification_failed"
    HUMAN_REVIEW_REQUIRED = "human_review_required"


class MigrationRunEvent(BaseModel):
    """One bounded orchestration event; source and full logs are excluded."""

    model_config = ConfigDict(extra="forbid")

    event_type: MigrationRunEventType
    occurred_at: datetime = Field(default_factory=_utc_now)
    stage: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("occurred_at")
    @classmethod
    def ensure_utc_timestamp(cls, value: datetime) -> datetime:
        """Require timezone-aware UTC event timestamps."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value.astimezone(UTC)


class MigrationRun(BaseModel):
    """Persistable state for one selected-service migration attempt."""

    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    run_id: UUID = Field(default_factory=uuid4)
    repository_root: Path
    selected_service: str = Field(min_length=1)
    task_id: UUID
    project_id: UUID | None = None
    worktree_path: Path | None = None
    started_at: datetime = Field(default_factory=_utc_now)
    completed_at: datetime | None = None
    status: MigrationRunStatus = MigrationRunStatus.PENDING
    current_stage: str = Field(default="pending", min_length=1)
    events: list[MigrationRunEvent] = Field(default_factory=list)
    generated_files: list[str] = Field(default_factory=list)
    verification_status: str | None = None
    final_task_status: TaskStatus | None = None
    warnings: list[str] = Field(default_factory=list)
    failure_reason: str | None = None
    debug_attempts: int = Field(default=0, ge=0, le=3)
    max_debug_attempts: int = Field(default=2, ge=0, le=3)

    @model_validator(mode="after")
    def validate_debug_attempts(self) -> "MigrationRun":
        """Keep the bounded repair counter internally consistent."""
        if self.debug_attempts > self.max_debug_attempts:
            raise ValueError("debug_attempts cannot exceed max_debug_attempts")
        return self

    @field_validator("started_at", "completed_at")
    @classmethod
    def ensure_utc_timestamp(cls, value: datetime | None) -> datetime | None:
        """Require aware timestamps and normalize them to UTC."""
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)

    def record(
        self,
        event_type: MigrationRunEventType,
        stage: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Append one bounded event in execution order."""
        self.events.append(
            MigrationRunEvent(
                event_type=event_type,
                stage=stage,
                payload=payload or {},
            )
        )


class MigrationRunResult(BaseModel):
    """Structured result returned by the orchestrator."""

    model_config = ConfigDict(extra="forbid")

    run: MigrationRun
    task: Task | None = None
    verification_result: BuildVerificationResult | None = None
    verification_decision: VerificationDecisionResult | None = None
    dry_run: bool = False

    @property
    def success(self) -> bool:
        """Return whether the overall run completed successfully."""
        return self.run.status is MigrationRunStatus.COMPLETED


class MigrationRunError(ValueError):
    """Base exception for orchestration input and execution failures."""


__all__ = [
    "MigrationRun",
    "MigrationRunError",
    "MigrationRunEvent",
    "MigrationRunEventType",
    "MigrationRunResult",
    "MigrationRunStatus",
]
