"""Prompt 29 tests for bounded observable behavior contracts."""

import json
from pathlib import Path

from migrationswarm.evaluation.behavior import (
    compare_api_contracts,
    compare_consumer_provider_contracts,
    compare_dto_shapes,
    compare_observable_behavior,
    compare_side_effects,
    compare_validation_outcomes,
    evaluate_behavior_contracts,
    extract_api_contracts,
)
from migrationswarm.evaluation.models import (
    ApiContractSpec,
    BehaviorCheckSpec,
    BehaviorObservation,
    ConsumerProviderContract,
    DtoShape,
    ErrorExpectation,
    SideEffectExpectation,
    ValidationContract,
)


def _contract() -> BehaviorCheckSpec:
    return BehaviorCheckSpec(
        name="create",
        service="Orders",
        source_markers=["create("],
        generated_markers=["behavior:create"],
        operation="create",
        expected_output={"accepted": True, "requestId": "ignored"},
        expected_status=201,
        expected_state={"order": "CREATED"},
        expected_side_effects=[
            SideEffectExpectation(kind="event", target="OrderEvents", operation="publish")
        ],
        expected_error=ErrorExpectation(error_type="none"),
        normalization_fields=["requestId"],
    )


def _observation(request_id: str = "one") -> BehaviorObservation:
    return BehaviorObservation(
        output={"accepted": True, "requestId": request_id},
        status=201,
        state={"order": "CREATED"},
        side_effects=[
            SideEffectExpectation(kind="event", target="OrderEvents", operation="publish")
        ],
        error=ErrorExpectation(error_type="none"),
    )


def test_observable_contract_allows_only_explicit_id_normalization() -> None:
    contract = _contract()
    assert compare_observable_behavior(contract, _observation("one"), _observation("two")) == (
        True,
        None,
    )
    changed = _observation("two").model_copy(update={"status": 400})
    assert compare_observable_behavior(contract, _observation(), changed)[0] is False


def test_error_state_and_side_effect_mutations_are_detected() -> None:
    contract = _contract()
    source = _observation()
    assert compare_observable_behavior(
        contract, source, source.model_copy(update={"state": {"order": "CANCELLED"}})
    )[0] is False
    assert compare_observable_behavior(
        contract,
        source,
        source.model_copy(
            update={
                "error": ErrorExpectation(error_type="validation", status=400),
            }
        ),
    )[0] is False
    assert compare_side_effects(source.side_effects, []).status == "incompatible"


def test_api_contracts_detect_route_method_and_shape_drift() -> None:
    source = [
        ApiContractSpec(
            method="POST",
            path="/orders",
            request_fields={"request": "CreateOrder"},
            response_fields={"$return": "OrderDto"},
            required_fields=["NotBlank"],
            response_status=201,
        )
    ]
    assert compare_api_contracts(source, source).status == "compatible"
    method_changed = source[0].model_copy(update={"method": "GET"})
    result = compare_api_contracts(source, [method_changed])
    assert result.status == "incompatible"
    assert result.missing == ["POST /orders"]
    field_changed = source[0].model_copy(update={"request_fields": {"request": "Other"}})
    assert compare_api_contracts(source, [field_changed]).mismatches


def test_dto_and_validation_comparisons_ignore_order_but_detect_drift() -> None:
    dto = DtoShape(name="OrderDto", fields={"id": "UUID", "total": "Money"})
    reordered = dto.model_copy(
        update={"fields": {"total": "Money", "id": "UUID"}}
    )
    changed = dto.model_copy(update={"fields": {"id": "String"}})
    assert compare_dto_shapes([dto], [reordered]).status == "compatible"
    assert compare_dto_shapes([dto], [changed]).status == "incompatible"
    rules = ValidationContract(target="Order", field="id", rules=["NotNull"])
    assert compare_validation_outcomes([rules], [rules]).status == "compatible"
    assert compare_validation_outcomes(
        [rules], [rules.model_copy(update={"rules": ["NotBlank"]})]
    ).status == "incompatible"


def test_consumer_provider_contracts_detect_operation_and_type_drift() -> None:
    contract = ConsumerProviderContract(
        provider="Payments",
        operation="authorize",
        request_type="PaymentRequest",
        response_type="PaymentResult",
    )
    assert compare_consumer_provider_contracts([contract], [contract]).status == "compatible"
    changed = contract.model_copy(update={"response_type": "OtherResult"})
    assert compare_consumer_provider_contracts([contract], [changed]).status == "incompatible"


def test_api_extraction_is_deterministic_and_scoped_to_declared_routes(tmp_path: Path) -> None:
    (tmp_path / "OrderController.java").write_text(
        """@RequestMapping("/orders") class OrderController {
        @PostMapping("/create") public OrderDto create(
            @NotBlank CreateOrder request) { return null; }
        }""",
        encoding="utf-8",
    )
    first = extract_api_contracts(tmp_path)
    second = extract_api_contracts(tmp_path)
    assert first == second
    assert first[0].method == "POST"
    assert first[0].path == "/orders/create"


def test_semantic_evaluator_reports_mutated_generated_adapter(tmp_path: Path) -> None:
    source = tmp_path / "source"
    generated = tmp_path / "generated"
    source.mkdir()
    generated.mkdir()
    (source / "OrderService.java").write_text(
        "class OrderService { void create() {} }", encoding="utf-8"
    )
    (generated / "OrderTest.java").write_text(
        "// behavior:create\n", encoding="utf-8"
    )
    contract = _contract()
    (generated / "behavior-contract.json").write_text(
        json.dumps({"contracts": {"create": _observation().model_dump(mode="json")}}),
        encoding="utf-8",
    )
    passed = evaluate_behavior_contracts(source, generated, [contract])
    assert passed.semantic_status == "complete"
    assert passed.contracts_passed == 1
    payload = json.loads((generated / "behavior-contract.json").read_text(encoding="utf-8"))
    payload["contracts"]["create"]["state"]["order"] = "CANCELLED"
    (generated / "behavior-contract.json").write_text(json.dumps(payload), encoding="utf-8")
    failed = evaluate_behavior_contracts(source, generated, [contract])
    assert failed.semantic_status == "failed"
    assert failed.contracts_failed == 1


def test_missing_generated_output_is_not_fabricated_as_success(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "OrderService.java").write_text(
        "class OrderService { void create() {} }", encoding="utf-8"
    )
    result = evaluate_behavior_contracts(source, tmp_path / "missing", [_contract()])
    assert result.semantic_status == "not_evaluated"
    assert result.contracts_passed == 0
    assert result.contracts_unavailable == 1
