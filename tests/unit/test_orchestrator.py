"""Tests for the controlled single-service migration orchestrator."""

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
import migrationswarm.cli.main as cli_module
from migrationswarm.agents.build_verification import (
    BuildCommandResult,
    BuildSystem,
    BuildVerificationAgent,
    BuildVerificationResult,
    UnsafeVerificationRootError,
    VerificationStatus,
    VerificationWorkspaceError,
)
from migrationswarm.agents.build_verification import (
    TestSummary as BuildTestSummary,
)
from migrationswarm.agents.debug import DebugAgent
from migrationswarm.agents.service_extraction import ServiceExtractionAgent
from migrationswarm.core.agents import AgentContext, AgentResult
from migrationswarm.core.git import GitRepository, GitWorktreeManager
from migrationswarm.core.orchestrator import (
    MigrationOrchestrator,
    MigrationRun,
    MigrationRunEvent,
    MigrationRunEventType,
    MigrationRunResult,
    MigrationRunStatus,
)
from migrationswarm.core.tasks import Task, TaskStatus, TaskType
from migrationswarm.persistence.db import Database
from migrationswarm.persistence.db.repositories import (
    AgentExecutionRepository,
    MigrationRunRepository,
    ProjectRepository,
    TaskRepository,
)
from migrationswarm.persistence.service import MigrationStateService
from tests.unit.test_service_extraction import FakeRouter, proposal_json

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="Git is unavailable")

RUNNER = CliRunner()
PROJECT_ID = UUID(int=14000)
SAMPLE_ROOT = Path(__file__).parents[2] / "examples" / "sample-spring-monolith"


def git(root: Path, *args: str) -> str:
    """Run a Git setup or inspection command."""
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
def migration_repository(tmp_path: Path) -> Path:
    """Create a clean Git repository with the checked-in migration evidence."""
    root = tmp_path / "repository"
    root.mkdir()
    shutil.copytree(SAMPLE_ROOT / "src", root / "src")
    metadata = root / ".migrationswarm"
    metadata.mkdir()
    for filename in (
        "service-boundaries.json",
        "migration-plan.json",
        "java-dependency-graph.json",
        "architecture-report.json",
    ):
        shutil.copy(SAMPLE_ROOT / ".migrationswarm" / filename, metadata / filename)
    (root / ".gitignore").write_text(".migrationswarm/\n", encoding="utf-8")
    git(root, "init")
    git(root, "config", "user.name", "MigrationSwarm Tests")
    git(root, "config", "user.email", "tests@migrationswarm.local")
    git(root, "branch", "-M", "main")
    git(root, "add", "src", ".gitignore")
    git(root, "commit", "-m", "initial monolith")
    return root


def patch_build_run(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int = 0,
) -> list[dict[str, object]]:
    """Replace only the build module's subprocess facade with a local fake."""
    calls: list[dict[str, object]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"command": command, **kwargs})
        output = "Tests run: 2, Failures: 0, Errors: 0, Skipped: 0\n"
        if returncode:
            output = "Tests run: 2, Failures: 1, Errors: 0, Skipped: 0\n"
        return subprocess.CompletedProcess(command, returncode, output, "")

    monkeypatch.setattr(
        build_module,
        "subprocess",
        SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired),
    )
    return calls


def patch_build_sequence(
    monkeypatch: pytest.MonkeyPatch,
    outputs: list[tuple[int, str]],
) -> list[dict[str, object]]:
    """Return scripted build outcomes for bounded re-verification tests."""
    calls: list[dict[str, object]] = []
    remaining = list(outputs)

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"command": command, **kwargs})
        returncode, output = remaining.pop(0)
        return subprocess.CompletedProcess(command, returncode, output, "")

    monkeypatch.setattr(
        build_module,
        "subprocess",
        SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired),
    )
    return calls


def debug_proposal_json() -> str:
    """Return a small repair for a generated service file."""
    return json.dumps(
        {
            "diagnosis": {
                "likely_cause": "The generated service source is incomplete.",
                "evidence": ["The compiler reports a generated source failure."],
                "proposed_fix_summary": "Restore the complete service source.",
                "confidence": 0.95,
            },
            "changes": [
                {
                    "path": (
                        "services/greeting-service/src/main/java/com/example/monolith/"
                        "GreetingService.java"
                    ),
                    "change_type": "modify",
                    "complete_new_content": (
                        "package com.example.monolith;\n\n" "public class GreetingService {}\n"
                    ),
                    "reasoning": "Limit the repair to the generated service source.",
                }
            ],
            "warnings": [],
        }
    )


def extraction_orchestrator(router: FakeRouter) -> MigrationOrchestrator:
    """Build an orchestrator with a provider-free extraction agent."""
    return MigrationOrchestrator(extraction_agent=ServiceExtractionAgent(router))


def test_migration_run_model_has_utc_timestamps() -> None:
    run = MigrationRun(
        repository_root=Path("."),
        selected_service="Greeting",
        task_id=uuid4(),
    )
    event = MigrationRunEvent(
        event_type=MigrationRunEventType.WORKTREE_CREATED,
        stage="preparing_workspace",
    )
    assert run.started_at.tzinfo is UTC
    assert event.occurred_at.tzinfo is UTC
    assert run.status is MigrationRunStatus.PENDING


def test_dry_run_validates_without_worktree_or_artifact(
    migration_repository: Path,
) -> None:
    result = MigrationOrchestrator().run(
        migration_repository,
        "Greeting",
        dry_run=True,
    )
    assert result.dry_run is True
    assert result.run.status is MigrationRunStatus.PENDING
    assert not (migration_repository / ".migrationswarm" / "worktrees").exists()
    assert not (migration_repository / ".migrationswarm" / "runs").exists()


def test_checked_in_sample_supports_dry_run_without_git() -> None:
    result = MigrationOrchestrator().run(SAMPLE_ROOT, "Greeting", dry_run=True)
    assert result.success is False
    assert result.dry_run is True
    assert result.run.status is MigrationRunStatus.PENDING
    assert result.task is not None


def test_candidate_validation_fails_before_worktree_creation(
    migration_repository: Path,
) -> None:
    result = MigrationOrchestrator().run(migration_repository, "Unknown", dry_run=False)
    assert result.run.status is MigrationRunStatus.FAILED
    assert result.task is None
    assert not (migration_repository / ".migrationswarm" / "worktrees").exists()


def test_migration_plan_candidate_must_match(
    migration_repository: Path,
) -> None:
    plan_path = migration_repository / ".migrationswarm" / "migration-plan.json"
    payload = json.loads(plan_path.read_text(encoding="utf-8"))
    payload["candidate_service"] = "Other Service"
    plan_path.write_text(json.dumps(payload), encoding="utf-8")
    result = MigrationOrchestrator().run(migration_repository, "Greeting")
    assert result.run.status is MigrationRunStatus.FAILED
    assert "does not match" in (result.run.failure_reason or "")


def test_successful_complete_migration_pipeline(
    migration_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = patch_build_run(monkeypatch)
    result = extraction_orchestrator(FakeRouter([proposal_json()])).run(
        migration_repository,
        "Greeting",
        timeout_seconds=12.5,
    )
    assert result.success is True
    assert result.run.status is MigrationRunStatus.COMPLETED
    assert result.task is not None
    assert result.task.status is TaskStatus.COMPLETED
    assert result.run.worktree_path is not None
    worktree = result.run.worktree_path
    assert (worktree / "services/greeting-service/pom.xml").is_file()
    assert result.verification_result is not None
    assert result.verification_result.status is VerificationStatus.PASSED
    assert calls[0]["cwd"] == worktree / "services/greeting-service"
    assert calls[0]["timeout"] == 12.5


def test_run_events_are_ordered_and_run_artifact_is_bounded(
    migration_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_build_run(monkeypatch)
    result = extraction_orchestrator(FakeRouter([proposal_json()])).run(
        migration_repository, "Greeting"
    )
    event_types = [event.event_type for event in result.run.events]
    assert event_types == [
        MigrationRunEventType.WORKTREE_CREATED,
        MigrationRunEventType.EXTRACTION_STARTED,
        MigrationRunEventType.EXTRACTION_COMPLETED,
        MigrationRunEventType.BUILD_STARTED,
        MigrationRunEventType.BUILD_PASSED,
        MigrationRunEventType.VERIFICATION_PASSED,
    ]
    artifact = migration_repository / ".migrationswarm" / "runs" / f"{result.run.run_id}.json"
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    serialized = json.dumps(payload)
    assert payload["status"] == "completed"
    assert payload["generated_files"]
    assert "complete_content" not in serialized
    assert "api_key" not in serialized.lower()
    assert GitRepository(migration_repository).changed_files() == ()


def test_extraction_failure_preserves_worktree_and_fails_task(
    migration_repository: Path,
) -> None:
    result = extraction_orchestrator(FakeRouter(["bad", "still bad"])).run(
        migration_repository, "Greeting"
    )
    assert result.run.status is MigrationRunStatus.FAILED
    assert result.task is not None
    assert result.task.status is TaskStatus.FAILED
    assert result.task.attempt == 0
    assert result.run.worktree_path is not None
    assert result.run.worktree_path.is_dir()


def test_build_failure_preserves_worktree_and_fails_task(
    migration_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_build_run(monkeypatch, returncode=1)
    result = extraction_orchestrator(FakeRouter([proposal_json()])).run(
        migration_repository, "Greeting"
    )
    assert result.run.status is MigrationRunStatus.FAILED
    assert result.task is not None
    assert result.task.status is TaskStatus.FAILED
    assert result.run.worktree_path is not None
    assert result.run.worktree_path.is_dir()
    assert result.run.generated_files


def test_eligible_failure_is_repaired_once_then_reverified(
    migration_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_build_sequence(
        monkeypatch,
        [
            (1, "COMPILATION ERROR: cannot find symbol\n"),
            (0, "Tests run: 2, Failures: 0, Errors: 0, Skipped: 0\n"),
        ],
    )
    router = FakeRouter([proposal_json()])
    debug_router = FakeRouter([debug_proposal_json()])
    result = MigrationOrchestrator(
        extraction_agent=ServiceExtractionAgent(router),
        debug_agent=DebugAgent(debug_router),
    ).run(migration_repository, "Greeting", max_debug_attempts=2)

    assert result.success is True
    assert result.run.debug_attempts == 1
    assert result.task is not None and result.task.status is TaskStatus.COMPLETED
    assert len(debug_router.requests) == 1
    assert any(
        event.event_type is MigrationRunEventType.DEBUG_APPLIED for event in result.run.events
    )
    assert any(
        event.event_type is MigrationRunEventType.REVERIFY_PASSED for event in result.run.events
    )
    artifact = (
        migration_repository
        / ".migrationswarm"
        / "debug-results"
        / str(result.run.task_id)
        / "attempt-1.json"
    )
    assert artifact.is_file()


def test_debug_attempts_are_bounded_and_exhaustion_requires_human_review(
    migration_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_build_sequence(
        monkeypatch,
        [
            (1, "COMPILATION ERROR: cannot find symbol\n"),
            (1, "COMPILATION ERROR: cannot find symbol\n"),
            (1, "COMPILATION ERROR: cannot find symbol\n"),
        ],
    )
    debug_router = FakeRouter([debug_proposal_json(), debug_proposal_json()])
    result = MigrationOrchestrator(
        extraction_agent=ServiceExtractionAgent(FakeRouter([proposal_json()])),
        debug_agent=DebugAgent(debug_router),
    ).run(migration_repository, "Greeting", max_debug_attempts=2)

    assert result.run.status is MigrationRunStatus.HUMAN_REVIEW
    assert result.run.debug_attempts == 2
    assert len(debug_router.requests) == 2
    assert (
        sum(event.event_type is MigrationRunEventType.DEBUG_STARTED for event in result.run.events)
        == 2
    )
    assert any(
        event.event_type is MigrationRunEventType.DEBUG_ATTEMPTS_EXHAUSTED
        for event in result.run.events
    )


def test_ineligible_toolchain_failure_does_not_call_debug_agent(
    migration_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_build_sequence(monkeypatch, [(1, "mvn not found\n")])
    debug_router = FakeRouter([])
    result = MigrationOrchestrator(
        extraction_agent=ServiceExtractionAgent(FakeRouter([proposal_json()])),
        debug_agent=DebugAgent(debug_router),
    ).run(migration_repository, "Greeting", max_debug_attempts=2)

    assert result.run.status is MigrationRunStatus.FAILED
    assert result.run.debug_attempts == 0
    assert debug_router.requests == []


def test_orchestrator_state_service_persists_run_task_and_executions(
    migration_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_build_run(monkeypatch)
    database = Database(f"sqlite:///{migration_repository.parent / 'state.db'}")
    database.create_all_for_tests()
    service = MigrationStateService(
        ProjectRepository(database.session_factory),
        TaskRepository(database.session_factory),
        MigrationRunRepository(database.session_factory),
        AgentExecutionRepository(database.session_factory),
    )
    result = MigrationOrchestrator(
        extraction_agent=ServiceExtractionAgent(FakeRouter([proposal_json()])),
        state_service=service,
    ).run(migration_repository, "Greeting")

    assert result.task is not None
    assert service.projects.get_required(result.task.project_id)
    assert service.tasks.get_required(result.task.id).status is TaskStatus.COMPLETED
    assert service.runs.get_required(result.run.run_id).status is MigrationRunStatus.COMPLETED
    assert len(service.executions.list_for_task(result.task.id)) >= 2


class InsufficientBuildAgent(BuildVerificationAgent):
    """Return passing-shaped evidence with a missing artifact for policy testing."""

    def execute(self, task: Task, context: AgentContext) -> AgentResult:
        del context
        verification = BuildVerificationResult(
            task_id=task.id,
            workspace_path="isolated",
            build_system=BuildSystem.MAVEN,
            commands_run=[
                BuildCommandResult(
                    command=["mvn", "test"],
                    status=VerificationStatus.PASSED,
                    exit_code=0,
                    duration_ms=1,
                )
            ],
            status=VerificationStatus.PASSED,
            exit_code=0,
            duration_ms=1,
            test_summary=BuildTestSummary(tests_run=2, failures=0, errors=0, skipped=0),
            changed_files=["services/greeting-service/pom.xml"],
        )
        return AgentResult(
            task_id=task.id,
            agent_name=self.name,
            success=True,
            summary="Passing-shaped test double with incomplete artifacts.",
            artifacts=[".migrationswarm/verification-results/missing.json"],
            metadata={"verification_result": verification.model_dump(mode="json")},
        )


def test_insufficient_evidence_requires_human_review(
    migration_repository: Path,
) -> None:
    result = MigrationOrchestrator(
        extraction_agent=ServiceExtractionAgent(FakeRouter([proposal_json()])),
        build_agent=InsufficientBuildAgent(),
    ).run(migration_repository, "Greeting")
    assert result.run.status is MigrationRunStatus.HUMAN_REVIEW
    assert result.task is not None
    assert result.task.status is TaskStatus.VERIFYING
    assert result.run.events[-1].event_type is MigrationRunEventType.HUMAN_REVIEW_REQUIRED


def test_build_verification_accepts_safe_subproject(
    migration_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = Task(
        project_id=PROJECT_ID,
        task_type=TaskType.VERIFY,
        title="Verify service",
        description="Verify extracted service.",
        status=TaskStatus.READY,
    )
    workspace = GitWorktreeManager(migration_repository).create_worktree(task)
    service_root = workspace.path / "services/greeting-service"
    service_root.mkdir(parents=True)
    (service_root / "pom.xml").write_text("<project/>\n", encoding="utf-8")
    patch_build_run(monkeypatch)
    result = BuildVerificationAgent().execute(
        task,
        AgentContext(
            project_id=task.project_id,
            task=task,
            workspace_path=str(workspace.path),
            metadata={"verification_root": "services/greeting-service"},
        ),
    )
    assert result.metadata["verification_result"]["workspace_path"].endswith(
        "services\\greeting-service"
    )
    absolute = AgentContext(
        project_id=task.project_id,
        task=task,
        workspace_path=str(workspace.path),
        metadata={"verification_root": str(service_root)},
    )
    absolute_result = BuildVerificationAgent().execute(task, absolute)
    assert absolute_result.success is True


def test_build_verification_rejects_traversal_and_main_repository(
    migration_repository: Path,
) -> None:
    task = Task(
        project_id=PROJECT_ID,
        task_type=TaskType.VERIFY,
        title="Verify service",
        description="Verify extracted service.",
        status=TaskStatus.READY,
    )
    workspace = GitWorktreeManager(migration_repository).create_worktree(task)
    agent = BuildVerificationAgent()
    traversal = AgentContext(
        project_id=task.project_id,
        task=task,
        workspace_path=str(workspace.path),
        metadata={"verification_root": "../"},
    )
    with pytest.raises(UnsafeVerificationRootError):
        agent.execute(task, traversal)
    main_context = traversal.model_copy(update={"workspace_path": str(migration_repository)})
    with pytest.raises(VerificationWorkspaceError):
        agent.execute(task, main_context)


def test_cli_dry_run_and_failure_reporting(
    migration_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = uuid4()
    fake_run = MigrationRun(
        repository_root=migration_repository,
        selected_service="Greeting Service",
        task_id=task_id,
        status=MigrationRunStatus.PENDING,
    )

    class FakeOrchestrator:
        def run(self, path: Path, service: str, **kwargs: object) -> MigrationRunResult:
            assert path == migration_repository
            assert service == "Greeting"
            assert kwargs["dry_run"] is True
            return MigrationRunResult(run=fake_run, dry_run=True)

    monkeypatch.setattr(cli_module, "MigrationOrchestrator", FakeOrchestrator)
    result = RUNNER.invoke(
        cli_module.app,
        [
            "migrate-service",
            str(migration_repository),
            "--service",
            "Greeting",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0
    assert "External call    no" in result.stdout


def test_cli_success_and_failed_status_codes(
    migration_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = uuid4()
    status = {"value": MigrationRunStatus.FAILED}

    class FakeOrchestrator:
        def run(self, *args: object, **kwargs: object) -> MigrationRunResult:
            del args, kwargs
            run = MigrationRun(
                repository_root=migration_repository,
                selected_service="Greeting Service",
                task_id=task_id,
                status=status["value"],
                failure_reason="build failed",
            )
            return MigrationRunResult(run=run)

    monkeypatch.setattr(cli_module, "MigrationOrchestrator", FakeOrchestrator)
    failed = RUNNER.invoke(
        cli_module.app,
        ["migrate-service", str(migration_repository), "--service", "Greeting"],
    )
    assert failed.exit_code == 1
    assert "build failed" in failed.stdout

    status["value"] = MigrationRunStatus.COMPLETED
    passed = RUNNER.invoke(
        cli_module.app,
        ["migrate-service", str(migration_repository), "--service", "Greeting"],
    )
    assert passed.exit_code == 0
    assert "Final status     completed" in passed.stdout
