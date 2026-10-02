"""Offline benchmark runner that observes existing MigrationSwarm behavior."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal, cast

from migrationswarm.agents.dependency_analysis import JavaDependencyGraph
from migrationswarm.agents.service_boundary import ServiceBoundaryReport
from migrationswarm.demo import DemoError, run_demo
from migrationswarm.evaluation.baseline import discover_package_baseline
from migrationswarm.evaluation.behavior import evaluate_behavior_contracts
from migrationswarm.evaluation.dependency import analyze_service_dependencies
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
    FixtureMetadata,
    FixtureResult,
    HumanReviewFinding,
    OwnershipReport,
    VerificationResult,
)
from migrationswarm.evaluation.ownership import (
    analyze_boundary_ownership,
    analyze_entity_ownership,
    inspect_generated_services,
)
from migrationswarm.evaluation.render import write_json, write_markdown


class BenchmarkError(RuntimeError):
    """Raised for invalid benchmark selection or output paths."""


def discover_fixtures(examples_root: str | Path) -> list[Path]:
    """Find directories with explicit benchmark metadata."""
    root = Path(examples_root).expanduser().resolve()
    return sorted(
        (path for path in root.iterdir() if path.is_dir() and (path / "benchmark.json").is_file()),
        key=lambda path: path.name.casefold(),
    )


def _run_maven_test(root: Path) -> tuple[bool, str | None]:
    maven = shutil.which("mvn")
    if maven is None:
        return False, "Maven is not installed"
    command = [maven, "test", "-q"]
    if maven.lower().endswith((".cmd", ".bat")):
        command = ["cmd.exe", "/d", "/s", "/c", *command]
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, type(error).__name__
    return completed.returncode == 0, None if completed.returncode == 0 else "Maven test failed"


def _service_results(
    expected: list[str], predicted: list[str], *, completed: bool, repair_attempts: int = 0
) -> list[ExtractionResult]:
    metrics = boundary_metrics(expected, predicted)
    matched = set(metrics.matched_services)
    results: list[ExtractionResult] = []
    for name in expected:
        discovered = name in matched
        results.append(
            ExtractionResult(
                service_name=name,
                boundary_discovered=discovered,
                extraction_attempted=discovered if completed else None,
                extraction_completed=discovered if completed else None,
                build_attempted=discovered if completed else None,
                build_passed=discovered if completed else None,
                tests_attempted=discovered if completed else None,
                tests_passed=discovered if completed else None,
                verification_attempted=discovered if completed else None,
                verification_passed=discovered if completed else None,
                verification=VerificationResult(
                    attempted=discovered if completed else False,
                    passed=discovered if completed else None,
                    build_attempted=discovered if completed else False,
                    build_passed=discovered if completed else None,
                    tests_attempted=discovered if completed else False,
                    tests_passed=discovered if completed else None,
                ),
                repair_required=repair_attempts > 0 and discovered,
                repair_attempts=repair_attempts if discovered else 0,
                repair_succeeded=True if repair_attempts > 0 and discovered else None,
                final_status="completed" if completed and discovered else "not_run",
                technical_success=completed and discovered,
            )
        )
    return results


def _copy_ignore(_directory: str, names: list[str]) -> set[str]:
    """Exclude generated state and build output from an isolated fixture copy."""
    return {
        name
        for name in names
        if name in {".git", ".migrationswarm", "target", "build", ".gradle"}
    }


def _latest_multi_run(root: Path) -> dict[str, Any] | None:
    """Load the newest multi-service result without exposing raw logs."""
    reports = sorted(
        (root / ".migrationswarm" / "multi-runs").glob("*.json"),
        key=lambda item: (item.stat().st_mtime, item.name),
    )
    for path in reversed(reports):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("services"), list):
            return payload
    return None


def _demo_service_results(
    metadata: FixtureMetadata,
    predicted: list[str],
    root: Path,
    generated_directories: list[str],
) -> list[ExtractionResult]:
    """Translate the real multi-service result into typed evaluation records."""
    matches, _, _ = match_services(
        metadata.expected_services,
        predicted,
        metadata.aliases,
    )
    predicted_by_expected = {item.expected: item.predicted for item in matches}
    run_payload = _latest_multi_run(root)
    service_payloads = {
        normalize_service_name(str(item.get("service_name", ""))): item
        for item in (run_payload or {}).get("services", [])
        if isinstance(item, dict)
    }
    generated_names = {
        normalize_service_name(Path(directory).name.removesuffix("-service"))
        for directory in generated_directories
    }
    results: list[ExtractionResult] = []
    for expected in metadata.expected_services:
        predicted_name = predicted_by_expected.get(expected)
        payload = service_payloads.get(normalize_service_name(predicted_name or ""))
        if payload is None:
            results.append(
                ExtractionResult(
                    service_name=expected,
                    boundary_discovered=predicted_name is not None,
                    final_status="not_run",
                )
            )
            continue

        status = str(payload.get("status", "unknown"))
        verification_payload = payload.get("verification_result")
        verification = (
            VerificationResult(
                attempted=True,
                passed=verification_payload.get("status") == "passed",
                build_attempted=True,
                build_passed=verification_payload.get("status") == "passed",
                tests_attempted=True,
                tests_passed=verification_payload.get("status") == "passed",
            )
            if isinstance(verification_payload, dict)
            else None
        )
        service_slug = str(payload.get("service_slug", ""))
        extraction_completed = (
            normalize_service_name(service_slug) in generated_names
            if service_slug
            else None
        )
        build_attempted = verification is not None or "build" in str(
            payload.get("failure_reason", "")
        ).casefold()
        results.append(
            ExtractionResult(
                service_name=expected,
                boundary_discovered=predicted_name is not None,
                extraction_attempted=status != "blocked",
                extraction_completed=extraction_completed,
                build_attempted=build_attempted,
                build_passed=verification.build_passed if verification else (
                    False if build_attempted else None
                ),
                tests_attempted=verification.tests_attempted if verification else False,
                tests_passed=verification.tests_passed if verification else None,
                verification_attempted=verification.attempted if verification else False,
                verification_passed=verification.passed if verification else None,
                verification=verification,
                repair_required=int(payload.get("repair_attempts", 0)) > 0,
                repair_attempts=int(payload.get("repair_attempts", 0)),
                repair_succeeded=(
                    status == "completed" and int(payload.get("repair_attempts", 0)) > 0
                )
                if int(payload.get("repair_attempts", 0)) > 0
                else None,
                final_status=status,
                technical_success=status == "completed",
                review_status=(
                    "review_required"
                    if status in {"human_review", "blocked"}
                    else "not_required"
                ),
                human_review_required=status in {"human_review", "blocked"},
            )
        )
    return results


def _fixture_result(
    fixture: Path,
    metadata: Any,
    baseline: Any,
    *,
    predicted: list[str],
    completed: bool,
    model_calls: int = 0,
    failure: str | None = None,
    elapsed_ms: float | None = None,
    repair_attempts: int = 0,
    ownership: OwnershipReport | None = None,
    status: str | None = None,
) -> FixtureResult:
    boundary = boundary_metrics(metadata.expected_services, predicted, metadata.aliases)
    services = _service_results(
        metadata.expected_services,
        predicted,
        completed=completed,
        repair_attempts=repair_attempts,
    )
    return FixtureResult(
        fixture=fixture.name,
        status=cast(
            Literal["completed", "failed", "unavailable", "review_required"],
            status
            if status in {"completed", "failed", "unavailable", "review_required"}
            else ("completed" if completed else "failed"),
        ),
        expected_services=metadata.expected_services,
        predicted_services=predicted,
        boundary=boundary,
        services=services,
        extraction=extraction_metrics(services),
        baseline=baseline,
        ownership=ownership or OwnershipReport(),
        model_calls=model_calls,
        elapsed_ms=elapsed_ms,
        failure=failure,
        technical_extraction_success=completed,
    )


def _ownership_review_findings(ownership: OwnershipReport) -> list[HumanReviewFinding]:
    """Translate compact ownership strings into typed review findings."""
    findings: list[HumanReviewFinding] = []
    for finding in ownership.findings:
        blocked = "multiple" in finding.casefold() or "duplicate" in finding.casefold()
        findings.append(
            HumanReviewFinding(
                category="ownership_conflict" if blocked else "ownership_review",
                source_service="ownership-analysis",
                affected_target="generated service output",
                evidence=[finding],
                impact="blocked" if blocked else "review_required",
                can_continue_safely=not blocked,
                recommended_review_focus=(
                    "Assign one persistence owner and keep other references read-only."
                    if blocked
                    else "Confirm that generated files stay within the selected boundary."
                ),
            )
        )
    return findings


def _evaluate_fixture(fixture: Path, *, baseline_only: bool, live: bool) -> FixtureResult:
    started = time.monotonic()
    live_mode = live is True
    metadata = load_fixture_metadata(fixture)
    build_passed, build_failure = _run_maven_test(fixture)
    baseline = discover_package_baseline(fixture, metadata, build_passed=build_passed)
    if baseline_only:
        return _fixture_result(
            fixture,
            metadata,
            baseline,
            predicted=baseline.predicted_services,
            completed=False,
            failure="System evaluation skipped by --baseline",
            elapsed_ms=(time.monotonic() - started) * 1000,
        ).model_copy(update={"status": "unavailable"})

    try:
        with tempfile.TemporaryDirectory(prefix="migrationswarm-benchmark-") as directory:
            isolated_fixture = Path(directory) / fixture.name
            shutil.copytree(fixture, isolated_fixture, ignore=_copy_ignore)
            result = run_demo(isolated_fixture, live=live_mode)
            boundary_path = isolated_fixture / ".migrationswarm" / "service-boundaries.json"
            payload = json.loads(boundary_path.read_text(encoding="utf-8"))
            generated = result.generated_service_directories
            predicted = [item["name"] for item in payload.get("candidate_services", [])]
            services = _demo_service_results(
                metadata,
                predicted,
                isolated_fixture,
                generated,
            )
            run_payload = _latest_multi_run(isolated_fixture)
            boundary_report = ServiceBoundaryReport.model_validate(payload)
            graph_payload = json.loads(
                (isolated_fixture / ".migrationswarm" / "java-dependency-graph.json").read_text(
                    encoding="utf-8"
                )
            )
            dependency_graph = JavaDependencyGraph.model_validate(graph_payload)
            dependency_analysis = analyze_service_dependencies(
                boundary_report, dependency_graph
            )
            boundary_ownership = analyze_boundary_ownership(
                boundary_report,
                metadata.expected_services,
            )
            entity_ownership = analyze_entity_ownership(boundary_report, dependency_graph)
            generated_ownership = inspect_generated_services(
                isolated_fixture,
                source_package_root=metadata.package_root,
            )
            ownership = boundary_ownership.model_copy(
                update={
                    "ownership_violation_count": (
                        boundary_ownership.ownership_violation_count
                        + entity_ownership.ownership_violation_count
                        + generated_ownership.ownership_violation_count
                    ),
                    "foreign_repository_dependency_count": len(
                        dependency_analysis.foreign_repository_dependencies
                    ),
                    "cross_domain_transaction_count": (
                        dependency_analysis.cross_domain_transaction_count
                    ),
                    "entity_reference_count": entity_ownership.entity_reference_count,
                    "shared_value_object_count": entity_ownership.shared_value_object_count,
                    "ambiguous_entity_ownership_count": (
                        entity_ownership.ambiguous_entity_ownership_count
                    ),
                    "duplicate_mutable_entity_count": (
                        entity_ownership.duplicate_mutable_entity_count
                    ),
                    "findings": [
                        *boundary_ownership.findings,
                        *entity_ownership.findings,
                        *generated_ownership.findings,
                    ],
                }
            )
            behavior = evaluate_behavior_contracts(
                isolated_fixture,
                isolated_fixture / ".migrationswarm" / "demo-services",
                metadata.behavior_checks,
            )
            semantic_by_service = {
                item.service: item.status for item in behavior.checks
            }
            services = [
                item.model_copy(
                    update={
                        "semantic_status": (
                            "complete"
                            if semantic_by_service.get(item.service_name) == "passed"
                            else "failed"
                            if semantic_by_service.get(item.service_name) == "failed"
                            else "partial"
                            if semantic_by_service.get(item.service_name) == "partial"
                            else "not_evaluated"
                        )
                    }
                )
                for item in services
            ]
            statuses = [
                str(item.get("status"))
                for item in (run_payload or {}).get("services", [])
                if isinstance(item, dict)
            ]
        completed = (
            result.status == "completed"
            and build_passed
            and len(generated) >= len(metadata.expected_services)
            and bool(statuses)
            and all(status == "completed" for status in statuses)
        )
        ownership_findings = _ownership_review_findings(ownership)
        review_findings = [*dependency_analysis.review_findings, *ownership_findings]
        review_status = (
            "blocked"
            if any(item.impact == "blocked" for item in review_findings)
            else "review_required"
            if review_findings or result.status != "completed"
            else "not_required"
        )
        fixture_status = cast(
            Literal["completed", "failed", "unavailable", "review_required"],
            "completed"
            if completed
            else "review_required"
            if review_status != "not_required"
            else "failed"
        )
        typed_review_status = cast(
            Literal["not_required", "review_required", "blocked"], review_status
        )
        boundary = boundary_metrics(metadata.expected_services, predicted, metadata.aliases)
        return FixtureResult(
            fixture=fixture.name,
            status=fixture_status,
            expected_services=metadata.expected_services,
            predicted_services=predicted,
            boundary=boundary,
            services=services,
            extraction=extraction_metrics(services),
            baseline=baseline,
            ownership=ownership,
            migration_ordering=dependency_analysis.ordering,
            review_findings=review_findings,
            review_status=typed_review_status,
            technical_extraction_success=completed,
            behavior_preservation=behavior,
            semantic_status=behavior.semantic_status,
            model_calls=result.model_calls,
            failure=None if completed else (
                "Human review is required before safe extraction."
                if review_status != "not_required"
                else "Fixture extraction did not complete all services or baseline failed."
            ),
            elapsed_ms=(time.monotonic() - started) * 1000,
        )
    except (DemoError, OSError, ValueError) as error:
        return _fixture_result(
            fixture,
            metadata,
            baseline,
            predicted=[],
            completed=False,
            failure=str(error) if build_failure is None else f"{error}; {build_failure}",
            elapsed_ms=(time.monotonic() - started) * 1000,
        )


def run_benchmark(
    fixtures: Sequence[str | Path],
    output_root: str | Path,
    *,
    baseline_only: bool = False,
    compare_baseline: bool = False,
    live: bool = False,
) -> tuple[BenchmarkRun, Path]:
    """Evaluate fixtures independently and write a run directory."""
    started = time.monotonic()
    live_mode = live is True
    results: list[FixtureResult] = []
    for fixture_value in fixtures:
        fixture = Path(fixture_value).expanduser().resolve()
        try:
            results.append(
                _evaluate_fixture(fixture, baseline_only=baseline_only, live=live_mode)
            )
        except Exception as error:  # noqa: BLE001 - isolate one broken fixture
            try:
                metadata = load_fixture_metadata(fixture)
                empty_baseline = discover_package_baseline(fixture, metadata)
                results.append(
                    _fixture_result(
                        fixture,
                        metadata,
                        empty_baseline,
                        predicted=[],
                        completed=False,
                        failure=f"{type(error).__name__}: {error}",
                    ).model_copy(update={"status": "unavailable"})
                )
            except Exception as metadata_error:  # noqa: BLE001 - isolate bad metadata too
                empty_boundary = boundary_metrics([], [])
                empty_baseline = BaselineResult(
                    boundary=empty_boundary,
                    failure=f"{type(metadata_error).__name__}: {metadata_error}",
                )
                results.append(
                    FixtureResult(
                        fixture=fixture.name,
                        status="unavailable",
                        boundary=empty_boundary,
                        extraction=extraction_metrics([]),
                        baseline=empty_baseline,
                        failure=(
                            f"{type(error).__name__}: {error}; "
                            f"metadata: {type(metadata_error).__name__}: {metadata_error}"
                        ),
                    )
                )
    run = BenchmarkRun(
        mode="live" if live_mode else "offline",
        baseline_only=baseline_only,
        compare_baseline=compare_baseline,
        fixtures=results,
        aggregate=aggregate_metrics(results),
        elapsed_ms=(time.monotonic() - started) * 1000,
    )
    directory = Path(output_root).expanduser().resolve() / str(run.run_id)
    write_json(directory / "summary.json", run)
    write_markdown(directory / "summary.md", run)
    for result in results:
        write_json(directory / f"{result.fixture}.json", result)
    return run, directory
