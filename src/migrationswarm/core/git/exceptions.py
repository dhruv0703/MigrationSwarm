"""Exceptions raised by the controlled Git integration."""


class GitRepositoryError(RuntimeError):
    """Base exception for repository and worktree operations."""


class GitUnavailableError(GitRepositoryError):
    """Raised when the Git executable is not available."""


class GitOperationError(GitRepositoryError):
    """Raised when a controlled Git command fails."""


class NotGitRepositoryError(GitRepositoryError):
    """Raised when a path is not inside a Git repository."""


class DuplicateWorktreeError(GitRepositoryError):
    """Raised when a task already has a managed worktree."""


class UnknownWorktreeError(GitRepositoryError):
    """Raised when a task has no managed worktree."""


class DirtyWorktreeError(GitRepositoryError):
    """Raised when a dirty worktree is removed without explicit force."""


class UnsafeWorktreePathError(GitRepositoryError):
    """Raised when an operation targets a path outside managed worktrees."""
