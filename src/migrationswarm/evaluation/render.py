"""Stable JSON and Markdown benchmark rendering."""

import json
from pathlib import Path
from typing import Any

from migrationswarm.evaluation.models import BenchmarkRun, FixtureResult


def write_json(path: Path, model: BenchmarkRun | FixtureResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(model.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _value(value: Any) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def render_markdown(run: BenchmarkRun) -> str:
    """Render a readable summary without changing structured values."""
    lines = [
        "# MigrationSwarm Benchmark",
        "",
        f"- Run: `{run.run_id}`",
        f"- Mode: `{run.mode}`",
        f"- Baseline only: `{run.baseline_only}`",
        "- Boundary averages: macro averages across fixture results with defined metrics.",
        "- Extraction rates use the explicit stage-attempt denominator recorded in JSON.",
        "",
        "## Fixture results",
        "",
        "| Fixture | Status | Semantic | Review | Expected services | Predicted services | "
        "Matched services | Precision | "
        "Recall | F1 | Extract | Build | Tests | Verification | Repair attempts | "
        "Baseline F1 | Cycles | Foreign repos | Transactions | Review findings | "
        "Behavior passed/failed | Elapsed ms | Model calls |",
        "|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for result in run.fixtures:
        metrics = result.extraction
        repair_attempts = sum(item.repair_attempts for item in result.services)
        lines.append(
            "| "
            + " | ".join(
                [
                    result.fixture,
                    result.status,
                    result.semantic_status,
                    result.review_status,
                    "; ".join(result.expected_services) or "N/A",
                    "; ".join(result.predicted_services) or "N/A",
                    "; ".join(result.boundary.matched_services) or "N/A",
                    _value(result.boundary.precision),
                    _value(result.boundary.recall),
                    _value(result.boundary.f1),
                    _value(metrics.extraction_completion_rate),
                    _value(metrics.build_success_rate),
                    _value(metrics.test_success_rate),
                    _value(metrics.verification_success_rate),
                    str(repair_attempts),
                    _value(result.baseline.boundary.f1),
                    str(len(result.migration_ordering.cyclic_components)),
                    str(result.ownership.foreign_repository_dependency_count),
                    str(result.ownership.cross_domain_transaction_count),
                    str(len(result.review_findings)),
                    f"{result.behavior_preservation.passed}/{result.behavior_preservation.failed}",
                    _value(result.elapsed_ms),
                    str(result.model_calls),
                ]
            )
            + " |"
        )
    aggregate = run.aggregate
    lines.extend(
        [
            "",
            "## Aggregate results",
            "",
            f"- Fixtures: {aggregate.fixture_count}; completed: "
            f"{aggregate.completed_fixture_count}",
            f"- Macro precision: {_value(aggregate.macro_boundary_precision)}",
            f"- Macro recall: {_value(aggregate.macro_boundary_recall)}",
            f"- Macro F1: {_value(aggregate.macro_boundary_f1)}",
            f"- Extracted services: {aggregate.total_extracted_services}",
            f"- Build passes: {aggregate.total_build_passes}",
            f"- Test passes: {aggregate.total_test_passes}",
            f"- Verification passes: {aggregate.total_verification_passes}",
            f"- Repair attempts: {aggregate.total_repair_attempts}",
            f"- Model calls: {aggregate.total_model_calls}",
            f"- Cross-service dependencies: {aggregate.total_cross_service_dependencies}",
            f"- Unresolved dependencies: {aggregate.total_unresolved_dependencies}",
            f"- Ownership violations: {aggregate.total_ownership_violations}",
            f"- Cyclic dependency components: {aggregate.total_cyclic_dependency_components}",
            f"- Foreign repository dependencies: {aggregate.total_foreign_repository_dependencies}",
            f"- Cross-domain transactions: {aggregate.total_cross_domain_transactions}",
            f"- Human-review findings: {aggregate.total_human_review_findings}",
            f"- Blocked services: {aggregate.total_blocked_services}",
            f"- First-pass failures: {aggregate.total_first_pass_failures}",
            f"- Post-repair successes: {aggregate.total_post_repair_successes}",
            f"- Behavior checks passed: {aggregate.total_behavior_checks_passed}",
            f"- Behavior checks failed: {aggregate.total_behavior_checks_failed}",
            f"- Semantic contracts defined/executed/passed/failed/unavailable: "
            f"{aggregate.total_semantic_contracts_defined}/"
            f"{aggregate.total_semantic_contracts_executed}/"
            f"{aggregate.total_semantic_contracts_passed}/"
            f"{aggregate.total_semantic_contracts_failed}/"
            f"{aggregate.total_semantic_contracts_unavailable}",
            f"- Semantic fixture statuses (complete/partial/failed): "
            f"{aggregate.semantic_complete_fixture_count}/"
            f"{aggregate.semantic_partial_fixture_count}/"
            f"{aggregate.semantic_failed_fixture_count}",
            "",
        ]
    )
    return "\n".join(lines)


def write_markdown(path: Path, run: BenchmarkRun) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(run), encoding="utf-8")
