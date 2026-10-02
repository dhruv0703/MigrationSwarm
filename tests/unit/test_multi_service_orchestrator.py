"""Deterministic tests for bounded multi-service orchestration."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Lock
from uuid import UUID, uuid4

import pytest
from typer.testing import CliRunner

from migrationswarm.agents.migration_planning import (
    MigrationPhase,
    MigrationPlan,
    MigrationStep,
    RiskLevel,
)
from migrationswarm.agents.service_boundary import (
    BoundaryRisk,
    CandidateService,
    ServiceBoundaryReport,
)
from migrationswarm.cli.main import app
from migrationswarm.core.orchestrator import (
    MultiServiceMigrationOrchestrator,
    MultiServiceMigrationStatus,
    ServiceDependencyCycleError,
    ServiceMigrationStateStatus,
)
from migrationswarm.core.orchestrator.approvals import update_approval
from migrationswarm.core.orchestrator.models import (
    MigrationRun,
    MigrationRunResult,
    MigrationRunStatus,
)
from migrationswarm.core.tasks.enums import TaskType
from migrationswarm.persistence.db import Database
from migrationswarm.persistence.db.repositories import MultiMigrationRunRepository


def _candidate(name: str, dependencies: list[str] | None = None) -> CandidateService:
    return CandidateService(
        name=name,
        description=f"The {name} domain.",
        classes=[f"example.{name.replace(' ', '')}"],
        packages=[f"example.{name.casefold()}"],
        controllers=[],
        services=[],
        repositories=[],
        confidence=0.9,
        reasoning="Grounded test candidate.",
        dependencies_on_other_candidates=dependencies or [],
        risks=[BoundaryRisk(description="Test risk", severity="low")],
    )


def _plan(name: str) -> MigrationPlan:
    step = MigrationStep(
        step_id="extract",
        phase_id="phase-1",
        title="Extract files",
        description="Copy candidate files into the service workspace.",
        task_type=TaskType.CODE_REFACTOR,
        affected_classes=[f"example.{name.replace(' ', '')}"],
        expected_outputs=["service files"],
        acceptance_criteria=["files are present"],
        risk_level=RiskLevel.LOW,
    )
    return MigrationPlan(
        repository="test-repository",
        target_architecture="bounded services",
        candidate_service=name,
        phases=[
            MigrationPhase(
                phase_id="phase-1",
                title="Extraction",
                description="Extract the candidate.",
                step_ids=["extract"],
            )
        ],
        steps=[step],
        estimated_task_count=1,
        model_provider="test",
        model_name="test",
    )


def _repository(
    tmp_path: Path,
    candidates: list[CandidateService],
    approved: list[str],
) -> Path:
    evidence = tmp_path / ".migrationswarm"
    evidence.mkdir(parents=True)
    report = ServiceBoundaryReport(
        candidate_services=candidates,
        shared_components=[],
        unresolved_classes=[],
        overall_reasoning="Test evidence.",
        warnings=[],
        model_provider="test",
        model_name="test",
    )
    (evidence / "service-boundaries.json").write_text(
        json.dumps(report.model_dump(mode="json")), encoding="utf-8"
    )
    (evidence / "service-approvals.json").write_text(
        json.dumps({"approved": approved}), encoding="utf-8"
    )
    for candidate in candidates:
        (evidence / "migration-plans").mkdir(exist_ok=True)
        (evidence / "migration-plans" / f"{candidate.name.casefold()}.json").write_text(
            json.dumps(_plan(candidate.name).model_dump(mode="json")), encoding="utf-8"
        )
    return tmp_path


class FakeWorkflow:
    def __init__(
        self,
        callback: Callable[[Path, str], MigrationRunResult],
    ) -> None:
        self.callback = callback

    def run(self, root: str | Path, service: str, **kwargs: object) -> MigrationRunResult:
        return self.callback(Path(root), service)


def _workflow_result(
    root: Path,
    service: str,
    status: MigrationRunStatus,
    repairs: int = 0,
) -> MigrationRunResult:
    task_id = uuid4()
    run = MigrationRun(
        repository_root=root,
        selected_service=service,
        task_id=task_id,
        worktree_path=root / ".migrationswarm" / "worktrees" / str(task_id),
        status=status,
        current_stage=status.value,
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        debug_attempts=repairs,
        max_debug_attempts=2,
    )
    return MigrationRunResult(run=run)


def _orchestrator(
    callback: Callable[[Path, str], MigrationRunResult],
) -> MultiServiceMigrationOrchestrator:
    return MultiServiceMigrationOrchestrator(
        workflow_factory=lambda: FakeWorkflow(callback)
    )


def test_approvals_are_explicit_and_cli_can_manage_them(tmp_path: Path) -> None:
    repository = _repository(tmp_path, [_candidate("Inventory")], [])
    assert update_approval(repository, "Inventory", approved=True).approved == ["Inventory"]
    runner = CliRunner()
    result = runner.invoke(app, ["approvals", str(repository)])
    assert result.exit_code == 0
    assert "Inventory" in result.stdout
    result = runner.invoke(
        app, ["revoke-service", str(repository), "--service", "Inventory"]
    )
    assert result.exit_code == 0
    assert json.loads(
        (repository / ".migrationswarm" / "service-approvals.json").read_text()
    ) == {"approved": []}


def test_unapproved_service_is_rejected_without_workflow(tmp_path: Path) -> None:
    repository = _repository(tmp_path, [_candidate("Inventory")], [])
    calls: list[str] = []

    def callback(root: Path, service: str) -> MigrationRunResult:
        calls.append(service)
        pytest.fail("unapproved service must not execute")

    result = _orchestrator(callback)
    with pytest.raises(ValueError, match="Unapproved"):
        result.run(repository, services=["Inventory"])
    assert calls == []


def test_dry_run_reports_dag_and_does_not_execute_or_write_run_artifacts(tmp_path: Path) -> None:
    candidates = [
        _candidate("Inventory"),
        _candidate("Orders", ["Inventory"]),
        _candidate("Notifications"),
    ]
    repository = _repository(tmp_path, candidates, [item.name for item in candidates])
    calls: list[str] = []

    def callback(root: Path, service: str) -> MigrationRunResult:
        calls.append(service)
        pytest.fail("dry run must not execute")

    result = _orchestrator(callback).run(
        repository, all_approved=True, service_workers=2, dry_run=True
    )
    assert result.root_services == ["Inventory", "Notifications"]
    assert [(edge.service_name, edge.depends_on) for edge in result.run.dependencies] == [
        ("Orders", "Inventory")
    ]
    assert calls == []
    assert not (repository / ".migrationswarm" / "multi-runs").exists()


def test_inventory_orders_notifications_dependency_and_concurrency_flow(tmp_path: Path) -> None:
    candidates = [
        _candidate("Inventory"),
        _candidate("Orders", ["Inventory"]),
        _candidate("Notifications"),
    ]
    repository = _repository(tmp_path, candidates, [item.name for item in candidates])
    barrier = Barrier(2)
    lock = Lock()
    calls: list[str] = []
    active = 0
    peak = 0

    def callback(root: Path, service: str) -> MigrationRunResult:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            calls.append(service)
        if service in {"Inventory", "Notifications"}:
            barrier.wait(timeout=2)
        with lock:
            active -= 1
        return _workflow_result(root, service, MigrationRunStatus.COMPLETED)

    result = _orchestrator(callback).run(repository, all_approved=True, service_workers=2)
    states = {state.service_name: state for state in result.run.services}
    assert result.run.status is MultiServiceMigrationStatus.COMPLETED
    assert all(state.status is ServiceMigrationStateStatus.COMPLETED for state in states.values())
    assert calls[:2] == ["Inventory", "Notifications"]
    assert calls[2:] == ["Orders"]
    assert peak == 2
    assert states["Orders"].dependencies == ["Inventory"]


def test_failed_branch_isolated_and_descendants_are_blocked(tmp_path: Path) -> None:
    candidates = [
        _candidate("Inventory"),
        _candidate("Orders", ["Inventory"]),
        _candidate("Notifications"),
    ]
    repository = _repository(tmp_path, candidates, [item.name for item in candidates])

    def callback(root: Path, service: str) -> MigrationRunResult:
        status = (
            MigrationRunStatus.FAILED
            if service == "Inventory"
            else MigrationRunStatus.COMPLETED
        )
        return _workflow_result(root, service, status)

    result = _orchestrator(callback).run(repository, all_approved=True, service_workers=2)
    states = {state.service_name: state for state in result.run.services}
    assert result.run.status is MultiServiceMigrationStatus.PARTIALLY_COMPLETED
    assert states["Inventory"].status is ServiceMigrationStateStatus.FAILED
    assert states["Orders"].status is ServiceMigrationStateStatus.BLOCKED
    assert states["Notifications"].status is ServiceMigrationStateStatus.COMPLETED


def test_repair_attempt_summary_is_preserved(tmp_path: Path) -> None:
    repository = _repository(tmp_path, [_candidate("Inventory")], ["Inventory"])

    def callback(root: Path, service: str) -> MigrationRunResult:
        return _workflow_result(root, service, MigrationRunStatus.COMPLETED, repairs=1)

    result = _orchestrator(callback).run(repository, services=["Inventory"])
    assert result.run.services[0].repair_attempts == 1
    artifact = next((repository / ".migrationswarm" / "multi-runs").glob("*.json"))
    payload = json.loads(artifact.read_text())
    assert payload["services"][0]["repair_attempts"] == 1


def test_dependency_cycle_requires_human_review_without_inventing_order(tmp_path: Path) -> None:
    candidates = [
        _candidate("Inventory", ["Orders"]),
        _candidate("Orders", ["Inventory"]),
    ]
    repository = _repository(tmp_path, candidates, [item.name for item in candidates])
    result = _orchestrator(lambda root, service: pytest.fail("cycle must not execute")).run(
        repository, all_approved=True
    )
    assert result.run.status is MultiServiceMigrationStatus.HUMAN_REVIEW
    assert all(
        state.status is ServiceMigrationStateStatus.HUMAN_REVIEW
        for state in result.run.services
    )
    assert "cycle" in result.run.warnings[0].lower()


def test_unselected_prerequisite_requires_human_review(tmp_path: Path) -> None:
    candidates = [_candidate("Inventory"), _candidate("Orders", ["Inventory"])]
    repository = _repository(tmp_path, candidates, ["Orders"])
    def callback(root: Path, service: str) -> MigrationRunResult:
        pytest.fail("blocked service must not execute")

    result = _orchestrator(callback).run(repository, services=["Orders"])
    assert result.run.status is MultiServiceMigrationStatus.HUMAN_REVIEW
    assert result.run.services[0].status is ServiceMigrationStateStatus.HUMAN_REVIEW


def test_multi_run_artifact_is_bounded_and_worktrees_are_distinct(tmp_path: Path) -> None:
    candidates = [_candidate("Inventory"), _candidate("Notifications")]
    repository = _repository(tmp_path, candidates, [item.name for item in candidates])
    result = _orchestrator(
        lambda root, service: _workflow_result(root, service, MigrationRunStatus.COMPLETED)
    ).run(repository, all_approved=True, service_workers=2)
    paths = [state.worktree_path for state in result.run.services]
    assert len(paths) == len(set(paths))
    payload = result.run.model_dump_json()
    assert "prompt" not in payload.lower()
    assert "api_key" not in payload.lower()
    assert (repository / ".migrationswarm" / "multi-runs").exists()


def test_multi_run_repository_round_trip(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "repo", [_candidate("Inventory")], ["Inventory"])
    result = _orchestrator(
        lambda root, service: _workflow_result(root, service, MigrationRunStatus.COMPLETED)
    ).run(repository, all_approved=True)
    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    database.create_all_for_tests()
    runs = MultiMigrationRunRepository(database.session_factory)
    runs.create(result.run)
    loaded = runs.get(result.run.run_id)
    assert loaded is not None
    assert loaded.selected_services == ["Inventory"]
    assert loaded.services[0].status is ServiceMigrationStateStatus.COMPLETED
    database.dispose()


def test_service_dependency_cycle_exception_is_exported() -> None:
    assert issubclass(ServiceDependencyCycleError, ValueError)
    assert UUID(str(uuid4()))
