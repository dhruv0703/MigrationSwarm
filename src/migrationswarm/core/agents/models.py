"""Pydantic models exchanged by agents and the worker runtime."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from migrationswarm.core.security import redact_secrets
from migrationswarm.core.tasks.models import Task


def _utc_now() -> datetime:
    """Return the current timezone-aware UTC timestamp."""
    return datetime.now(UTC)


class AgentResult(BaseModel):
    """The deterministic result returned by an agent execution."""

    model_config = ConfigDict(validate_assignment=True)

    task_id: UUID
    agent_name: str
    success: bool
    summary: str
    artifacts: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=_utc_now)
    completed_at: datetime = Field(default_factory=_utc_now)

    @field_validator("artifacts", "metadata", mode="before")
    @classmethod
    def redact_result_values(cls, value: Any) -> Any:
        """Keep credentials out of result objects before persistence or telemetry."""
        return redact_secrets(value)

    @field_validator("started_at", "completed_at")
    @classmethod
    def ensure_utc_timestamp(cls, value: datetime) -> datetime:
        """Require timezone-aware timestamps and normalize them to UTC."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_completion_order(self) -> "AgentResult":
        """Ensure execution completion cannot precede execution start."""
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not be before started_at")
        return self


class AgentContext(BaseModel):
    """Execution context supplied to an agent by the runtime.

    For code-changing tasks, ``workspace_path`` must point to an isolated
    ``.migrationswarm/worktrees/<task-id>`` directory, never the repository root.
    Read-only agents may continue to use the original repository path.
    """

    project_id: UUID
    task: Task
    workspace_path: str | None = Field(
        default=None,
        description=(
            "For code-changing tasks, an isolated task worktree path; "
            "read-only agents may use the repository root."
        ),
    )
    metadata: dict[str, Any] = Field(default_factory=dict)
