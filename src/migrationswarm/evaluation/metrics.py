"""Lifecycle and aggregate benchmark calculations."""

from statistics import mean

from migrationswarm.evaluation.models import (
    AggregateMetrics,
    ExtractionMetrics,
    FixtureResult,
    ServiceEvaluation,
)


def _rate(successes: int, denominator: int) -> float | None:
    return successes / denominator if denominator else None


def extraction_metrics(services: list[ServiceEvaluation]) -> ExtractionMetrics:
    """Calculate rates using only services for which each stage was attempted."""
    extraction = [item for item in services if item.extraction_attempted is True]
    builds = [item for item in services if item.build_attempted is True]
    tests = [item for item in services if item.tests_attempted is True]
    verification = [item for item in services if item.verification_attempted is True]
    repaired = [item for item in services if item.repair_required]
    first_pass = [item for item in extraction if item.repair_attempts == 0]
    return ExtractionMetrics(
        extraction_completion_rate=_rate(
            sum(item.extraction_completed is True for item in extraction), len(extraction)
        ),
        build_success_rate=_rate(sum(item.build_passed is True for item in builds), len(builds)),
        test_success_rate=_rate(sum(item.tests_passed is True for item in tests), len(tests)),
        verification_success_rate=_rate(
            sum(item.verification_passed is True for item in verification), len(verification)
        ),
        first_pass_success_rate=_rate(
            sum(
                item.extraction_completed is True
                and item.build_passed is True
                and item.tests_passed is True
                and item.verification_passed is True
                for item in first_pass
            ),
            len(first_pass),
        ),
        repair_success_rate=_rate(
            sum(item.repair_succeeded is True for item in repaired), len(repaired)
        ),
        extraction_denominator=len(extraction),
        build_denominator=len(builds),
        test_denominator=len(tests),
        verification_denominator=len(verification),
        first_pass_denominator=len(first_pass),
        repair_denominator=len(repaired),
        first_pass_failure_count=sum(
            item.extraction_completed is not True
            or item.build_passed is not True
            or item.tests_passed is not True
            or item.verification_passed is not True
            for item in first_pass
        ),
        post_repair_success_count=sum(
            item.repair_succeeded is True for item in repaired
        ),
    )


def aggregate_metrics(results: list[FixtureResult]) -> AggregateMetrics:
    """Aggregate with macro boundary averages and service-level totals."""
    completed = [item for item in results if item.status == "completed"]
    precision = [item.boundary.precision for item in results if item.boundary.precision is not None]
    recall = [item.boundary.recall for item in results if item.boundary.recall is not None]
    f1 = [item.boundary.f1 for item in results if item.boundary.f1 is not None]
    services = [service for item in results for service in item.services]
    return AggregateMetrics(
        fixture_count=len(results),
        completed_fixture_count=len(completed),
        macro_boundary_precision=mean(precision) if precision else None,
        macro_boundary_recall=mean(recall) if recall else None,
        macro_boundary_f1=mean(f1) if f1 else None,
        total_extracted_services=sum(item.extraction_completed is True for item in services),
        total_build_passes=sum(item.build_passed is True for item in services),
        total_test_passes=sum(item.tests_passed is True for item in services),
        total_verification_passes=sum(
            item.verification_passed is True for item in services
        ),
        total_repair_attempts=sum(item.repair_attempts for item in services),
        total_model_calls=sum(item.model_calls for item in results),
        total_cross_service_dependencies=sum(
            item.ownership.cross_service_dependency_count for item in results
        ),
        total_unresolved_dependencies=sum(
            item.ownership.unresolved_dependency_count for item in results
        ),
        total_ownership_violations=sum(
            item.ownership.ownership_violation_count for item in results
        ),
        total_cyclic_dependency_components=sum(
            len(item.migration_ordering.cyclic_components) for item in results
        ),
        total_foreign_repository_dependencies=sum(
            item.ownership.foreign_repository_dependency_count for item in results
        ),
        total_cross_domain_transactions=sum(
            sum(
                finding.category == "cross_domain_transaction"
                for finding in item.review_findings
            )
            for item in results
        ),
        total_human_review_findings=sum(len(item.review_findings) for item in results),
        total_blocked_services=sum(
            len(item.migration_ordering.blocked_services) for item in results
        ),
        total_first_pass_failures=sum(
            item.extraction.first_pass_failure_count for item in results
        ),
        total_post_repair_successes=sum(
            item.extraction.post_repair_success_count for item in results
        ),
        total_behavior_checks_passed=sum(
            item.behavior_preservation.passed for item in results
        ),
        total_behavior_checks_failed=sum(
            item.behavior_preservation.failed for item in results
        ),
        total_semantic_contracts_defined=sum(
            item.behavior_preservation.contracts_defined for item in results
        ),
        total_semantic_contracts_executed=sum(
            item.behavior_preservation.contracts_executed for item in results
        ),
        total_semantic_contracts_passed=sum(
            item.behavior_preservation.contracts_passed for item in results
        ),
        total_semantic_contracts_failed=sum(
            item.behavior_preservation.contracts_failed for item in results
        ),
        total_semantic_contracts_unavailable=sum(
            item.behavior_preservation.contracts_unavailable for item in results
        ),
        semantic_complete_fixture_count=sum(
            item.semantic_status == "complete" for item in results
        ),
        semantic_partial_fixture_count=sum(
            item.semantic_status == "partial" for item in results
        ),
        semantic_failed_fixture_count=sum(
            item.semantic_status == "failed" for item in results
        ),
    )
