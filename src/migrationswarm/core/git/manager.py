"""Safe lifecycle management for task-owned Git worktrees."""

import re
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from migrationswarm.core.git.exceptions import (
    DirtyWorktreeError,
    DuplicateWorktreeError,
    GitOperationError,
    UnknownWorktreeError,
    UnsafeWorktreePathError,
)
from migrationswarm.core.git.models import GitWorktree, WorktreeStatus
from migrationswarm.core.git.repository import GitRepository
from migrationswarm.core.tasks import Task
from migrationswarm.core.workspaces import TaskWorkspace

_SAFE_BRANCH_CHARACTER = re.compile(r"[^A-Za-z0-9._/-]+")


class GitWorktreeManager:
    """Create, inspect, and remove only worktrees under the managed directory."""

    def __init__(self, repository: GitRepository | str | Path) -> None:
        self.repository = (
            repository
            if isinstance(repository, GitRepository)
            else GitRepository.from_path(repository)
        )
        raw_managed_root = self.repository.root / ".migrationswarm" / "worktrees"
        if any(
            part.is_symlink()
            for part in (self.repository.root / ".migrationswarm", raw_managed_root)
        ):
            raise UnsafeWorktreePathError(
                "Managed worktree directory cannot contain symlink components"
            )
        self.managed_root = raw_managed_root.resolve()

    def create_worktree(self, task: Task | UUID) -> GitWorktree:
        """Create one isolated branch and worktree from the current HEAD."""
        task_id = task.id if isinstance(task, Task) else task
        path = self._task_path(task_id)
        branch_name = self.branch_name(task_id)
        if path.exists() or any(item.task_id == task_id for item in self.list_worktrees()):
            raise DuplicateWorktreeError(f"Worktree already exists for task: {task_id}")
        if self._branch_exists(branch_name):
            raise DuplicateWorktreeError(f"Managed branch already exists: {branch_name}")

        base_commit = self.repository.head_commit()
        self.managed_root.mkdir(parents=True, exist_ok=True)
        try:
            self._run_git("worktree", "add", "-b", branch_name, str(path), base_commit)
        except GitOperationError:
            if path.exists():
                self._run_git("worktree", "remove", "--force", str(path))
            raise
        return GitWorktree(
            task_id=task_id,
            branch_name=branch_name,
            path=path,
            base_commit=base_commit,
            created_at=datetime.now(UTC),
            status=WorktreeStatus.CLEAN,
        )

    def remove_worktree(self, task_id: UUID, *, force: bool = False) -> None:
        """Remove one managed worktree, requiring force for dirty content."""
        worktree = self.get_worktree(task_id)
        self._validate_managed_path(worktree.path)
        repository = GitRepository(worktree.path)
        if repository.is_dirty() and not force:
            raise DirtyWorktreeError(
                f"Worktree is dirty; pass force=True to remove it: {task_id}"
            )
        arguments = ["worktree", "remove"]
        if force:
            arguments.append("--force")
        arguments.append(str(worktree.path))
        self._run_git(*arguments)
        if self._branch_exists(worktree.branch_name):
            self._run_git("branch", "-D", worktree.branch_name)

    def get_worktree(self, task_id: UUID) -> GitWorktree:
        """Return metadata for one existing managed worktree."""
        for worktree in self.list_worktrees():
            if worktree.task_id == task_id:
                return worktree
        raise UnknownWorktreeError(f"Unknown worktree for task: {task_id}")

    def list_worktrees(self) -> tuple[GitWorktree, ...]:
        """List managed worktrees, excluding the main repository."""
        records = self._run_git("worktree", "list", "--porcelain")
        lines = records.splitlines()
        result: list[GitWorktree] = []
        index = 0
        while index < len(lines):
            if not lines[index].startswith("worktree "):
                index += 1
                continue
            path = Path(lines[index][9:]).resolve()
            head = ""
            branch = ""
            index += 1
            while index < len(lines) and lines[index]:
                if lines[index].startswith("HEAD "):
                    head = lines[index][5:]
                elif lines[index].startswith("branch "):
                    branch = lines[index][7:].removeprefix("refs/heads/")
                index += 1
            if self._is_managed_path(path) and branch.startswith("migrationswarm/"):
                try:
                    task_id = UUID(path.name)
                except ValueError:
                    continue
                status = (
                    WorktreeStatus.DIRTY
                    if GitRepository(path).is_dirty()
                    else WorktreeStatus.CLEAN
                )
                result.append(
                    GitWorktree(
                        task_id=task_id,
                        branch_name=branch,
                        path=path,
                        base_commit=head,
                        created_at=datetime.fromtimestamp(path.stat().st_ctime, UTC),
                        status=status,
                    )
                )
            index += 1
        return tuple(sorted(result, key=lambda item: str(item.task_id)))

    def is_dirty(self, task_id: UUID) -> bool:
        """Return whether a managed task worktree contains changes."""
        return GitRepository(self.get_worktree(task_id).path).is_dirty()

    def changed_files(self, task_id: UUID) -> tuple[str, ...]:
        """Return changed paths inside a managed task worktree."""
        return GitRepository(self.get_worktree(task_id).path).changed_files()

    def diff(self, task_id: UUID) -> str:
        """Return the tracked diff inside a managed task worktree."""
        return GitRepository(self.get_worktree(task_id).path).diff()

    def workspace_path(self, task_id: UUID) -> Path:
        """Return a validated isolated workspace path for a task."""
        return self.get_worktree(task_id).path

    def task_workspace(self, task_id: UUID) -> TaskWorkspace:
        """Return the isolated workspace contract for a task worktree."""
        worktree = self.get_worktree(task_id)
        return TaskWorkspace(
            task_id=worktree.task_id,
            repository_root=self.repository.root,
            workspace_path=worktree.path,
            branch_name=worktree.branch_name,
            base_commit=worktree.base_commit,
        )

    @staticmethod
    def branch_name(task_id: UUID) -> str:
        """Return a safe deterministic branch name for a task."""
        raw = f"migrationswarm/{task_id}"
        sanitized = _SAFE_BRANCH_CHARACTER.sub("-", raw).strip(".-/")
        return sanitized or "migrationswarm/task"

    def _task_path(self, task_id: UUID) -> Path:
        path = (self.managed_root / str(task_id)).resolve()
        if path.is_symlink():
            raise UnsafeWorktreePathError("Managed task path cannot be a symlink")
        self._validate_managed_path(path)
        return path

    def _validate_managed_path(self, path: Path) -> None:
        resolved = path.expanduser().resolve()
        if resolved == self.repository.root or not self._is_managed_path(resolved):
            raise UnsafeWorktreePathError(
                f"Path is outside managed worktrees: {resolved}"
            )

    def _is_managed_path(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.managed_root)
        except ValueError:
            return False
        return path.resolve() != self.managed_root

    def _branch_exists(self, branch_name: str) -> bool:
        try:
            self._run_git("show-ref", "--verify", f"refs/heads/{branch_name}")
        except GitOperationError:
            return False
        return True

    def _run_git(self, *arguments: str) -> str:
        return self.repository._run(*arguments)


__all__ = ["GitWorktreeManager"]
