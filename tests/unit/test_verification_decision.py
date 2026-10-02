"""Tests for deterministic verification decisions and lifecycle completion."""

import json
import shutil
import subprocess
from datetime import UTC
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from typer.testing import CliRunner

import migrationswarm.agents.build_verification as build_module
from migrationswarm.agents.build_verification import (
    BuildCommandResult,
    BuildSystem,
    BuildVerificationAgent,
    BuildVerificationResult,
)
from migrationswarm.agents.build_verification import (
    TestSummary as BuildTestSummary,
)
from migrationswarm.agents.build_verification import (
    VerificationStatus as BuildStatus,
)
from migrationswarm.agents.refactor import RefactorAgent
from migrationswarm.cli.main import app
from migrationswarm.core.agents import AgentContext, AgentRegistry, WorkerRuntime
from migrationswarm.core.git import GitWorktreeManager
from migrationswarm.core.models import ModelRequest, ModelResponse
from migrationswarm.core.scheduler import TaskGraph, TaskScheduler
from migrationswarm.core.tasks import Task, TaskStateMachine, TaskStatus, TaskType
from migrationswarm.core.verification import (
    DECISION_RESULTS_DIR,
    VerificationCoordinator,
    VerificationDecision,
    VerificationDecisionEngine,
    VerificationEvidence,
    VerificationStateError,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="Git is unavailable")
RUNNER = CliRunner()
PROJECT_ID = UUID(int=9300)


def make_task(
    *,
    task_type: TaskType = TaskType.CODE_REFACTOR,
    status: TaskStatus = TaskStatus.VERIFYING,
) -> Task:
    """Create a task for decision tests."""
    return Task(
        project_id=PROJECT_ID,
        task_type=task_type,
        title="Verify task",
        description="Evaluate deterministic verification evidence.",
        status=status,
    )


def make_evidence(
    tmp_path: Path,
    task: Task,
    *,
    build_status: BuildStatus = BuildStatus.PASSED,
    build_exit_code: int | None = 0,
    tests_run: int | None = 2,
    test_failures: int | None = 0,
    test_errors: int | None = 0,
    test_skipped: int | None = 0,
    changed_files: list[str] | None = None,
    warnings: list[str] | None = None,
) -> VerificationEvidence:
    """Create evidence with two real required artifact files."""
    artifact_dir = tmp_path / ".migrationswarm" / "verification-results"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    json_artifact = artifact_dir / f"{task.id}.json"
    log_artifact = artifact_dir / f"{task.id}.log"
    json_artifact.write_text("{}\n", encoding="utf-8")
    log_artifact.write_text("verification\n", encoding="utf-8")
    return VerificationEvidence(
        task_id=task.id,
        build_status=build_status,
        build_exit_code=build_exit_code,
        tests_run=tests_run,
        test_failures=test_failures,
        test_errors=test_errors,
        test_skipped=test_skipped,
        changed_files=changed_files if changed_files is not None else ["src/A.java"],
        artifact_paths=[str(json_artifact), str(log_artifact)],
        warnings=warnings or [],
    )


def test_passed_build_produces_passed_decision(tmp_path: Path) -> None:
    """Complete passing evidence produces PASSED."""
    task = make_task()
    result = VerificationDecisionEngine(artifact_root=tmp_path).decide(
        task, make_evidence(tmp_path, task)
    )
    assert result.decision is VerificationDecision.PASSED
    assert result.reasons == ["Build and test evidence satisfied the verification policy."]


@pytest.mark.parametrize("status", [BuildStatus.FAILED, BuildStatus.ERROR])
def test_failed_or_error_build_produces_failed_decision(
    tmp_path: Path, status: BuildStatus
) -> None:
    """Build failures and execution errors cannot pass lifecycle verification."""
    task = make_task()
    result = VerificationDecisionEngine(artifact_root=tmp_path).decide(
        task, make_evidence(tmp_path, task, build_status=status, build_exit_code=1)
    )
    assert result.decision is VerificationDecision.FAILED
    assert result.reasons == [f"Build verification status was {status.value}."]


def test_nonzero_exit_code_produces_failed_decision(tmp_path: Path) -> None:
    """A non-zero exit code fails even if the reported status says passed."""
    task = make_task()
    result = VerificationDecisionEngine(artifact_root=tmp_path).decide(
        task, make_evidence(tmp_path, task, build_exit_code=2)
    )
    assert result.decision is VerificationDecision.FAILED
    assert result.reasons == ["Build verification exit code was 2."]


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("test_failures", 1, "Test failures were reported: 1."),
        ("test_errors", 2, "Test errors were reported: 2."),
    ],
)
def test_test_failures_and_errors_fail(
    tmp_path: Path, field: str, value: int, reason: str
) -> None:
    """Reported test failures and errors fail otherwise passing evidence."""
    task = make_task()
    failures = value if field == "test_failures" else 0
    errors = value if field == "test_errors" else 0
    result = VerificationDecisionEngine(artifact_root=tmp_path).decide(
        task,
        make_evidence(tmp_path, task, test_failures=failures, test_errors=errors),
    )
    assert result.decision is VerificationDecision.FAILED
    assert reason in result.reasons


def test_zero_tests_passes_with_warning(tmp_path: Path) -> None:
    """A successful no-test build passes but surfaces a warning."""
    task = make_task()
    result = VerificationDecisionEngine(artifact_root=tmp_path).decide(
        task, make_evidence(tmp_path, task, tests_run=0)
    )
    assert result.decision is VerificationDecision.PASSED
    assert "No tests were discovered or executed." in result.warnings


def test_missing_changed_files_is_insufficient_for_refactor(tmp_path: Path) -> None:
    """Code-changing tasks require at least one changed file."""
    task = make_task(task_type=TaskType.CODE_REFACTOR)
    result = VerificationDecisionEngine(artifact_root=tmp_path).decide(
        task, make_evidence(tmp_path, task, changed_files=[])
    )
    assert result.decision is VerificationDecision.INSUFFICIENT_EVIDENCE
    assert "No changed files" in result.reasons[0]


def test_missing_required_artifact_is_insufficient(tmp_path: Path) -> None:
    """Evidence without the required JSON and log artifacts cannot pass."""
    task = make_task()
    evidence = make_evidence(tmp_path, task)
    evidence.artifact_paths = [evidence.artifact_paths[0]]
    result = VerificationDecisionEngine(artifact_root=tmp_path).decide(task, evidence)
    assert result.decision is VerificationDecision.INSUFFICIENT_EVIDENCE
    assert any("Required verification artifacts are missing" in reason for reason in result.reasons)


def test_nonfatal_warning_does_not_fail(tmp_path: Path) -> None:
    """Ordinary warnings remain visible but do not fail the decision."""
    task = make_task()
    result = VerificationDecisionEngine(artifact_root=tmp_path).decide(
        task, make_evidence(tmp_path, task, warnings=["Compiler emitted a note."])
    )
    assert result.decision is VerificationDecision.PASSED
    assert result.warnings == ["Compiler emitted a note."]


def test_fatal_warning_fails(tmp_path: Path) -> None:
    """Warnings marked fatal fail otherwise passing evidence."""
    task = make_task()
    result = VerificationDecisionEngine(artifact_root=tmp_path).decide(
        task, make_evidence(tmp_path, task, warnings=["fatal: evidence is stale"])
    )
    assert result.decision is VerificationDecision.FAILED
    assert any("Fatal verification warning" in reason for reason in result.reasons)


def test_reasons_are_deterministic(tmp_path: Path) -> None:
    """Repeated evaluation returns identical reasons and warnings."""
    task = make_task()
    evidence = make_evidence(tmp_path, task, test_failures=1, tests_run=0)
    engine = VerificationDecisionEngine(artifact_root=tmp_path)
    first = engine.decide(task, evidence)
    second = engine.decide(task, evidence)
    assert first.reasons == second.reasons
    assert first.warnings == second.warnings


def test_decision_timestamp_is_timezone_aware_utc(tmp_path: Path) -> None:
    """Decision timestamps are aware and normalized to UTC."""
    task = make_task()
    result = VerificationDecisionEngine(artifact_root=tmp_path).decide(
        task, make_evidence(tmp_path, task)
    )
    assert result.decided_at.tzinfo is UTC
    assert result.decided_at.utcoffset() == UTC.utcoffset(result.decided_at)


def test_coordinator_rejects_non_verifying_task(tmp_path: Path) -> None:
    """Lifecycle decisions accept only VERIFYING tasks."""
    task = make_task(status=TaskStatus.READY)
    with pytest.raises(VerificationStateError, match="VERIFYING"):
        VerificationCoordinator(tmp_path).decide(task, make_evidence(tmp_path, task))


def test_passed_decision_transitions_to_completed(tmp_path: Path) -> None:
    """PASSED uses TaskStateMachine for VERIFYING -> COMPLETED."""
    task = make_task()
    result = VerificationCoordinator(tmp_path).decide(task, make_evidence(tmp_path, task))
    assert result.decision is VerificationDecision.PASSED
    assert task.status is TaskStatus.COMPLETED


def test_failed_decision_transitions_to_failed(tmp_path: Path) -> None:
    """FAILED uses TaskStateMachine for VERIFYING -> FAILED."""
    task = make_task()
    result = VerificationCoordinator(tmp_path).decide(
        task, make_evidence(tmp_path, task, build_status=BuildStatus.FAILED, build_exit_code=1)
    )
    assert result.decision is VerificationDecision.FAILED
    assert task.status is TaskStatus.FAILED


def test_insufficient_evidence_leaves_task_verifying(tmp_path: Path) -> None:
    """INSUFFICIENT_EVIDENCE does not silently complete or fail the task."""
    task = make_task()
    result = VerificationCoordinator(tmp_path).decide(
        task, make_evidence(tmp_path, task, changed_files=[])
    )
    assert result.decision is VerificationDecision.INSUFFICIENT_EVIDENCE
    assert task.status is TaskStatus.VERIFYING


def test_coordinator_uses_task_state_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Coordinator delegates lifecycle changes to the centralized state machine."""
    calls: list[TaskStatus] = []
    original = TaskStateMachine.transition

    def spy(cls: type[TaskStateMachine], task: Task, target: TaskStatus) -> Task:
        calls.append(target)
        return original(task, target)

    monkeypatch.setattr(TaskStateMachine, "transition", classmethod(spy))
    task = make_task()
    VerificationCoordinator(tmp_path).decide(task, make_evidence(tmp_path, task))
    assert calls == [TaskStatus.COMPLETED]


def test_completed_task_remains_terminal(tmp_path: Path) -> None:
    """The coordinator rejects already completed tasks before any transition."""
    task = make_task(status=TaskStatus.COMPLETED)
    with pytest.raises(VerificationStateError):
        VerificationCoordinator(tmp_path).decide(task, make_evidence(tmp_path, task))
    assert task.status is TaskStatus.COMPLETED


def test_decision_artifact_creation(tmp_path: Path) -> None:
    """Coordinator writes a normalized decision artifact without logs."""
    task = make_task()
    result = VerificationCoordinator(tmp_path).decide(task, make_evidence(tmp_path, task))
    assert result.artifact_path is not None
    artifact = tmp_path / result.artifact_path
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert result.artifact_path == f"{DECISION_RESULTS_DIR}/{task.id}.json"
    assert payload["decision"] == "passed"
    assert "stdout" not in json.dumps(payload).lower()


def write_cli_result(
    root: Path,
    task_id: UUID,
    *,
    status: BuildStatus,
    exit_code: int | None,
    changed_files: list[str],
) -> None:
    """Write a local verification result in the production artifact format."""
    artifact_dir = root / ".migrationswarm" / "verification-results"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    json_path = artifact_dir / f"{task_id}.json"
    log_path = artifact_dir / f"{task_id}.log"
    result = BuildVerificationResult(
        task_id=task_id,
        workspace_path=str(root / ".migrationswarm" / "worktrees" / str(task_id)),
        build_system=BuildSystem.MAVEN,
        commands_run=[
            BuildCommandResult(
                command=["mvn", "test"],
                status=status,
                exit_code=exit_code,
                duration_ms=1,
            )
        ],
        status=status,
        exit_code=exit_code,
        duration_ms=1,
        test_summary=BuildTestSummary(tests_run=2, failures=0, errors=0, skipped=0),
        changed_files=changed_files,
    )
    json_path.write_text(
        json.dumps(
            {
                **result.model_dump(mode="json"),
                "task_type": TaskType.CODE_REFACTOR.value,
                "artifacts": {
                    "json": str(json_path.relative_to(root).as_posix()),
                    "log": str(log_path.relative_to(root).as_posix()),
                },
            }
        ),
        encoding="utf-8",
    )
    log_path.write_text("local build output\n", encoding="utf-8")


def init_cli_repository(tmp_path: Path) -> Path:
    """Create a minimal local Git repository for the decision CLI."""
    root = tmp_path / "cli-repository"
    root.mkdir()
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    return root


def test_cli_passing_result(tmp_path: Path) -> None:
    """CLI evaluates persisted passing evidence."""
    root = init_cli_repository(tmp_path)
    task_id = uuid4()
    write_cli_result(
        root, task_id, status=BuildStatus.PASSED, exit_code=0, changed_files=["A.java"]
    )
    result = RUNNER.invoke(app, ["decide-verification", str(root), "--task-id", str(task_id)])
    assert result.exit_code == 0
    assert "decision=passed" in result.stdout
    assert "verification-decisions" in result.stdout


def test_cli_failing_result(tmp_path: Path) -> None:
    """CLI evaluates persisted failed evidence."""
    root = init_cli_repository(tmp_path)
    task_id = uuid4()
    write_cli_result(root, task_id, status=BuildStatus.FAILED, exit_code=1, changed_files=[])
    result = RUNNER.invoke(app, ["decide-verification", str(root), "--task-id", str(task_id)])
    assert result.exit_code == 0
    assert "decision=failed" in result.stdout


def test_cli_insufficient_evidence_result(tmp_path: Path) -> None:
    """CLI reports insufficient evidence instead of completing the task."""
    root = init_cli_repository(tmp_path)
    task_id = uuid4()
    write_cli_result(root, task_id, status=BuildStatus.PASSED, exit_code=0, changed_files=[])
    result = RUNNER.invoke(app, ["decide-verification", str(root), "--task-id", str(task_id)])
    assert result.exit_code == 0
    assert "decision=insufficient_evidence" in result.stdout


class FakeRefactorRouter:
    """Router returning one deterministic complete-file refactor proposal."""

    def generate(self, request: ModelRequest) -> ModelResponse:
        del request
        content = (
            '{"summary":"Create facade","changes":[{"path":"src/main/java/com/example/monolith/'
            'GreetingFacade.java","change_type":"create","complete_new_content":"package '
            'com.example.monolith;\\n\\npublic class GreetingFacade {}\\n","reasoning":"narrow"}]}'
        )
        return ModelResponse(provider="fake", model="fake", content=content, latency_ms=1.0)


def git(root: Path, *args: str) -> None:
    """Run a Git setup command."""
    subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        encoding="utf-8",
        text=True,
    )


def test_full_ready_to_completed_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fake refactor, fake passing build, and coordinator complete one task."""
    root = tmp_path / "lifecycle"
    source = root / "src/main/java/com/example/monolith/GreetingService.java"
    root.mkdir()
    source.parent.mkdir(parents=True)
    git(root, "init")
    git(root, "config", "user.name", "MigrationSwarm Tests")
    git(root, "config", "user.email", "tests@migrationswarm.local")
    (root / ".gitignore").write_text(".migrationswarm/\n", encoding="utf-8")
    source.write_text(
        "package com.example.monolith;\npublic class GreetingService {}\n",
        encoding="utf-8",
    )
    git(root, "add", ".")
    git(root, "commit", "-m", "initial")
    task = Task(
        id=uuid4(),
        project_id=PROJECT_ID,
        task_type=TaskType.CODE_REFACTOR,
        title="Create facade",
        description="Create GreetingFacade in the same package.",
        status=TaskStatus.PENDING,
        assigned_agent=RefactorAgent.name,
    )
    workspace = GitWorktreeManager(root).create_worktree(task)
    refactor_context = AgentContext(
        project_id=task.project_id,
        task=task,
        workspace_path=str(workspace.path),
        metadata={
            "affected_files": ["src/main/java/com/example/monolith/GreetingService.java"],
            "expected_outputs": ["GreetingFacade.java"],
            "acceptance_criteria": ["Facade exists."],
        },
    )
    registry = AgentRegistry()
    registry.register(RefactorAgent(FakeRefactorRouter()))
    scheduler = TaskScheduler(TaskGraph([task]))
    scheduler.schedule()
    runtime = WorkerRuntime(scheduler, registry)
    refactor_result = runtime.execute(task, refactor_context)
    assert refactor_result.success
    assert task.status is TaskStatus.VERIFYING

    (workspace.path / "pom.xml").write_text("<project/>\n", encoding="utf-8")
    monkeypatch.setattr(
        build_module,
        "subprocess",
        SimpleNamespace(
            run=lambda command, **kwargs: subprocess.CompletedProcess(
                command,
                0,
                stdout="Tests run: 1, Failures: 0, Errors: 0, Skipped: 0\n",
                stderr="",
            ),
            TimeoutExpired=subprocess.TimeoutExpired,
        ),
    )
    build_result = BuildVerificationAgent().execute(task, refactor_context)
    evidence_payload = build_result.metadata["verification_result"]
    evidence = VerificationEvidence.from_build_result(
        BuildVerificationResult.model_validate(evidence_payload),
        [str(root / artifact) for artifact in build_result.artifacts],
    )
    decision = VerificationCoordinator(root).decide(task, evidence)
    assert decision.decision is VerificationDecision.PASSED
    assert task.status.value == TaskStatus.COMPLETED.value
