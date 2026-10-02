"""Small subprocess-backed Git repository facade."""

import shutil
import subprocess
from pathlib import Path
from typing import Final

from migrationswarm.core.git.exceptions import (
    GitOperationError,
    GitRepositoryError,
    GitUnavailableError,
    NotGitRepositoryError,
)
from migrationswarm.core.security import MAX_ARTIFACT_BYTES, redact_text, require_bytes

GIT_EXECUTABLE: Final[str] = "git"


class GitRepository:
    """Read repository state and run narrowly scoped Git commands."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()

    @classmethod
    def from_path(cls, path: str | Path) -> "GitRepository":
        """Resolve a repository root from any path inside the repository."""
        root = cls.resolve_root(path)
        return cls(root)

    @staticmethod
    def git_available() -> bool:
        """Return whether the installed Git executable can be located."""
        return shutil.which(GIT_EXECUTABLE) is not None

    @classmethod
    def is_repository(cls, path: str | Path) -> bool:
        """Return whether a path is inside a Git work tree."""
        try:
            cls.resolve_root(path)
        except GitRepositoryError:
            return False
        return True

    @classmethod
    def resolve_root(cls, path: str | Path) -> Path:
        """Resolve and return the top-level repository directory."""
        candidate = Path(path).expanduser().resolve()
        output = cls._run_at(candidate, "rev-parse", "--show-toplevel")
        return Path(output.strip()).resolve()

    def current_branch(self) -> str:
        """Return the current branch name, or ``HEAD`` when detached."""
        return self._run("rev-parse", "--abbrev-ref", "HEAD").strip()

    def head_commit(self) -> str:
        """Return the full current HEAD commit hash."""
        return self._run("rev-parse", "HEAD").strip()

    def is_dirty(self) -> bool:
        """Return whether tracked or untracked files have changed."""
        return bool(self._run("status", "--porcelain=v1", "--untracked-files=all").strip())

    def changed_files(self) -> tuple[str, ...]:
        """Return changed tracked and untracked paths in deterministic order."""
        output = self._run("status", "--porcelain=v1", "--untracked-files=all")
        paths: set[str] = set()
        for line in output.splitlines():
            if len(line) < 4:
                continue
            path = line[3:]
            if " -> " in path:
                path = path.rsplit(" -> ", 1)[-1]
            paths.add(path)
        return tuple(sorted(paths))

    def diff(self) -> str:
        """Return the textual diff of tracked changes against HEAD."""
        return self._run("diff", "HEAD", "--")

    def _run(self, *arguments: str) -> str:
        """Run one Git command with this repository as its working directory."""
        return self._run_at(self.root, *arguments)

    @classmethod
    def _run_at(cls, cwd: Path, *arguments: str) -> str:
        """Run one controlled Git command and normalize failures."""
        if not cls.git_available():
            raise GitUnavailableError("Git executable is not available")
        try:
            completed = subprocess.run(
                [GIT_EXECUTABLE, "-C", str(cwd), *arguments],
                capture_output=True,
                check=False,
                encoding="utf-8",
                errors="replace",
                text=True,
                shell=False,
                timeout=30,
            )
        except OSError as error:
            raise GitUnavailableError("Could not execute Git") from error
        except subprocess.TimeoutExpired as error:
            raise GitOperationError("Git command timed out") from error
        if completed.returncode != 0:
            message = redact_text(completed.stderr.strip() or completed.stdout.strip())[:500]
            if arguments[:2] == ("rev-parse", "--show-toplevel"):
                raise NotGitRepositoryError(
                    f"Path is not inside a Git repository: {cwd}"
                ) from None
            raise GitOperationError(
                f"Git command failed ({' '.join(arguments)}): {message}"
            )
        try:
            require_bytes(completed.stdout, MAX_ARTIFACT_BYTES, label="Git output")
        except ValueError as error:
            raise GitOperationError("Git output exceeded the safety limit") from error
        return completed.stdout


__all__ = ["GitRepository", "GIT_EXECUTABLE"]
