"""Pydantic models describing isolated Git worktrees."""

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC)


class WorktreeStatus(StrEnum):
    """Observable status of a managed worktree."""

    ACTIVE = "active"
    DIRTY = "dirty"
    CLEAN = "clean"
    REMOVED = "removed"


class GitWorktree(BaseModel):
    """Metadata for one task-owned Git worktree."""

    model_config = ConfigDict(validate_assignment=True)

    task_id: UUID
    branch_name: str = Field(min_length=1)
    path: Path
    base_commit: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=_utc_now)
    status: WorktreeStatus = WorktreeStatus.ACTIVE

    @field_validator("created_at")
    @classmethod
    def ensure_utc_timestamp(cls, value: datetime) -> datetime:
        """Require timezone-aware timestamps and normalize them to UTC."""
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value.astimezone(UTC)
