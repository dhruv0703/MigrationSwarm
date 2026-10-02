"""Typed contracts for reproducible MigrationSwarm benchmark results."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, TypeAlias
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _utc_now() -> datetime:
    return datetime.now(UTC)


# Pydantic's recursive aliases require a named PEP 695 alias on newer v2
# releases.  The container shapes remain explicit while nested JSON values are
# intentionally open to fixture-owned payloads.
JsonValue: TypeAlias = str | int | float | bool | None | list[object] | dict[str, object]


class SideEffectExpectation(BaseModel):
    """One externally observable effect declared by a fixture contract."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1)
    target: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class ErrorExpectation(BaseModel):
    """Expected error shape without requiring a framework-specific exception."""

    model_config = ConfigDict(extra="forbid")

    error_type: str = Field(min_length=1)
    message: str | None = None
    status: int | str | None = None


class ApiContractSpec(BaseModel):
    """A small HTTP/API contract that can be compared deterministically."""

    model_config = ConfigDict(extra="forbid")

    method: str = Field(min_length=1)
    path: str = Field(min_length=1)
    request_fields: dict[str, str] = Field(default_factory=dict)
    response_fields: dict[str, str] = Field(default_factory=dict)
    required_fields: list[str] = Field(default_factory=list)
    response_status: int | str | None = None


class DtoShape(BaseModel):
    """Stable DTO shape used for field/type compatibility checks."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    fields: dict[str, str] = Field(default_factory=dict)
    required_fields: list[str] = Field(default_factory=list)


class ValidationContract(BaseModel):
    """Validation rule observed on one request or DTO field."""

    model_config = ConfigDict(extra="forbid")

    target: str = Field(min_length=1)
    field: str = Field(min_length=1)
    rules: list[str] = Field(default_factory=list)


class ContractComparison(BaseModel):
    """Structured result for API, DTO, and provider-contract comparison."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["compatible", "incompatible", "not_evaluated"]
    missing: list[str] = Field(default_factory=list)
    unexpected: list[str] = Field(default_factory=list)
    mismatches: list[str] = Field(default_factory=list)


class ConsumerProviderContract(BaseModel):
    """A provider operation required by a generated consumer."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    request_type: str | None = None
    response_type: str | None = None
    required: bool = True


class BehaviorCheckSpec(BaseModel):
    """A typed, fixture-owned observable behavior contract.

    The marker fields remain supported as a cheap source-evidence guard.  A
    semantic result is complete only when the declared observable fields can
    also be compared.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    service: str = Field(min_length=1)
    source_markers: list[str] = Field(default_factory=list)
    generated_markers: list[str] = Field(default_factory=list)
    operation: str | None = None
    input: dict[str, JsonValue] = Field(default_factory=dict)
    expected_output: JsonValue = None
    expected_status: int | str | None = None
    expected_state: dict[str, JsonValue] = Field(default_factory=dict)
    expected_side_effects: list[SideEffectExpectation] = Field(default_factory=list)
    expected_error: ErrorExpectation | None = None
    api: ApiContractSpec | None = None
    consumer_provider: ConsumerProviderContract | None = None
    normalization_fields: list[str] = Field(default_factory=list)


BehaviorContractSpec = BehaviorCheckSpec


class BehaviorObservation(BaseModel):
    """Observed values used by the deterministic semantic comparator."""

    model_config = ConfigDict(extra="forbid")

    output: JsonValue = None
    status: int | str | None = None
    state: dict[str, JsonValue] = Field(default_factory=dict)
    side_effects: list[SideEffectExpectation] = Field(default_factory=list)
    error: ErrorExpectation | None = None
    api: ApiContractSpec | None = None
    consumer_provider: ConsumerProviderContract | None = None


class FixtureMetadata(BaseModel):
    """Machine-readable expected decomposition for one fixture."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    expected_services: list[str] = Field(min_length=1)
    aliases: dict[str, list[str]] = Field(default_factory=dict)
    package_root: str = Field(min_length=1)
    ignored_package_segments: list[str] = Field(default_factory=list)
    shared_components: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    behavior_checks: list[BehaviorCheckSpec] = Field(default_factory=list)


class ServiceBoundaryResult(BaseModel):
    """Precision, recall, and F1 for one boundary prediction."""

    model_config = ConfigDict(extra="forbid")

    expected_service_count: int = Field(ge=0)
    predicted_service_count: int = Field(ge=0)
    matched_service_count: int = Field(ge=0)
    matched_services: list[str] = Field(default_factory=list)
    missed_services: list[str] = Field(default_factory=list)
    unexpected_services: list[str] = Field(default_factory=list)
    precision: float | None = Field(default=None, ge=0.0, le=1.0)
    recall: float | None = Field(default=None, ge=0.0, le=1.0)
    f1: float | None = Field(default=None, ge=0.0, le=1.0)


class VerificationResult(BaseModel):
    """Typed verification outcome for one evaluated service."""

    model_config = ConfigDict(extra="forbid")

    attempted: bool = False
    passed: bool | None = None
    build_attempted: bool = False
    build_passed: bool | None = None
    tests_attempted: bool = False
    tests_passed: bool | None = None


class ExtractionResult(BaseModel):
    """Observed extraction and verification lifecycle for one service."""

    model_config = ConfigDict(extra="forbid")

    service_name: str = Field(min_length=1)
    boundary_discovered: bool
    extraction_attempted: bool | None = None
    extraction_completed: bool | None = None
    build_attempted: bool | None = None
    build_passed: bool | None = None
    tests_attempted: bool | None = None
    tests_passed: bool | None = None
    verification_attempted: bool | None = None
    verification_passed: bool | None = None
    verification: VerificationResult | None = None
    repair_required: bool = False
    repair_attempts: int = Field(default=0, ge=0)
    repair_succeeded: bool | None = None
    final_status: str = Field(min_length=1)
    technical_success: bool | None = None
    review_status: Literal["not_required", "review_required", "blocked"] = "not_required"
    human_review_required: bool = False
    semantic_status: Literal["complete", "partial", "failed", "not_evaluated"] = (
        "not_evaluated"
    )


class ExtractionMetrics(BaseModel):
    """Aggregate lifecycle rates with explicit denominator counts."""

    model_config = ConfigDict(extra="forbid")

    extraction_completion_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    build_success_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    test_success_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    verification_success_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    first_pass_success_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    repair_success_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    extraction_denominator: int = Field(ge=0)
    build_denominator: int = Field(ge=0)
    test_denominator: int = Field(ge=0)
    verification_denominator: int = Field(ge=0)
    first_pass_denominator: int = Field(ge=0)
    repair_denominator: int = Field(ge=0)
    first_pass_failure_count: int = Field(default=0, ge=0)
    post_repair_success_count: int = Field(default=0, ge=0)


class OwnershipReport(BaseModel):
    """Deterministic cross-boundary and persistence ownership findings."""

    model_config = ConfigDict(extra="forbid")

    cross_service_dependency_count: int = Field(default=0, ge=0)
    unresolved_dependency_count: int = Field(default=0, ge=0)
    shared_component_count: int = Field(default=0, ge=0)
    ownership_violation_count: int = Field(default=0, ge=0)
    foreign_repository_dependency_count: int = Field(default=0, ge=0)
    cross_domain_transaction_count: int = Field(default=0, ge=0)
    entity_reference_count: int = Field(default=0, ge=0)
    shared_value_object_count: int = Field(default=0, ge=0)
    ambiguous_entity_ownership_count: int = Field(default=0, ge=0)
    duplicate_mutable_entity_count: int = Field(default=0, ge=0)
    findings: list[str] = Field(default_factory=list)


class HumanReviewFinding(BaseModel):
    """A deterministic finding that separates review from technical success."""

    model_config = ConfigDict(extra="forbid")

    category: str = Field(min_length=1)
    source_service: str = Field(min_length=1)
    affected_target: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)
    impact: Literal["review_required", "blocked"]
    can_continue_safely: bool
    recommended_review_focus: str = Field(min_length=1)


class ServiceDependencyEdge(BaseModel):
    """A directed service dependency: source depends on target."""

    model_config = ConfigDict(extra="forbid")

    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    relationship: str = Field(default="cross_service_dependency", min_length=1)


class DependencyComponent(BaseModel):
    """One strongly connected component in the service graph."""

    model_config = ConfigDict(extra="forbid")

    services: list[str] = Field(min_length=1)
    cyclic: bool = False


class MigrationOrdering(BaseModel):
    """Deterministic extraction order plus explicit cycle/block information."""

    model_config = ConfigDict(extra="forbid")

    services: list[str] = Field(default_factory=list)
    dependencies: list[ServiceDependencyEdge] = Field(default_factory=list)
    components: list[DependencyComponent] = Field(default_factory=list)
    cyclic_components: list[list[str]] = Field(default_factory=list)
    recommended_order: list[str] = Field(default_factory=list)
    blocked_services: list[str] = Field(default_factory=list)


class BehaviorCheckResult(BaseModel):
    """Result of one bounded source/generated behavior-marker comparison."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    service: str = Field(min_length=1)
    source_observed: bool
    generated_observed: bool
    passed: bool
    comparison_scope: str = Field(min_length=1)
    status: Literal["passed", "failed", "partial", "not_evaluated"] = "partial"
    failure_reason: str | None = None
    evidence: list[str] = Field(default_factory=list)
    source_observation: BehaviorObservation | None = None
    generated_observation: BehaviorObservation | None = None


class BehaviorPreservationResult(BaseModel):
    """Aggregate result for limited deterministic behavior comparisons."""

    model_config = ConfigDict(extra="forbid")

    checks: list[BehaviorCheckResult] = Field(default_factory=list)
    attempted: int = Field(default=0, ge=0)
    passed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    semantic_status: Literal["complete", "partial", "failed", "not_evaluated"] = (
        "not_evaluated"
    )
    contracts_defined: int = Field(default=0, ge=0)
    contracts_executed: int = Field(default=0, ge=0)
    contracts_passed: int = Field(default=0, ge=0)
    contracts_failed: int = Field(default=0, ge=0)
    contracts_unavailable: int = Field(default=0, ge=0)
    api_contracts_compared: int = Field(default=0, ge=0)
    api_incompatibilities: int = Field(default=0, ge=0)
    validation_cases_passed: int = Field(default=0, ge=0)
    state_transitions_passed: int = Field(default=0, ge=0)
    side_effect_checks_passed: int = Field(default=0, ge=0)
    consumer_provider_contracts_passed: int = Field(default=0, ge=0)
    evidence: list[str] = Field(default_factory=list)


class ServiceDependencyAnalysis(BaseModel):
    """Deterministic dependency risks derived from source and boundary evidence."""

    model_config = ConfigDict(extra="forbid")

    ordering: MigrationOrdering = Field(default_factory=MigrationOrdering)
    foreign_repository_dependencies: list[ServiceDependencyEdge] = Field(
        default_factory=list
    )
    cross_domain_transaction_count: int = Field(default=0, ge=0)
    review_findings: list[HumanReviewFinding] = Field(default_factory=list)


# Compatibility aliases retain the names used by earlier evaluation callers.
BoundaryMetrics = ServiceBoundaryResult
BoundaryResult = ServiceBoundaryResult
ServiceEvaluation = ExtractionResult


class BaselineResult(BaseModel):
    """Result of the deterministic package-grouping baseline."""

    model_config = ConfigDict(extra="forbid")

    predicted_services: list[str] = Field(default_factory=list)
    boundary: BoundaryMetrics
    build_attempted: bool = False
    build_passed: bool | None = None
    tests_attempted: bool = False
    tests_passed: bool | None = None
    failure: str | None = None


class FixtureResult(BaseModel):
    """Complete evaluation result for one benchmark fixture."""

    model_config = ConfigDict(extra="forbid")

    fixture: str = Field(min_length=1)
    status: Literal["completed", "failed", "unavailable", "review_required"]
    expected_services: list[str] = Field(default_factory=list)
    predicted_services: list[str] = Field(default_factory=list)
    boundary: ServiceBoundaryResult
    services: list[ExtractionResult] = Field(default_factory=list)
    extraction: ExtractionMetrics
    baseline: BaselineResult
    ownership: OwnershipReport = Field(default_factory=OwnershipReport)
    migration_ordering: MigrationOrdering = Field(default_factory=MigrationOrdering)
    review_findings: list[HumanReviewFinding] = Field(default_factory=list)
    review_status: Literal["not_required", "review_required", "blocked"] = "not_required"
    technical_extraction_success: bool | None = None
    behavior_preservation: BehaviorPreservationResult = Field(
        default_factory=BehaviorPreservationResult
    )
    semantic_status: Literal["complete", "partial", "failed", "not_evaluated"] = (
        "not_evaluated"
    )
    model_calls: int = Field(default=0, ge=0)
    elapsed_ms: float | None = Field(default=None, ge=0.0)
    failure: str | None = None


class AggregateMetrics(BaseModel):
    """Macro boundary and total lifecycle metrics across fixtures."""

    model_config = ConfigDict(extra="forbid")

    fixture_count: int = Field(ge=0)
    completed_fixture_count: int = Field(ge=0)
    macro_boundary_precision: float | None = Field(default=None, ge=0.0, le=1.0)
    macro_boundary_recall: float | None = Field(default=None, ge=0.0, le=1.0)
    macro_boundary_f1: float | None = Field(default=None, ge=0.0, le=1.0)
    total_extracted_services: int = Field(ge=0)
    total_build_passes: int = Field(ge=0)
    total_test_passes: int = Field(ge=0)
    total_verification_passes: int = Field(ge=0)
    total_repair_attempts: int = Field(ge=0)
    total_model_calls: int = Field(ge=0)
    total_cross_service_dependencies: int = Field(default=0, ge=0)
    total_unresolved_dependencies: int = Field(default=0, ge=0)
    total_ownership_violations: int = Field(default=0, ge=0)
    total_cyclic_dependency_components: int = Field(default=0, ge=0)
    total_foreign_repository_dependencies: int = Field(default=0, ge=0)
    total_cross_domain_transactions: int = Field(default=0, ge=0)
    total_human_review_findings: int = Field(default=0, ge=0)
    total_blocked_services: int = Field(default=0, ge=0)
    total_first_pass_failures: int = Field(default=0, ge=0)
    total_post_repair_successes: int = Field(default=0, ge=0)
    total_behavior_checks_passed: int = Field(default=0, ge=0)
    total_behavior_checks_failed: int = Field(default=0, ge=0)
    total_semantic_contracts_defined: int = Field(default=0, ge=0)
    total_semantic_contracts_executed: int = Field(default=0, ge=0)
    total_semantic_contracts_passed: int = Field(default=0, ge=0)
    total_semantic_contracts_failed: int = Field(default=0, ge=0)
    total_semantic_contracts_unavailable: int = Field(default=0, ge=0)
    semantic_complete_fixture_count: int = Field(default=0, ge=0)
    semantic_partial_fixture_count: int = Field(default=0, ge=0)
    semantic_failed_fixture_count: int = Field(default=0, ge=0)


class BenchmarkRun(BaseModel):
    """Stable structured benchmark artifact for one evaluation invocation."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID = Field(default_factory=uuid4)
    generated_at: datetime = Field(default_factory=_utc_now)
    mode: Literal["offline", "live"]
    baseline_only: bool = False
    compare_baseline: bool = False
    fixtures: list[FixtureResult] = Field(default_factory=list)
    aggregate: AggregateMetrics
    elapsed_ms: float | None = Field(default=None, ge=0.0)

    @field_validator("generated_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("benchmark timestamps must be timezone-aware")
        return value.astimezone(UTC)
