"""Workspace contracts passed to future code-changing agents."""

from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TaskWorkspace(BaseModel):
    """An isolated task worktree, never the original repository root."""

    model_config = ConfigDict(extra="forbid")

    task_id: UUID
    repository_root: Path
    workspace_path: Path
    branch_name: str = Field(min_length=1)
    base_commit: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_isolated_path(self) -> "TaskWorkspace":
        """Require the workspace to be a managed child of the repository."""
        root = self.repository_root.expanduser().resolve()
        workspace = self.workspace_path.expanduser().resolve()
        managed = (root / ".migrationswarm" / "worktrees").resolve()
        if workspace == root:
            raise ValueError("TaskWorkspace cannot use the repository root")
        try:
            workspace.relative_to(managed)
        except ValueError as error:
            raise ValueError(
                "TaskWorkspace must be inside .migrationswarm/worktrees"
            ) from error
        return self
