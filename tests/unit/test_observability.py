"""Tests for secret-free demo metrics and benchmark artifacts."""

import json
from pathlib import Path
from uuid import uuid4

from migrationswarm.core.models import ModelResponse
from migrationswarm.core.observability import MetricsCollector
from migrationswarm.core.observability.models import LiveValidationReport
from migrationswarm.core.orchestrator import (
    MultiServiceMigrationRun,
    MultiServiceMigrationStatus,
    ServiceMigrationState,
    ServiceMigrationStateStatus,
)


def _run(root: Path) -> MultiServiceMigrationRun:
    states = [
        ServiceMigrationState(
            service_name="Inventory",
            service_slug="inventory",
            status=ServiceMigrationStateStatus.COMPLETED,
        ),
        ServiceMigrationState(
            service_name="Notifications",
            service_slug="notifications",
            status=ServiceMigrationStateStatus.COMPLETED,
        ),
    ]
    return MultiServiceMigrationRun(
        project_id=uuid4(),
        repository_root=root,
        selected_services=[state.service_name for state in states],
        services=states,
        status=MultiServiceMigrationStatus.COMPLETED,
    )


def test_collector_aggregates_calls_without_secret_or_prompt() -> None:
    collector = MetricsCollector()
    collector.record_model_call(
        ModelResponse(
            provider="offline",
            model="offline-deterministic",
            content="safe summary",
            latency_ms=2.5,
        )
    )
    collector.record_worker("fake-worker", success=True)
    collector.record_tasks(created=2, completed=2)

    metrics = collector.finish()

    assert metrics.model_calls == 1
    assert metrics.models[0].input_tokens is None
    assert metrics.models[0].output_tokens is None
    serialized = json.dumps(metrics.model_dump(mode="json"), sort_keys=True).lower()
    assert "prompt" not in serialized
    assert "api_key" not in serialized


def test_benchmark_writes_json_and_markdown_with_null_tokens(tmp_path: Path) -> None:
    collector = MetricsCollector()
    collector.record_model_call(
        ModelResponse(
            provider="offline",
            model="offline-deterministic",
            content="safe summary",
            latency_ms=1.0,
        )
    )
    benchmark, metrics_path, json_path, markdown_path = collector.benchmark(
        tmp_path,
        inventory=None,
        dependency_graph=None,
        boundaries=None,
        run=_run(tmp_path),
    )

    assert metrics_path.is_file()
    assert json_path.is_file()
    assert markdown_path.is_file()
    assert benchmark.ai["input_tokens"] is None
    assert benchmark.ai["output_tokens"] is None
    payload = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert payload["models"][0]["input_tokens"] is None
    report = json.loads(
        (tmp_path / ".migrationswarm" / "reports" / f"{benchmark.run_id}-benchmark.json")
        .read_text(encoding="utf-8")
    )
    assert report["safety"]["automatic_commits"] is False


def test_live_report_serialization_is_secret_free() -> None:
    report = LiveValidationReport(
        run_id=uuid4(),
        repository="demo",
        provider="groq",
        models=[
            {
                "logical_name": "groq-reasoning",
                "provider": "groq",
                "provider_model_id": "openai/gpt-oss-120b",
                "calls": 1,
                "input_tokens": None,
                "output_tokens": None,
            }
        ],
        safety={"main_repository_modified": False, "secrets_detected": False},
    )

    serialized = json.dumps(report.model_dump(mode="json"), sort_keys=True).lower()

    assert report.models[0]["input_tokens"] is None
    assert "api_key" not in serialized
    assert "prompt" not in serialized
