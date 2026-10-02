"""Controlled Git repository and isolated worktree support."""

from migrationswarm.core.git.exceptions import (
    DirtyWorktreeError,
    DuplicateWorktreeError,
    GitOperationError,
    GitRepositoryError,
    GitUnavailableError,
    NotGitRepositoryError,
    UnknownWorktreeError,
    UnsafeWorktreePathError,
)
from migrationswarm.core.git.manager import GitWorktreeManager
from migrationswarm.core.git.models import GitWorktree, WorktreeStatus
from migrationswarm.core.git.repository import GitRepository

__all__ = [
    "DirtyWorktreeError",
    "DuplicateWorktreeError",
    "GitOperationError",
    "GitRepository",
    "GitRepositoryError",
    "GitUnavailableError",
    "GitWorktree",
    "GitWorktreeManager",
    "NotGitRepositoryError",
    "UnknownWorktreeError",
    "UnsafeWorktreePathError",
    "WorktreeStatus",
]
