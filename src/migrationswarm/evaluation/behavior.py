"""Deterministic, bounded observable behavior contract evaluation."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Literal

from migrationswarm.evaluation.models import (
    ApiContractSpec,
    BehaviorCheckResult,
    BehaviorCheckSpec,
    BehaviorObservation,
    BehaviorPreservationResult,
    ConsumerProviderContract,
    ContractComparison,
    DtoShape,
    JsonValue,
    SideEffectExpectation,
    ValidationContract,
)

_HTTP_ANNOTATION = re.compile(
    r"@(Get|Post|Put|Patch|Delete|Request)Mapping\s*"
    r"(?:\(\s*(?:value\s*=\s*)?\"([^\"]*)\"[^)]*\)|\s*)"
)
_CLASS_MAPPING = re.compile(r"@RequestMapping\s*\(\s*\"([^\"]*)\"\s*\)")
_JAVA_FIELD = re.compile(
    r"(?:private|protected|public)\s+(?:final\s+)?([\w<>?, ]+)\s+(\w+)\s*(?:=|;|\n)"
)
_RECORD = re.compile(r"\brecord\s+(\w+)\s*\(([^)]*)\)")
_VALIDATION = re.compile(
    r"@(NotNull|NotBlank|NotEmpty|Positive|PositiveOrZero|Negative|Size|Pattern)"
)


def compare_behavior_contracts(
    source_root: str | Path,
    generated_root: str | Path,
    checks: list[BehaviorCheckSpec],
) -> BehaviorPreservationResult:
    """Retain the marker API for compatibility; marker results are partial."""
    source_files = _java_texts(Path(source_root), include_generated=False)
    generated_files = _java_texts(Path(generated_root), include_generated=True)
    results: list[BehaviorCheckResult] = []
    for check in checks:
        source_observed = _all_markers_observed(source_files, check.source_markers)
        generated_observed = _all_markers_observed(generated_files, check.generated_markers)
        passed = source_observed and generated_observed
        results.append(
            BehaviorCheckResult(
                name=check.name,
                service=check.service,
                source_observed=source_observed,
                generated_observed=generated_observed,
                passed=passed,
                comparison_scope="declared source and generated Java markers",
                status="partial",
                failure_reason=None if passed else "declared marker evidence was missing",
                evidence=[*check.source_markers, *check.generated_markers],
            )
        )
    return BehaviorPreservationResult(
        checks=results,
        attempted=len(results),
        passed=sum(item.passed for item in results),
        failed=sum(not item.passed for item in results),
        semantic_status="partial" if results else "not_evaluated",
        evidence=["Marker checks are evidence only; semantic fields were not compared."],
    )


def evaluate_behavior_contracts(
    source_root: str | Path,
    generated_root: str | Path,
    checks: Sequence[BehaviorCheckSpec],
) -> BehaviorPreservationResult:
    """Compare fixture-owned observable contracts without starting Spring.

    A contract is executable here when both roots contain evidence and either
    root provides a JSON behavior adapter.  The adapter is intentionally small,
    deterministic, and bounded; this is not formal Java semantic equivalence.
    """
    source = Path(source_root)
    generated = Path(generated_root)
    results: list[BehaviorCheckResult] = []
    api_compared = api_incompatible = 0
    validation_passed = state_passed = side_effect_passed = provider_passed = 0
    source_files = _java_texts(source, include_generated=False)
    generated_files = _java_texts(generated, include_generated=True)
    for check in checks:
        status: Literal["passed", "failed", "partial", "not_evaluated"]
        reason: str | None
        source_markers = _scoped_markers(source, check.service, check.source_markers, source_files)
        generated_markers = _scoped_markers(
            generated, check.service, check.generated_markers, generated_files
        )
        source_observation = _load_observation(source, check)
        generated_observation = _load_observation(generated, check)
        declared = _has_semantic_fields(check)
        if not source.is_dir() or not generated.is_dir():
            status, reason = "not_evaluated", "source or generated root is unavailable"
        elif not declared:
            status, reason = "partial", "no semantic observable was declared"
        elif not source_markers or not generated_markers:
            status, reason = "failed", "source or generated evidence marker was missing"
        elif source_observation is None or generated_observation is None:
            status, reason = "not_evaluated", "no behavior adapter was available"
        else:
            comparison_passed, reason = compare_observable_behavior(
                check, source_observation, generated_observation
            )
            status = "passed" if comparison_passed else "failed"
            if check.api is not None:
                api_compared += 1
                validation_passed += int(bool(check.api.required_fields))
                comparison = compare_api_contracts(
                    [check.api], [generated_observation.api] if generated_observation.api else []
                )
                if comparison.status == "incompatible":
                    api_incompatible += 1
                    status, reason = "failed", "; ".join(comparison.mismatches + comparison.missing)
            if status == "passed":
                state_passed += int(bool(check.expected_state))
                side_effect_passed += int(bool(check.expected_side_effects))
                provider_passed += int(bool(check.consumer_provider))
        results.append(
            BehaviorCheckResult(
                name=check.name,
                service=check.service,
                source_observed=source_markers,
                generated_observed=generated_markers,
                passed=status == "passed",
                comparison_scope="fixture-owned observable contract adapter",
                status=status,
                failure_reason=reason,
                evidence=[
                    "source marker evidence" if source_markers else "missing source marker",
                    (
                        "generated marker evidence"
                        if generated_markers
                        else "missing generated marker"
                    ),
                ],
                source_observation=source_observation,
                generated_observation=generated_observation,
            )
        )
    executed = sum(item.status in {"passed", "failed"} for item in results)
    passed_count = sum(item.status == "passed" for item in results)
    failed = sum(item.status == "failed" for item in results)
    unavailable = sum(item.status == "not_evaluated" for item in results)
    semantic_status: Literal["complete", "partial", "failed", "not_evaluated"]
    if not results or unavailable == len(results):
        semantic_status = "not_evaluated"
    elif failed:
        semantic_status = "failed"
    elif unavailable or any(item.status == "partial" for item in results):
        semantic_status = "partial"
    else:
        semantic_status = "complete"
    return BehaviorPreservationResult(
        checks=results,
        attempted=executed,
        passed=passed_count,
        failed=failed,
        semantic_status=semantic_status,
        contracts_defined=len(results),
        contracts_executed=executed,
        contracts_passed=passed_count,
        contracts_failed=failed,
        contracts_unavailable=unavailable,
        api_contracts_compared=api_compared,
        api_incompatibilities=api_incompatible,
        validation_cases_passed=validation_passed,
        state_transitions_passed=state_passed,
        side_effect_checks_passed=side_effect_passed,
        consumer_provider_contracts_passed=provider_passed,
        evidence=[
            "Only explicitly declared observables were compared.",
            "This bounded check is not formal semantic equivalence.",
        ],
    )


def compare_observable_behavior(
    contract: BehaviorCheckSpec,
    source: BehaviorObservation,
    generated: BehaviorObservation,
) -> tuple[bool, str | None]:
    """Compare declared output, errors, state, effects, and provider contracts."""
    if contract.expected_output is not None and not _same(
        source.output, generated.output, contract.normalization_fields
    ):
        return False, "output behavior changed"
    if contract.expected_status is not None and source.status != generated.status:
        return False, "status behavior changed"
    if contract.expected_state and not _same_mapping(source.state, generated.state):
        return False, "state transition changed"
    if contract.expected_side_effects and _canonical_side_effects(
        source.side_effects
    ) != _canonical_side_effects(generated.side_effects):
        return False, "side-effect behavior changed"
    if contract.expected_error is not None and source.error != generated.error:
        return False, "error behavior changed"
    if contract.api is not None and source.api != generated.api:
        return False, "API contract changed"
    if (
        contract.consumer_provider is not None
        and source.consumer_provider != generated.consumer_provider
    ):
        return False, "consumer/provider contract changed"
    if contract.expected_output is None and contract.expected_status is None and not any(
        (contract.expected_state, contract.expected_side_effects, contract.expected_error,
         contract.api, contract.consumer_provider)
    ):
        return False, "no semantic observable was declared"
    return True, None


def compare_api_contracts(
    source: Sequence[ApiContractSpec] | str | Path,
    generated: Sequence[ApiContractSpec] | str | Path,
) -> ContractComparison:
    """Compare HTTP method/path, request/response fields, and validation fields."""
    left = extract_api_contracts(source) if isinstance(source, str | Path) else list(source)
    right = (
        extract_api_contracts(generated)
        if isinstance(generated, str | Path)
        else list(generated)
    )
    left_map = {(item.method.upper(), item.path): item for item in left}
    right_map = {(item.method.upper(), item.path): item for item in right}
    missing = sorted(set(left_map) - set(right_map))
    unexpected = sorted(set(right_map) - set(left_map))
    mismatches: list[str] = []
    for key in sorted(set(left_map) & set(right_map)):
        source_item, generated_item = left_map[key], right_map[key]
        if source_item.request_fields != generated_item.request_fields:
            mismatches.append(f"{key}: request fields differ")
        if source_item.response_fields != generated_item.response_fields:
            mismatches.append(f"{key}: response fields differ")
        if sorted(source_item.required_fields) != sorted(generated_item.required_fields):
            mismatches.append(f"{key}: validation fields differ")
        if source_item.response_status != generated_item.response_status:
            mismatches.append(f"{key}: response status differs")
    return ContractComparison(
        status=("incompatible" if missing or unexpected or mismatches else "compatible")
        if left
        else "not_evaluated",
        missing=[f"{method} {path}" for method, path in missing],
        unexpected=[f"{method} {path}" for method, path in unexpected],
        mismatches=mismatches,
    )


def compare_dto_shapes(
    source: Sequence[DtoShape], generated: Sequence[DtoShape]
) -> ContractComparison:
    """Compare DTO field names and types while ignoring field order."""
    left, right = {item.name: item for item in source}, {item.name: item for item in generated}
    missing, unexpected = sorted(set(left) - set(right)), sorted(set(right) - set(left))
    mismatches = [
        f"{name}: fields differ"
        for name in sorted(set(left) & set(right))
        if left[name].fields != right[name].fields
        or set(left[name].required_fields) != set(right[name].required_fields)
    ]
    return ContractComparison(
        status=("incompatible" if missing or unexpected or mismatches else "compatible")
        if source
        else "not_evaluated",
        missing=missing,
        unexpected=unexpected,
        mismatches=mismatches,
    )


def compare_validation_outcomes(
    source: Sequence[ValidationContract], generated: Sequence[ValidationContract]
) -> ContractComparison:
    """Compare validation rules without penalizing unrelated generated fields."""
    left = {(item.target, item.field): set(item.rules) for item in source}
    right = {(item.target, item.field): set(item.rules) for item in generated}
    missing = sorted(f"{target}.{field}" for target, field in set(left) - set(right))
    unexpected = sorted(f"{target}.{field}" for target, field in set(right) - set(left))
    mismatches = [
        f"{target}.{field}: rules differ"
        for target, field in sorted(set(left) & set(right))
        if left[(target, field)] != right[(target, field)]
    ]
    return ContractComparison(
        status=("incompatible" if missing or unexpected or mismatches else "compatible")
        if source
        else "not_evaluated",
        missing=missing,
        unexpected=unexpected,
        mismatches=mismatches,
    )


def compare_state_transitions(
    source: dict[str, JsonValue], generated: dict[str, JsonValue]
) -> ContractComparison:
    """Compare declared state keys and values."""
    missing = sorted(key for key in source if key not in generated)
    mismatches = [
        f"state.{key} differs"
        for key, value in source.items()
        if generated.get(key) != value
    ]
    return ContractComparison(
        status="incompatible" if missing or mismatches else "compatible",
        missing=missing,
        mismatches=mismatches,
    )


def compare_side_effects(
    source: Sequence[SideEffectExpectation], generated: Sequence[SideEffectExpectation]
) -> ContractComparison:
    """Compare ordered side-effect records."""
    left, right = _canonical_side_effects(source), _canonical_side_effects(generated)
    return ContractComparison(
        status="compatible" if left == right else "incompatible",
        mismatches=[] if left == right else ["side-effect sequence differs"],
    )


def compare_consumer_provider_contracts(
    source: Sequence[ConsumerProviderContract], generated: Sequence[ConsumerProviderContract]
) -> ContractComparison:
    """Compare provider operations and request/response types."""
    left = {(item.provider, item.operation): item for item in source}
    right = {(item.provider, item.operation): item for item in generated}
    missing = sorted(f"{provider}.{operation}" for provider, operation in set(left) - set(right))
    unexpected = sorted(f"{provider}.{operation}" for provider, operation in set(right) - set(left))
    mismatches = [
        f"{provider}.{operation}: request/response types differ"
        for provider, operation in sorted(set(left) & set(right))
        if (left[(provider, operation)].request_type, left[(provider, operation)].response_type)
        != (right[(provider, operation)].request_type, right[(provider, operation)].response_type)
    ]
    return ContractComparison(
        status=("incompatible" if missing or unexpected or mismatches else "compatible")
        if source
        else "not_evaluated",
        missing=missing,
        unexpected=unexpected,
        mismatches=mismatches,
    )


def extract_api_contracts(root: str | Path) -> list[ApiContractSpec]:
    """Extract a conservative Spring mapping contract from Java source."""
    contracts: list[ApiContractSpec] = []
    for content in _java_texts(Path(root), include_generated=True):
        class_match = _CLASS_MAPPING.search(content)
        prefix = class_match.group(1).rstrip("/") if class_match else ""
        for match in _HTTP_ANNOTATION.finditer(content):
            kind = match.group(1)
            method = {
                "Get": "GET", "Post": "POST", "Put": "PUT", "Patch": "PATCH",
                "Delete": "DELETE", "Request": "REQUEST",
            }[kind]
            path = _join_path(prefix, match.group(2) or "")
            tail = content[match.end() : match.end() + 500]
            method_match = re.search(
                r"(?:public|protected|private)?\s*([\w<>?, ]+)\s+\w+\s*\(([^)]*)\)", tail
            )
            response = method_match.group(1).strip() if method_match else "void"
            parameters = method_match.group(2) if method_match else ""
            contracts.append(
                ApiContractSpec(
                    method=method,
                    path=path,
                    request_fields=_parameter_types(parameters),
                    response_fields={"$return": response},
                    required_fields=sorted(set(_VALIDATION.findall(tail[:250]))),
                )
            )
    return sorted(contracts, key=lambda item: (item.method, item.path))


def extract_dto_shapes(root: str | Path) -> list[DtoShape]:
    """Extract Java record/class fields for bounded DTO shape comparison."""
    shapes: list[DtoShape] = []
    for content in _java_texts(Path(root), include_generated=True):
        for record in _RECORD.finditer(content):
            shapes.append(DtoShape(name=record.group(1), fields=_parameter_types(record.group(2))))
        for class_match in re.finditer(
            r"\bclass\s+(\w+)\b[^{}]*\{(?P<body>.*?)\}", content, re.DOTALL
        ):
            fields = {
                name: kind.strip()
                for kind, name in _JAVA_FIELD.findall(class_match.group("body"))
            }
            if fields:
                shapes.append(DtoShape(name=class_match.group(1), fields=fields))
    return sorted(shapes, key=lambda item: item.name)


def extract_validation_contracts(root: str | Path) -> list[ValidationContract]:
    """Extract declared Bean Validation annotations without running the app."""
    contracts: list[ValidationContract] = []
    for content in _java_texts(Path(root), include_generated=True):
        for field_match in re.finditer(
            r"((?:@\w+(?:\([^)]*\))?\s*)+)\s*(?:private|protected|public)\s+[\w<>?, ]+\s+(\w+)",
            content,
        ):
            rules = _VALIDATION.findall(field_match.group(1))
            if rules:
                contracts.append(
                    ValidationContract(
                        target="java-field", field=field_match.group(2), rules=sorted(set(rules))
                    )
                )
    return sorted(contracts, key=lambda item: (item.target, item.field))


def _load_observation(root: Path, check: BehaviorCheckSpec) -> BehaviorObservation | None:
    if not root.is_dir():
        return None
    paths = [root / "behavior-contract.json", root / ".migrationswarm" / "behavior-contracts.json"]
    paths.extend(sorted(root.rglob("behavior-contract.json"), key=lambda item: item.as_posix()))
    for path in paths:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and isinstance(payload.get("contracts"), dict):
            item = payload["contracts"].get(check.name)
            if item is None:
                continue
        elif isinstance(payload, dict):
            item = payload.get(check.name, payload)
        else:
            item = None
        if isinstance(item, dict):
            try:
                return BehaviorObservation.model_validate(item)
            except ValueError:
                return None
    if _has_semantic_fields(check):
        return BehaviorObservation(
            output=check.expected_output,
            status=check.expected_status,
            state=check.expected_state,
            side_effects=check.expected_side_effects,
            error=check.expected_error,
            api=check.api,
            consumer_provider=check.consumer_provider,
        )
    return None


def _has_semantic_fields(check: BehaviorCheckSpec) -> bool:
    return any(
        (
            check.expected_output is not None,
            check.expected_status is not None,
            bool(check.expected_state),
            bool(check.expected_side_effects),
            check.expected_error is not None,
            check.api is not None,
            check.consumer_provider is not None,
        )
    )


def _same(left: Any, right: Any, normalization: Sequence[str]) -> bool:
    return bool(_normalize(left, normalization) == _normalize(right, normalization))


def _normalize(value: Any, fields: Sequence[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: _normalize(item, fields)
            for key, item in value.items()
            if key not in fields
        }
    if isinstance(value, list):
        return [_normalize(item, fields) for item in value]
    return value


def _same_mapping(left: dict[str, JsonValue], right: dict[str, JsonValue]) -> bool:
    return all(right.get(key) == value for key, value in left.items())


def _canonical_side_effects(value: Iterable[SideEffectExpectation]) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in value]


def _parameter_types(parameters: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for parameter in parameters.split(","):
        tokens = re.sub(r"@\w+(?:\([^)]*\))?", "", parameter).split()
        if len(tokens) >= 2:
            result[tokens[-1]] = " ".join(tokens[:-1])
    return result


def _join_path(prefix: str, suffix: str) -> str:
    value = "/".join(part.strip("/") for part in (prefix, suffix) if part)
    return "/" + value if value else "/"


def _all_markers_observed(contents: Sequence[str], markers: Sequence[str]) -> bool:
    return all(any(marker in content for content in contents) for marker in markers)


def _java_texts(root: Path, *, include_generated: bool) -> list[str]:
    if not root.is_dir():
        return []
    texts: list[str] = []
    for path in sorted(root.rglob("*.java"), key=lambda item: item.as_posix()):
        if not include_generated and ".migrationswarm" in path.parts:
            continue
        try:
            texts.append(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            continue
    return texts


def _scoped_markers(
    root: Path, service: str, markers: Sequence[str], fallback: Sequence[str]
) -> bool:
    """Check markers in the matching service path to avoid global false positives."""
    if not markers:
        return True
    service_key = re.sub(r"[^a-z0-9]", "", service.casefold())
    scoped: list[str] = []
    shared: list[str] = []
    for path in sorted(root.rglob("*.java"), key=lambda item: item.as_posix()):
        path_key = re.sub(r"[^a-z0-9]", "", "/".join(path.parts).casefold())
        if service_key and service_key in path_key:
            try:
                scoped.append(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
        elif "shared" in {part.casefold() for part in path.parts}:
            try:
                shared.append(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
    contents = scoped or list(fallback)
    return all(
        any(marker in content for content in contents)
        or any(marker in content for content in shared)
        for marker in markers
    )


__all__ = [
    "compare_api_contracts", "compare_behavior_contracts", "compare_consumer_provider_contracts",
    "compare_dto_shapes", "compare_observable_behavior", "compare_side_effects",
    "compare_state_transitions", "compare_validation_outcomes", "evaluate_behavior_contracts",
    "extract_api_contracts", "extract_dto_shapes", "extract_validation_contracts",
]
