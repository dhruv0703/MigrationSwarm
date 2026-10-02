"""Unit tests for deterministic benchmark evaluation contracts."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import migrationswarm.cli.main as cli_main
from migrationswarm.agents.service_boundary import CandidateService, ServiceBoundaryReport
from migrationswarm.cli.main import app
from migrationswarm.evaluation.baseline import discover_package_baseline
from migrationswarm.evaluation.matching import (
    boundary_metrics,
    match_services,
    normalize_service_name,
)
from migrationswarm.evaluation.metadata import load_fixture_metadata
from migrationswarm.evaluation.metrics import aggregate_metrics, extraction_metrics
from migrationswarm.evaluation.models import (
    BaselineResult,
    BenchmarkRun,
    ExtractionResult,
    FixtureResult,
    ServiceBoundaryResult,
    ServiceEvaluation,
)
from migrationswarm.evaluation.ownership import (
    analyze_boundary_ownership,
    inspect_generated_services,
)
from migrationswarm.evaluation.render import render_markdown

ROOT = Path(__file__).resolve().parents[2]
RUNNER = CliRunner()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Order Service", "order"),
        ("order-service", "order"),
        ("order_service", "order"),
        ("Orders", "order"),
        ("Analysis", "analysis"),
    ],
)
def test_service_normalization_is_predictable(value: str, expected: str) -> None:
    assert normalize_service_name(value) == expected


def test_normalization_does_not_match_unrelated_compounds() -> None:
    assert normalize_service_name("Order Service") != normalize_service_name("Order Notification")
    assert normalize_service_name("User") != normalize_service_name("UserProfile")


def test_alias_matching_is_one_to_one() -> None:
    matches, missed, unexpected = match_services(
        ["Orders", "Payments"],
        ["OrderService", "Payment", "Order"],
        {"Orders": ["OrderService"]},
    )

    assert [(item.expected, item.predicted) for item in matches] == [
        ("Orders", "OrderService"),
        ("Payments", "Payment"),
    ]
    assert missed == []
    assert unexpected == ["Order"]


def test_boundary_metrics_use_standard_formulas() -> None:
    result = boundary_metrics(["Orders", "Payments"], ["Order Service", "Users"])

    assert result.matched_service_count == 1
    assert result.precision == pytest.approx(0.5)
    assert result.recall == pytest.approx(0.5)
    assert result.f1 == pytest.approx(0.5)


def test_boundary_metrics_keep_undefined_values_explicit() -> None:
    no_predictions = boundary_metrics(["Orders"], [])
    no_expectations = boundary_metrics([], ["Orders"])
    both_empty = boundary_metrics([], [])

    assert no_predictions.precision is None
    assert no_predictions.recall == 0.0
    assert no_predictions.f1 is None
    assert no_expectations.precision == 0.0
    assert no_expectations.recall is None
    assert no_expectations.f1 is None
    assert both_empty.precision is None
    assert both_empty.recall is None
    assert both_empty.f1 is None


def test_extraction_rates_document_their_denominators() -> None:
    services = [
        ServiceEvaluation(
            service_name="Orders",
            boundary_discovered=True,
            extraction_attempted=True,
            extraction_completed=True,
            build_attempted=True,
            build_passed=True,
            tests_attempted=True,
            tests_passed=True,
            verification_attempted=True,
            verification_passed=True,
            final_status="completed",
        ),
        ServiceEvaluation(
            service_name="Payments",
            boundary_discovered=True,
            extraction_attempted=True,
            extraction_completed=False,
            build_attempted=False,
            tests_attempted=False,
            verification_attempted=False,
            repair_required=True,
            repair_attempts=1,
            repair_succeeded=False,
            final_status="failed",
        ),
    ]
    result = extraction_metrics(services)

    assert result.extraction_denominator == 2
    assert result.build_denominator == 1
    assert result.test_denominator == 1
    assert result.verification_denominator == 1
    assert result.repair_denominator == 1
    assert result.extraction_completion_rate == pytest.approx(0.5)
    assert result.build_success_rate == 1.0
    assert result.repair_success_rate == 0.0


def test_fixture_metadata_and_baseline_are_machine_readable() -> None:
    fixture = ROOT / "examples" / "demo-booking-monolith"
    metadata = load_fixture_metadata(fixture)
    baseline = discover_package_baseline(fixture, metadata)

    assert metadata.expected_services == ["Reservations", "Payments", "Notifications"]
    assert baseline.predicted_services == ["Notifications", "Payments", "Reservations"]
    assert baseline.boundary.matched_service_count == 3


def test_ownership_check_detects_duplicate_repositories() -> None:
    boundary = ServiceBoundaryReport(
        candidate_services=[
            CandidateService(
                name="Orders",
                description="orders",
                classes=["example.Order", "example.OrderRepository"],
                packages=["example.orders"],
                controllers=[],
                services=[],
                repositories=["example.OrderRepository"],
                confidence=0.9,
                reasoning="package",
                dependencies_on_other_candidates=[],
                risks=[],
            ),
            CandidateService(
                name="Payments",
                description="payments",
                classes=["example.Order", "example.OrderRepository"],
                packages=["example.payments"],
                controllers=[],
                services=[],
                repositories=["example.OrderRepository"],
                confidence=0.9,
                reasoning="package",
                dependencies_on_other_candidates=[],
                risks=[],
            ),
        ],
        shared_components=[],
        unresolved_classes=[],
        overall_reasoning="test",
        warnings=[],
        model_provider="test",
        model_name="test",
    )

    report = analyze_boundary_ownership(boundary, ["Orders", "Payments"])

    assert report.ownership_violation_count >= 2
    assert any("multiple candidates" in finding for finding in report.findings)


def test_generated_service_inspection_detects_absolute_source_paths(tmp_path: Path) -> None:
    generated = tmp_path / ".migrationswarm" / "demo-services" / "orders"
    generated.mkdir(parents=True)
    (generated / "Order.java").write_text(
        f"package example.orders;\n// {tmp_path}\npublic class Order {{}}\n",
        encoding="utf-8",
    )

    report = inspect_generated_services(tmp_path)

    assert report.ownership_violation_count == 1


def test_aggregate_metrics_are_macro_for_boundaries() -> None:
    boundary = boundary_metrics(["Orders"], ["Orders"])
    empty_services: list[ServiceEvaluation] = []
    extraction = extraction_metrics(empty_services)
    baseline = BaselineResult(predicted_services=["Orders"], boundary=boundary)
    results = [
        FixtureResult(
            fixture="one",
            status="completed",
            expected_services=["Orders"],
            predicted_services=["Orders"],
            boundary=boundary,
            services=empty_services,
            extraction=extraction,
            baseline=baseline,
        )
    ]

    aggregate = aggregate_metrics(results)

    assert aggregate.macro_boundary_f1 == 1.0
    assert aggregate.total_extracted_services == 0


def test_benchmark_json_and_markdown_are_serializable() -> None:
    boundary = boundary_metrics(["Orders"], ["Orders"])
    empty = extraction_metrics([])
    baseline = BaselineResult(predicted_services=["Orders"], boundary=boundary)
    result = FixtureResult(
        fixture="demo",
        status="completed",
        expected_services=["Orders"],
        predicted_services=["Orders"],
        boundary=boundary,
        extraction=empty,
        baseline=baseline,
    )
    run = BenchmarkRun(fixtures=[result], aggregate=aggregate_metrics([result]), mode="offline")

    encoded = json.dumps(run.model_dump(mode="json"), sort_keys=True)
    markdown = render_markdown(run)

    assert str(run.run_id) in encoded
    assert "N/A" in markdown
    assert "| Fixture |" in markdown


def test_evaluation_contracts_have_explicit_boundary_and_extraction_models() -> None:
    boundary = ServiceBoundaryResult(
        expected_service_count=1,
        predicted_service_count=1,
        matched_service_count=1,
        matched_services=["Orders"],
    )
    extraction = ExtractionResult(
        service_name="Orders",
        boundary_discovered=True,
        final_status="completed",
    )

    assert boundary.matched_service_count == 1
    assert extraction.service_name == "Orders"
    assert ServiceEvaluation is ExtractionResult


def test_benchmark_cli_requires_explicit_fixture_selection() -> None:
    result = RUNNER.invoke(app, ["benchmark"])

    assert result.exit_code == 2
    assert "provide a repository path" in result.output.lower()


def test_benchmark_cli_rejects_unknown_fixture() -> None:
    result = RUNNER.invoke(app, ["benchmark", "--fixture", "does-not-exist"])

    assert result.exit_code == 2
    assert "unknown benchmark fixture" in result.output.lower()


def test_benchmark_all_is_offline_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    boundary = boundary_metrics(["Orders"], ["Orders"])
    empty = extraction_metrics([])
    baseline = BaselineResult(predicted_services=["Orders"], boundary=boundary)
    fixture_result = FixtureResult(
        fixture="demo",
        status="completed",
        expected_services=["Orders"],
        predicted_services=["Orders"],
        boundary=boundary,
        extraction=empty,
        baseline=baseline,
    )
    run = BenchmarkRun(
        fixtures=[fixture_result],
        aggregate=aggregate_metrics([fixture_result]),
        mode="offline",
    )
    captured: dict[str, object] = {}

    def fake_discover(_root: Path) -> list[Path]:
        return [tmp_path]

    def fake_run(*args: object, **kwargs: object) -> tuple[BenchmarkRun, Path]:
        captured.update(kwargs)
        return run, tmp_path

    monkeypatch.setattr(cli_main, "discover_fixtures", fake_discover)
    monkeypatch.setattr(cli_main, "run_benchmark", fake_run)

    result = RUNNER.invoke(app, ["benchmark", "--all"])

    assert result.exit_code == 0
    assert captured["live"] is False


@pytest.mark.parametrize(
    ("arguments", "expected_live"),
    [
        (["benchmark", "--fixture", "demo-commerce-monolith"], False),
        (["benchmark", "--all"], False),
        (["benchmark", "--all", "--live"], True),
    ],
)
def test_benchmark_cli_provider_mode_is_explicit(
    arguments: list[str],
    expected_live: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    boundary = boundary_metrics(["Orders"], ["Orders"])
    fixture_result = FixtureResult(
        fixture="demo",
        status="completed",
        expected_services=["Orders"],
        predicted_services=["Orders"],
        boundary=boundary,
        extraction=extraction_metrics([]),
        baseline=BaselineResult(predicted_services=["Orders"], boundary=boundary),
    )
    run = BenchmarkRun(
        fixtures=[fixture_result],
        aggregate=aggregate_metrics([fixture_result]),
        mode="live" if expected_live else "offline",
    )
    provider_calls = 0
    captured: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> tuple[BenchmarkRun, Path]:
        nonlocal provider_calls
        captured.update(kwargs)
        if kwargs.get("live") is True:
            provider_calls += 1
        return run, tmp_path

    monkeypatch.setattr(cli_main, "run_benchmark", fake_run)
    result = RUNNER.invoke(app, arguments)

    assert result.exit_code == 0
    assert captured["live"] is expected_live
    assert provider_calls == (1 if expected_live else 0)


def test_benchmark_runner_defaults_to_offline_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import migrationswarm.evaluation.runner as benchmark_runner

    seen_modes: list[bool] = []

    def fake_evaluate(fixture: Path, *, baseline_only: bool, live: bool) -> FixtureResult:
        del fixture, baseline_only
        seen_modes.append(live)
        boundary = boundary_metrics(["Orders"], ["Orders"])
        return FixtureResult(
            fixture="demo",
            status="completed",
            expected_services=["Orders"],
            predicted_services=["Orders"],
            boundary=boundary,
            extraction=extraction_metrics([]),
            baseline=BaselineResult(predicted_services=["Orders"], boundary=boundary),
        )

    monkeypatch.setattr(benchmark_runner, "_evaluate_fixture", fake_evaluate)
    run, _ = benchmark_runner.run_benchmark([tmp_path], tmp_path / "artifacts")

    assert seen_modes == [False]
    assert run.mode == "offline"


def test_benchmark_isolates_missing_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import migrationswarm.evaluation.runner as benchmark_runner

    broken = tmp_path / "broken-fixture"
    broken.mkdir()

    def fail_fixture(*args: object, **kwargs: object) -> FixtureResult:
        raise RuntimeError("fixture failed")

    monkeypatch.setattr(benchmark_runner, "_evaluate_fixture", fail_fixture)

    run, _ = benchmark_runner.run_benchmark([broken], tmp_path / "artifacts")

    assert run.fixtures[0].status == "unavailable"
    assert "metadata" in (run.fixtures[0].failure or "")


def test_fixture_metadata_rejects_directory_name_mismatch(tmp_path: Path) -> None:
    (tmp_path / "benchmark.json").write_text(
        json.dumps({
            "name": "different",
            "expected_services": ["Orders"],
            "package_root": "com.example",
        }),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="does not match"):
        load_fixture_metadata(tmp_path)
