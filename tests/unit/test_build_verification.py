"""Tests for deterministic build verification in isolated worktrees."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from typer.testing import CliRunner

import migrationswarm.agents.build_verification as verification_module
from migrationswarm.agents.build_verification import (
    BuildSystem,
    BuildVerificationAgent,
    UnsupportedBuildProjectError,
    VerificationWorkspaceError,
    parse_test_summary,
)
from migrationswarm.agents.build_verification import (
    TestSummary as VerificationTestSummary,
)
from migrationswarm.cli.main import app
from migrationswarm.core.agents import AgentContext, AgentRegistry, WorkerRuntime
from migrationswarm.core.git import GitWorktreeManager
from migrationswarm.core.scheduler import TaskGraph, TaskScheduler
from migrationswarm.core.tasks import Task, TaskStatus, TaskType

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="Git is unavailable")
RUNNER = CliRunner()
PROJECT_ID = UUID(int=9200)


def git(root: Path, *args: str) -> str:
    """Run a Git setup command for a temporary repository."""
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        encoding="utf-8",
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def isolated_task(tmp_path: Path) -> tuple[Path, Task, Path]:
    """Create a committed repository and an existing managed task worktree."""
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init")
    git(root, "config", "user.name", "MigrationSwarm Tests")
    git(root, "config", "user.email", "tests@migrationswarm.local")
    git(root, "branch", "-M", "main")
    (root / ".gitignore").write_text(".migrationswarm/\n", encoding="utf-8")
    (root / "README.md").write_text("unchanged\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    task = Task(
        id=uuid4(),
        project_id=PROJECT_ID,
        task_type=TaskType.VERIFY,
        title="Verify build",
        description="Run the safe project test command.",
        status=TaskStatus.READY,
        assigned_agent=BuildVerificationAgent.name,
    )
    workspace = GitWorktreeManager(root).create_worktree(task)
    return root, task, workspace.path


def context(task: Task, workspace: Path, **metadata: object) -> AgentContext:
    """Build a context for one isolated verification."""
    return AgentContext(
        project_id=task.project_id,
        task=task,
        workspace_path=str(workspace),
        metadata=dict(metadata),
    )


def completed(
    command: list[str],
    *,
    returncode: int = 0,
    stdout: str = "Tests run: 2, Failures: 0, Errors: 0, Skipped: 1\n",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    """Create a subprocess result for a fake local build tool."""
    return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=stderr)


def patch_run(monkeypatch: pytest.MonkeyPatch, runner: object) -> None:
    """Patch only the verifier subprocess module, preserving real Git calls."""
    monkeypatch.setattr(
        verification_module,
        "subprocess",
        SimpleNamespace(run=runner, TimeoutExpired=subprocess.TimeoutExpired),
    )


def mark_maven(workspace: Path, *, wrapper: str | None = None) -> None:
    """Add a local Maven marker to a worktree."""
    (workspace / "pom.xml").write_text("<project/>\n", encoding="utf-8")
    if wrapper:
        (workspace / wrapper).write_text("", encoding="utf-8")


def mark_gradle(workspace: Path, *, wrapper: str | None = None) -> None:
    """Add a local Gradle marker to a worktree."""
    (workspace / "build.gradle").write_text("tasks.register('test')\n", encoding="utf-8")
    if wrapper:
        (workspace / wrapper).write_text("", encoding="utf-8")


def test_missing_workspace_rejected(isolated_task: tuple[Path, Task, Path]) -> None:
    """Verification requires an isolated workspace path."""
    _, task, _ = isolated_task
    with pytest.raises(VerificationWorkspaceError, match="workspace_path"):
        BuildVerificationAgent().execute(task, AgentContext(project_id=task.project_id, task=task))


def test_main_repository_rejected(isolated_task: tuple[Path, Task, Path]) -> None:
    """The main checkout cannot be used as the verification workspace."""
    root, task, _ = isolated_task
    with pytest.raises(VerificationWorkspaceError):
        BuildVerificationAgent().execute(task, context(task, root))


def test_outside_managed_worktree_rejected(isolated_task: tuple[Path, Task, Path]) -> None:
    """A checkout outside the managed directory is rejected."""
    root, task, _ = isolated_task
    outside = root.parent / str(task.id)
    outside.mkdir()
    with pytest.raises(VerificationWorkspaceError, match="worktrees"):
        BuildVerificationAgent().execute(task, context(task, outside))


def test_unregistered_worktree_rejected(isolated_task: tuple[Path, Task, Path]) -> None:
    """A task-shaped directory is not an authorized Git worktree."""
    root, task, _ = isolated_task
    unknown_id = uuid4()
    unknown = root / ".migrationswarm" / "worktrees" / str(unknown_id)
    unknown.mkdir(parents=True)
    unknown_task = task.model_copy(update={"id": unknown_id})
    with pytest.raises(VerificationWorkspaceError, match="validate"):
        BuildVerificationAgent().execute(unknown_task, context(unknown_task, unknown))


@pytest.mark.parametrize("marker", ["pom.xml", "mvnw", "mvnw.cmd"])
def test_maven_detection(
    isolated_task: tuple[Path, Task, Path], marker: str
) -> None:
    """Each supported Maven marker selects Maven."""
    _, task, workspace = isolated_task
    (workspace / marker).write_text("", encoding="utf-8")
    assert BuildVerificationAgent.detect_build_system(workspace) is BuildSystem.MAVEN


@pytest.mark.parametrize("marker", ["build.gradle", "build.gradle.kts", "gradlew", "gradlew.bat"])
def test_gradle_detection(
    isolated_task: tuple[Path, Task, Path], marker: str
) -> None:
    """Each supported Gradle marker selects Gradle."""
    _, task, workspace = isolated_task
    (workspace / marker).write_text("", encoding="utf-8")
    assert BuildVerificationAgent.detect_build_system(workspace) is BuildSystem.GRADLE


def test_maven_wrapper_preference(
    isolated_task: tuple[Path, Task, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Maven wrapper selection prefers the platform-specific wrapper."""
    _, _, workspace = isolated_task
    mark_maven(workspace, wrapper="mvnw")
    (workspace / "mvnw.cmd").write_text("", encoding="utf-8")
    monkeypatch.setattr(verification_module, "os", SimpleNamespace(name="nt"))
    assert BuildVerificationAgent.command_for(BuildSystem.MAVEN, workspace) == [
        "mvnw.cmd",
        "test",
    ]
    monkeypatch.setattr(verification_module, "os", SimpleNamespace(name="posix"))
    assert BuildVerificationAgent.command_for(BuildSystem.MAVEN, workspace) == ["./mvnw", "test"]


def test_gradle_wrapper_preference(
    isolated_task: tuple[Path, Task, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gradle wrapper selection prefers the platform-specific wrapper."""
    _, _, workspace = isolated_task
    mark_gradle(workspace, wrapper="gradlew")
    (workspace / "gradlew.bat").write_text("", encoding="utf-8")
    monkeypatch.setattr(verification_module, "os", SimpleNamespace(name="nt"))
    assert BuildVerificationAgent.command_for(BuildSystem.GRADLE, workspace) == [
        "gradlew.bat",
        "test",
    ]
    monkeypatch.setattr(verification_module, "os", SimpleNamespace(name="posix"))
    assert BuildVerificationAgent.command_for(BuildSystem.GRADLE, workspace) == [
        "./gradlew",
        "test",
    ]


def test_maven_fallback(isolated_task: tuple[Path, Task, Path]) -> None:
    """Maven without a wrapper uses the fixed fallback command."""
    _, _, workspace = isolated_task
    mark_maven(workspace)
    assert BuildVerificationAgent.command_for(BuildSystem.MAVEN, workspace) == ["mvn", "test"]


def test_gradle_fallback(isolated_task: tuple[Path, Task, Path]) -> None:
    """Gradle without a wrapper uses the fixed fallback command."""
    _, _, workspace = isolated_task
    mark_gradle(workspace)
    assert BuildVerificationAgent.command_for(BuildSystem.GRADLE, workspace) == [
        "gradle",
        "test",
    ]


def test_unsupported_project_rejected(isolated_task: tuple[Path, Task, Path]) -> None:
    """A repository without a Maven or Gradle marker is rejected."""
    _, _, workspace = isolated_task
    with pytest.raises(UnsupportedBuildProjectError, match="marker"):
        BuildVerificationAgent.detect_build_system(workspace)


def test_allowed_command_no_shell_cwd_and_no_injection(
    isolated_task: tuple[Path, Task, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task metadata cannot alter the fixed command, cwd, or shell setting."""
    _, task, workspace = isolated_task
    mark_maven(workspace)
    calls: list[dict[str, object]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"command": command, **kwargs})
        return completed(command)

    patch_run(monkeypatch, fake_run)
    result = BuildVerificationAgent().execute(
        task,
        context(task, workspace, command=["format", ";", "whoami"], shell=True),
    )

    assert calls[0]["command"] == ["mvn", "test"]
    assert calls[0]["cwd"] == workspace
    assert calls[0]["shell"] is False
    assert result.success


def test_successful_build_result_and_stdout_capture(
    isolated_task: tuple[Path, Task, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exit code zero produces PASSED evidence and parsed test counts."""
    _, task, workspace = isolated_task
    mark_maven(workspace)
    patch_run(
        monkeypatch,
        lambda command, **kwargs: completed(
            command,
            stdout="Tests run: 5, Failures: 0, Errors: 0, Skipped: 1\n",
            stderr="tool warning\n",
        ),
    )
    result = BuildVerificationAgent().execute(task, context(task, workspace))
    evidence = result.metadata["verification_result"]
    assert result.success
    assert evidence["status"] == "passed"
    assert evidence["test_summary"] == {
        "tests_run": 5,
        "failures": 0,
        "errors": 0,
        "skipped": 1,
    }
    assert evidence["stderr_summary"] == "tool warning\n"


def test_failed_build_result(
    isolated_task: tuple[Path, Task, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-zero build exit produces FAILED evidence without being hidden."""
    _, task, workspace = isolated_task
    mark_gradle(workspace)
    patch_run(
        monkeypatch,
        lambda command, **kwargs: completed(
            command,
            returncode=1,
            stdout="5 tests completed, 2 failed, 1 skipped\n",
        ),
    )
    result = BuildVerificationAgent().execute(task, context(task, workspace))
    evidence = result.metadata["verification_result"]
    assert not result.success
    assert evidence["status"] == "failed"
    assert evidence["exit_code"] == 1
    assert evidence["test_summary"]["failures"] == 2


def test_execution_error(
    isolated_task: tuple[Path, Task, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing build executable produces ERROR evidence."""
    _, task, workspace = isolated_task
    mark_maven(workspace)

    def fail_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("tool missing")

    patch_run(monkeypatch, fail_run)
    result = BuildVerificationAgent().execute(task, context(task, workspace))
    evidence = result.metadata["verification_result"]
    assert not result.success
    assert evidence["status"] == "error"
    assert "tool missing" in evidence["warnings"][0]


def test_timeout(isolated_task: tuple[Path, Task, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """A timed-out command becomes ERROR and records a warning."""
    _, task, workspace = isolated_task
    mark_maven(workspace)

    def timeout_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        timeout_value = kwargs["timeout"]
        assert isinstance(timeout_value, int | float)
        raise subprocess.TimeoutExpired(
            command, float(timeout_value), output="partial", stderr="late"
        )

    patch_run(monkeypatch, timeout_run)
    result = BuildVerificationAgent(timeout_seconds=0.1).execute(task, context(task, workspace))
    evidence = result.metadata["verification_result"]
    assert evidence["status"] == "error"
    assert evidence["commands_run"][0]["timed_out"] is True
    assert "timed out" in evidence["warnings"][0]


def test_output_is_truncated_in_agent_result(
    isolated_task: tuple[Path, Task, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Large command output is bounded in structured evidence."""
    _, task, workspace = isolated_task
    mark_maven(workspace)
    large_output = "x" * 500
    patch_run(monkeypatch, lambda command, **kwargs: completed(command, stdout=large_output))
    result = BuildVerificationAgent(max_summary_chars=100).execute(
        task, context(task, workspace)
    )
    summary = result.metadata["verification_result"]["stdout_summary"]
    assert len(summary) < 180
    assert "truncated" in summary


def test_full_logs_and_json_artifacts_created(
    isolated_task: tuple[Path, Task, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Complete stdout/stderr are archived outside the worktree."""
    root, task, workspace = isolated_task
    mark_maven(workspace)
    patch_run(
        monkeypatch,
        lambda command, **kwargs: completed(
            command,
            stdout="full stdout\n",
            stderr="full stderr\n",
        ),
    )
    result = BuildVerificationAgent().execute(task, context(task, workspace))
    json_path = root / result.artifacts[0]
    log_path = root / result.artifacts[1]
    assert json_path.is_file()
    assert log_path.is_file()
    assert json.loads(json_path.read_text(encoding="utf-8"))["status"] == "passed"
    log = log_path.read_text(encoding="utf-8")
    assert "full stdout" in log
    assert "full stderr" in log
    assert not (workspace / ".migrationswarm" / "verification-results").exists()


def test_maven_summary_parser() -> None:
    """Maven test counts are normalized."""
    summary, warning = parse_test_summary(
        BuildSystem.MAVEN,
        "Tests run: 4, Failures: 1, Errors: 2, Skipped: 3",
    )
    assert summary == VerificationTestSummary(tests_run=4, failures=1, errors=2, skipped=3)
    assert warning is None


def test_gradle_summary_parser() -> None:
    """Gradle test counts are normalized."""
    summary, warning = parse_test_summary(
        BuildSystem.GRADLE, "7 tests completed, 2 failed, 1 skipped"
    )
    assert summary == VerificationTestSummary(tests_run=7, failures=2, errors=0, skipped=1)
    assert warning is None


def test_summary_parser_warning_does_not_fail() -> None:
    """Unrecognized output yields a warning and no fabricated counts."""
    summary, warning = parse_test_summary(BuildSystem.MAVEN, "build output without summary")
    assert summary == VerificationTestSummary()
    assert warning is not None


def test_changed_files_and_main_repository_unchanged(
    isolated_task: tuple[Path, Task, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verification reports worktree changes and never changes the main checkout."""
    root, task, workspace = isolated_task
    mark_maven(workspace)
    (workspace / "README.md").write_text("worktree only\n", encoding="utf-8")
    patch_run(monkeypatch, lambda command, **kwargs: completed(command))
    result = BuildVerificationAgent().execute(task, context(task, workspace))
    changed = result.metadata["verification_result"]["changed_files"]
    assert "README.md" in changed
    assert (root / "README.md").read_text(encoding="utf-8") == "unchanged\n"


def test_agent_result_structure(
    isolated_task: tuple[Path, Task, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent result contains evidence and both artifact paths."""
    _, task, workspace = isolated_task
    mark_maven(workspace)
    patch_run(monkeypatch, lambda command, **kwargs: completed(command))
    result = BuildVerificationAgent().execute(task, context(task, workspace))
    assert result.task_id == task.id
    assert result.agent_name == BuildVerificationAgent.name
    assert len(result.artifacts) == 2
    assert "verification_result" in result.metadata


def test_worker_runtime_transitions_success_to_verifying(
    isolated_task: tuple[Path, Task, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A passing verifier uses the normal READY -> RUNNING -> VERIFYING flow."""
    _, task, workspace = isolated_task
    mark_maven(workspace)
    patch_run(monkeypatch, lambda command, **kwargs: completed(command))
    registry = AgentRegistry()
    registry.register(BuildVerificationAgent())
    scheduler = TaskScheduler(TaskGraph([task]))
    runtime = WorkerRuntime(scheduler, registry)
    result = runtime.execute(task, context(task, workspace))
    assert result.success
    assert task.status is TaskStatus.VERIFYING


def test_cli_verify_build(
    isolated_task: tuple[Path, Task, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI locates the existing worktree and prints structured evidence."""
    root, task, workspace = isolated_task
    mark_maven(workspace)
    patch_run(monkeypatch, lambda command, **kwargs: completed(command))
    result = RUNNER.invoke(app, ["verify-build", str(root), "--task-id", str(task.id)])
    assert result.exit_code == 0
    assert "build_system=maven" in result.stdout
    assert "status=passed" in result.stdout
    assert "artifact=.migrationswarm/verification-results/" in result.stdout


def test_cli_timeout_behavior(
    isolated_task: tuple[Path, Task, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI reports timeout ERROR and returns a failing exit code."""
    root, task, workspace = isolated_task
    mark_maven(workspace)

    def timeout_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        timeout_value = kwargs["timeout"]
        assert isinstance(timeout_value, int | float)
        raise subprocess.TimeoutExpired(command, float(timeout_value))

    patch_run(monkeypatch, timeout_run)
    result = RUNNER.invoke(
        app,
        ["verify-build", str(root), "--task-id", str(task.id), "--timeout", "0.1"],
    )
    assert result.exit_code == 1
    assert "status=error" in result.stdout
    assert "skipped=n/a" in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="Windows wrapper integration")
def test_local_fake_maven_wrapper_integration(
    isolated_task: tuple[Path, Task, Path],
) -> None:
    """A local Windows Maven wrapper script verifies without network access."""
    _, task, workspace = isolated_task
    (workspace / "pom.xml").write_text("<project/>\n", encoding="utf-8")
    (workspace / "mvnw.cmd").write_text(
        "@echo off\necho Tests run: 2, Failures: 0, Errors: 0, Skipped: 0\nexit /b 0\n",
        encoding="utf-8",
    )
    result = BuildVerificationAgent(timeout_seconds=5).execute(task, context(task, workspace))
    assert result.success
    assert result.metadata["verification_result"]["status"] == "passed"


@pytest.mark.skipif(os.name == "nt", reason="Unix wrapper integration")
def test_local_fake_unix_maven_wrapper_integration(
    isolated_task: tuple[Path, Task, Path],
) -> None:
    """A local Unix Maven wrapper script verifies without network access."""
    _, task, workspace = isolated_task
    (workspace / "pom.xml").write_text("<project/>\n", encoding="utf-8")
    wrapper = workspace / "mvnw"
    wrapper.write_text(
        "#!/bin/sh\necho 'Tests run: 2, Failures: 0, Errors: 0, Skipped: 0'\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    result = BuildVerificationAgent(timeout_seconds=5).execute(task, context(task, workspace))
    assert result.success
