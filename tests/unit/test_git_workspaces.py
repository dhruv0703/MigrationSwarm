"""Integration-style tests for real Git repository and worktree isolation."""

import shutil
import subprocess
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from typer.testing import CliRunner

from migrationswarm.cli.main import app
from migrationswarm.core.agents import AgentContext
from migrationswarm.core.git import (
    DirtyWorktreeError,
    DuplicateWorktreeError,
    GitRepository,
    GitWorktreeManager,
    NotGitRepositoryError,
    UnknownWorktreeError,
    UnsafeWorktreePathError,
    WorktreeStatus,
)
from migrationswarm.core.tasks import Task, TaskType
from migrationswarm.core.workspaces import TaskWorkspace

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="Git is unavailable")
RUNNER = CliRunner()


def run_git(root: Path, *arguments: str) -> str:
    """Run a test-repository Git command."""
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        encoding="utf-8",
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def git_repository(tmp_path: Path) -> tuple[Path, str]:
    """Create a local committed repository with ignored managed workspaces."""
    root = tmp_path / "repository"
    root.mkdir()
    run_git(root, "init")
    run_git(root, "config", "user.name", "MigrationSwarm Tests")
    run_git(root, "config", "user.email", "tests@migrationswarm.local")
    run_git(root, "branch", "-M", "main")
    (root / ".gitignore").write_text(".migrationswarm/\n", encoding="utf-8")
    (root / "README.md").write_text("initial\n", encoding="utf-8")
    run_git(root, "add", ".")
    run_git(root, "commit", "-m", "initial")
    return root, run_git(root, "rev-parse", "HEAD")


def task(task_id: UUID | None = None) -> Task:
    """Create a task identity for worktree tests."""
    return Task(
        id=task_id or uuid4(),
        project_id=uuid4(),
        task_type=TaskType.CODE_REFACTOR,
        title="Change isolated code",
        description="Test isolated task workspace behavior.",
    )


def test_repository_detection_root_branch_and_head(git_repository: tuple[Path, str]) -> None:
    """Repository metadata resolves from a nested path."""
    root, commit = git_repository
    nested = root / "src"
    nested.mkdir()
    repository = GitRepository.from_path(nested)

    assert GitRepository.is_repository(nested)
    assert repository.root == root.resolve()
    assert repository.current_branch() == "main"
    assert repository.head_commit() == commit


def test_non_repository_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A normal directory is not accepted as a repository."""
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    assert not GitRepository.is_repository(tmp_path)
    with pytest.raises(NotGitRepositoryError, match="not inside a Git repository"):
        GitRepository.from_path(tmp_path)


def test_clean_dirty_changed_files_and_diff(git_repository: tuple[Path, str]) -> None:
    """Git inspection reports tracked, untracked, and diff state."""
    root, _ = git_repository
    repository = GitRepository.from_path(root)
    assert not repository.is_dirty()
    assert repository.changed_files() == ()

    (root / "README.md").write_text("changed\n", encoding="utf-8")
    (root / "new.txt").write_text("new\n", encoding="utf-8")

    assert repository.is_dirty()
    assert repository.changed_files() == ("README.md", "new.txt")
    assert "-initial" in repository.diff()
    assert "+changed" in repository.diff()


def test_create_worktree_is_deterministic_and_starts_at_head(
    git_repository: tuple[Path, str],
) -> None:
    """A task worktree has a deterministic path, branch, and base commit."""
    root, commit = git_repository
    worktree = GitWorktreeManager(root).create_worktree(task(UUID(int=1)))

    assert worktree.path == root / ".migrationswarm" / "worktrees" / str(UUID(int=1))
    assert worktree.branch_name == f"migrationswarm/{UUID(int=1)}"
    assert worktree.base_commit == commit
    assert worktree.status is WorktreeStatus.CLEAN
    assert GitRepository.from_path(worktree.path).head_commit() == commit


def test_duplicate_worktree_creation_is_rejected(git_repository: tuple[Path, str]) -> None:
    """A task cannot acquire a second managed worktree."""
    root, _ = git_repository
    manager = GitWorktreeManager(root)
    task_id = UUID(int=2)
    manager.create_worktree(task_id)

    with pytest.raises(DuplicateWorktreeError):
        manager.create_worktree(task_id)


def test_worktree_changes_are_isolated_and_diffable(git_repository: tuple[Path, str]) -> None:
    """Changes in a task worktree do not change the main checkout."""
    root, _ = git_repository
    manager = GitWorktreeManager(root)
    worktree = manager.create_worktree(task(UUID(int=3)))
    (worktree.path / "README.md").write_text("worktree change\n", encoding="utf-8")

    assert (root / "README.md").read_text(encoding="utf-8") == "initial\n"
    assert manager.is_dirty(worktree.task_id)
    assert manager.changed_files(worktree.task_id) == ("README.md",)
    assert "+worktree change" in manager.diff(worktree.task_id)
    assert manager.get_worktree(worktree.task_id).status is WorktreeStatus.DIRTY


def test_clean_removal_and_unknown_lookup(git_repository: tuple[Path, str]) -> None:
    """Clean worktrees can be removed and then become unknown."""
    root, _ = git_repository
    manager = GitWorktreeManager(root)
    worktree = manager.create_worktree(task(UUID(int=4)))
    manager.remove_worktree(worktree.task_id)

    assert not worktree.path.exists()
    assert manager.list_worktrees() == ()
    with pytest.raises(UnknownWorktreeError):
        manager.get_worktree(worktree.task_id)


def test_dirty_removal_requires_force_and_force_is_scoped(
    git_repository: tuple[Path, str],
) -> None:
    """Dirty task content is protected unless explicit force is supplied."""
    root, _ = git_repository
    manager = GitWorktreeManager(root)
    worktree = manager.create_worktree(task(UUID(int=5)))
    (worktree.path / "README.md").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(DirtyWorktreeError):
        manager.remove_worktree(worktree.task_id)
    assert worktree.path.exists()
    assert (root / "README.md").read_text(encoding="utf-8") == "initial\n"

    manager.remove_worktree(worktree.task_id, force=True)
    assert not worktree.path.exists()


def test_root_and_outside_paths_are_rejected(git_repository: tuple[Path, str]) -> None:
    """Managed-path validation cannot target the main repository or an outside path."""
    root, _ = git_repository
    manager = GitWorktreeManager(root)

    with pytest.raises(UnsafeWorktreePathError):
        manager._validate_managed_path(root)
    with pytest.raises(UnsafeWorktreePathError):
        manager._validate_managed_path(root.parent / "outside")


def test_multiple_worktrees_and_listing(git_repository: tuple[Path, str]) -> None:
    """Independent task worktrees coexist and list deterministically."""
    root, _ = git_repository
    manager = GitWorktreeManager(root)
    first = manager.create_worktree(task(UUID(int=6)))
    second = manager.create_worktree(task(UUID(int=7)))

    listed = manager.list_worktrees()
    assert [item.task_id for item in listed] == sorted([first.task_id, second.task_id], key=str)
    assert {item.branch_name for item in listed} == {
        f"migrationswarm/{first.task_id}",
        f"migrationswarm/{second.task_id}",
    }


def test_task_workspace_and_agent_context_contract(
    git_repository: tuple[Path, str],
) -> None:
    """TaskWorkspace gives agents only the isolated worktree path."""
    root, commit = git_repository
    manager = GitWorktreeManager(root)
    worktree = manager.create_worktree(task(UUID(int=8)))
    workspace = manager.task_workspace(worktree.task_id)
    context = AgentContext(
        project_id=uuid4(),
        task=task(worktree.task_id),
        workspace_path=str(workspace.workspace_path),
    )

    assert workspace.workspace_path != workspace.repository_root
    assert context.workspace_path == str(worktree.path)
    with pytest.raises(ValueError, match="repository root"):
        TaskWorkspace(
            task_id=worktree.task_id,
            repository_root=root,
            workspace_path=root,
            branch_name=worktree.branch_name,
            base_commit=commit,
        )


def test_cli_git_status(git_repository: tuple[Path, str]) -> None:
    """The Git status CLI is read-only and concise."""
    root, commit = git_repository
    result = RUNNER.invoke(app, ["git-status", str(root)])

    assert result.exit_code == 0
    assert f"repository root={root}" in result.stdout
    assert "branch=main" in result.stdout
    assert f"HEAD={commit}" in result.stdout
    assert "status=clean" in result.stdout


def test_cli_create_list_and_remove_worktree(git_repository: tuple[Path, str]) -> None:
    """All worktree CLI commands operate on the managed task directory."""
    root, _ = git_repository
    task_id = UUID(int=9)
    create = RUNNER.invoke(
        app, ["create-worktree", str(root), "--task-id", str(task_id)]
    )
    assert create.exit_code == 0
    assert f"task_id={task_id}" in create.stdout
    assert f"branch=migrationswarm/{task_id}" in create.stdout

    listed = RUNNER.invoke(app, ["worktrees", str(root)])
    assert listed.exit_code == 0
    assert str(task_id) in listed.stdout

    removed = RUNNER.invoke(
        app, ["remove-worktree", str(root), "--task-id", str(task_id)]
    )
    assert removed.exit_code == 0
    assert f"removed task_id={task_id}" in removed.stdout
