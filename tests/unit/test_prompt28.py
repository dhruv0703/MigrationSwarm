"""Hard-boundary evaluation tests for Prompt 28."""

import json
import shutil
from pathlib import Path

import pytest

from migrationswarm.agents.debug import DebugAgent
from migrationswarm.agents.dependency_analysis import DependencyAnalysisAgent, JavaDependencyGraph
from migrationswarm.agents.service_boundary import ServiceBoundaryReport
from migrationswarm.agents.service_extraction import ServiceExtractionAgent
from migrationswarm.core.orchestrator import (
    MigrationOrchestrator,
    MigrationRunEventType,
    MigrationRunStatus,
)
from migrationswarm.demo import OfflineRouter, _load_fixture_definition
from migrationswarm.evaluation.behavior import compare_behavior_contracts
from migrationswarm.evaluation.dependency import (
    analyze_service_dependencies,
    continuation_status,
    dependency_order,
    strongly_connected_components,
)
from migrationswarm.evaluation.matching import boundary_metrics
from migrationswarm.evaluation.metadata import load_fixture_metadata
from migrationswarm.evaluation.models import BehaviorCheckSpec
from migrationswarm.evaluation.ownership import analyze_entity_ownership
from tests.unit.test_orchestrator import (
    debug_proposal_json,
    patch_build_sequence,
)
from tests.unit.test_orchestrator import (
    migration_repository as migration_repository_fixture,
)
from tests.unit.test_service_extraction import FakeRouter, proposal_json

ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="Git is unavailable")


@pytest.fixture
def prompt28_repository(tmp_path: Path) -> Path:
    """Reuse the real managed-worktree fixture used by orchestration tests."""
    return Path(migration_repository_fixture.__wrapped__(tmp_path))  # type: ignore[attr-defined]


def _fulfillment_evidence() -> tuple[ServiceBoundaryReport, JavaDependencyGraph]:
    root = ROOT / "examples" / "demo-fulfillment-monolith"
    graph = DependencyAnalysisAgent().analyze(root)
    fixture = _load_fixture_definition(root)
    payload = OfflineRouter(root, fixture, graph)._boundary_payload()
    payload.update(model_provider="offline", model_name="offline")
    return ServiceBoundaryReport.model_validate(payload), graph


def test_commerce_fourth_service_is_a_real_boundary() -> None:
    fixture = ROOT / "examples" / "demo-commerce-monolith"
    metadata = load_fixture_metadata(fixture)
    predicted = ["Customers", "Inventory", "Notifications", "Orders"]
    before = boundary_metrics(["Inventory", "Notifications", "Orders"], predicted)
    after = boundary_metrics(metadata.expected_services, predicted, metadata.aliases)

    assert "Customers" in metadata.expected_services
    assert before.precision == 0.75
    assert before.recall == 1.0
    assert before.f1 == 0.8571428571428571
    assert after.precision == after.recall == after.f1 == 1.0


def test_scc_analysis_covers_acyclic_cycles_self_and_disconnected_graphs() -> None:
    assert strongly_connected_components(["A", "B"], [("A", "B")]) == [("A",), ("B",)]
    assert strongly_connected_components(["A", "B"], [("A", "B"), ("B", "A")]) == [
        ("A", "B")
    ]
    assert strongly_connected_components(
        ["A", "B", "C", "D", "E"],
        [("A", "A"), ("B", "C"), ("C", "B")],
    ) == [("A",), ("B", "C"), ("D",), ("E",)]


def test_dependency_order_is_prerequisite_first_and_deterministic() -> None:
    edges = [("E", "C"), ("E", "D"), ("D", "A"), ("D", "B"), ("C", "A")]
    assert dependency_order(["A", "B", "C", "D", "E"], edges) == [
        "A",
        "B",
        "C",
        "D",
        "E",
    ]
    assert dependency_order(["B", "A"], []) == ["A", "B"]


def test_fulfillment_records_cycle_foreign_repository_and_transaction() -> None:
    boundary, graph = _fulfillment_evidence()
    analysis = analyze_service_dependencies(boundary, graph)

    assert analysis.ordering.cyclic_components == [["Billing", "Inventory", "Orders"]]
    assert analysis.ordering.recommended_order == ["Notifications"]
    assert "Shipping" in analysis.ordering.blocked_services
    assert len(analysis.foreign_repository_dependencies) >= 3
    assert analysis.cross_domain_transaction_count == 1
    categories = {finding.category for finding in analysis.review_findings}
    assert {
        "circular_dependency",
        "foreign_repository_dependency",
        "cross_domain_transaction",
    } <= categories


def test_entity_references_do_not_become_duplicate_ownership() -> None:
    boundary, graph = _fulfillment_evidence()
    report = analyze_entity_ownership(boundary, graph)

    assert report.entity_reference_count >= 1
    assert report.duplicate_mutable_entity_count == 0
    assert report.ambiguous_entity_ownership_count == 0


def test_safe_continuation_policy_is_explicit() -> None:
    assert continuation_status("shared_stateless_utility") == "safe"
    assert continuation_status("read_only_dto") == "safe"
    assert continuation_status("cross_service_contract") == "safe"
    assert continuation_status("foreign_repository_dependency") == "review_required"
    assert continuation_status("cross_domain_transaction") == "review_required"
    assert continuation_status("ownership_conflict") == "blocked"


def test_behavior_comparison_detects_mutated_generated_contract(tmp_path: Path) -> None:
    source = tmp_path / "source"
    generated = tmp_path / "generated"
    source.mkdir()
    generated.mkdir()
    (source / "OrderService.java").write_text(
        "class OrderService { void placeOrder() {} }\n", encoding="utf-8"
    )
    (generated / "OrderServiceTest.java").write_text(
        "// behavior-marker: behavior:place_order\n", encoding="utf-8"
    )
    check = BehaviorCheckSpec(
        name="place_order",
        service="Orders",
        source_markers=["placeOrder("],
        generated_markers=["behavior:place_order"],
    )

    passed = compare_behavior_contracts(source, generated, [check])
    assert passed.passed == 1
    (generated / "OrderServiceTest.java").write_text("class Mutated {}\n", encoding="utf-8")
    failed = compare_behavior_contracts(source, generated, [check])
    assert failed.failed == 1


def test_fulfillment_metadata_is_typed_and_realistic() -> None:
    metadata = load_fixture_metadata(ROOT / "examples" / "demo-fulfillment-monolith")
    assert metadata.expected_services == [
        "Orders",
        "Inventory",
        "Shipping",
        "Billing",
        "Notifications",
    ]
    assert len(metadata.behavior_checks) == 5
    assert "OrderService" in json.dumps(metadata.model_dump(mode="json"))


def test_controlled_repairable_defect_uses_real_bounded_path(
    prompt28_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_build_sequence(
        monkeypatch,
        [
            (1, "COMPILATION ERROR: cannot find symbol\n"),
            (0, "Tests run: 2, Failures: 0, Errors: 0, Skipped: 0\n"),
        ],
    )
    result = MigrationOrchestrator(
        extraction_agent=ServiceExtractionAgent(FakeRouter([proposal_json()])),
        debug_agent=DebugAgent(FakeRouter([debug_proposal_json()])),
    ).run(prompt28_repository, "Greeting", max_debug_attempts=2)

    assert result.run.status is MigrationRunStatus.COMPLETED
    assert result.run.debug_attempts == 1
    assert any(
        event.event_type is MigrationRunEventType.REVERIFY_PASSED
        for event in result.run.events
    )


def test_controlled_unrecoverable_defect_is_bounded(
    prompt28_repository: Path,
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
    result = MigrationOrchestrator(
        extraction_agent=ServiceExtractionAgent(FakeRouter([proposal_json()])),
        debug_agent=DebugAgent(
            FakeRouter([debug_proposal_json(), debug_proposal_json()])
        ),
    ).run(prompt28_repository, "Greeting", max_debug_attempts=2)

    assert result.run.status is MigrationRunStatus.HUMAN_REVIEW
    assert result.run.debug_attempts == 2
