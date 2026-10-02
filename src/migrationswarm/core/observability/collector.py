"""In-process metrics collection with bounded, secret-free artifacts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from time import monotonic
from typing import Any
from uuid import UUID

from migrationswarm.agents.dependency_analysis import JavaDependencyGraph
from migrationswarm.agents.repository_analysis import RepositoryInventory
from migrationswarm.agents.service_boundary import ServiceBoundaryReport
from migrationswarm.core.models.models import ModelResponse
from migrationswarm.core.observability.models import (
    MigrationBenchmark,
    MigrationMetrics,
    ModelMetrics,
    WorkerMetrics,
)
from migrationswarm.core.orchestrator.multi_service import MultiServiceMigrationRun

METRICS_DIR = ".migrationswarm/metrics"
REPORTS_DIR = ".migrationswarm/reports"


class MetricsCollector:
    """Collect counters in memory and write compact JSON/Markdown artifacts."""

    def __init__(self, run_id: UUID | None = None) -> None:
        self.metrics = MigrationMetrics(run_id=run_id) if run_id else MigrationMetrics()
        self._models: dict[tuple[str, str], ModelMetrics] = {}
        self._workers: dict[str, WorkerMetrics] = {}
        self._stage_started: dict[str, float] = {}
        self._lock = Lock()

    def record_model_call(self, response: ModelResponse) -> None:
        """Record provider/model/latency and only available token counts."""
        with self._lock:
            key = (response.provider, response.model)
            model = self._models.setdefault(
                key,
                ModelMetrics(provider=response.provider, model=response.model),
            )
            model.calls += 1
            model.total_latency_ms += response.latency_ms
            if response.input_tokens is not None:
                model.input_tokens = (model.input_tokens or 0) + response.input_tokens
            if response.output_tokens is not None:
                model.output_tokens = (model.output_tokens or 0) + response.output_tokens
            routing = response.metadata.get("routing")
            if isinstance(routing, dict):
                self.metrics.routing_decisions.append(_safe_routing(routing))
            self.metrics.model_calls += 1
            self.metrics.model_latency_ms += response.latency_ms

    def record_worker(self, worker: str, *, success: bool) -> None:
        with self._lock:
            item = self._workers.setdefault(worker, WorkerMetrics(worker=worker))
            item.executions += 1
            if success:
                item.successes += 1
            else:
                item.failures += 1
            self.metrics.worker_executions += 1

    def record_tasks(self, *, created: int = 0, completed: int = 0, failed: int = 0) -> None:
        with self._lock:
            self.metrics.tasks_created += created
            self.metrics.tasks_completed += completed
            self.metrics.tasks_failed += failed

    def record_verification(self, count: int = 1) -> None:
        with self._lock:
            self.metrics.verification_decisions += count

    def record_repair(self, count: int = 1) -> None:
        with self._lock:
            self.metrics.repair_attempts += count

    def record_worktree(self, count: int = 1) -> None:
        with self._lock:
            self.metrics.worktrees_created += count

    def record_extraction(self, count: int = 1) -> None:
        with self._lock:
            self.metrics.services_extracted += count

    def start_stage(self, stage: str) -> None:
        self._stage_started[stage] = monotonic()

    def finish_stage(self, stage: str) -> float:
        started = self._stage_started.pop(stage, monotonic())
        duration = max(0.0, (monotonic() - started) * 1000)
        with self._lock:
            self.metrics.stage_durations_ms[stage] = duration
            field = f"{stage}_duration_ms"
            if hasattr(self.metrics, field):
                setattr(self.metrics, field, duration)
        return duration

    def finish(self) -> MigrationMetrics:
        self.metrics.completed_at = datetime.now(UTC)
        self.metrics.migration_run_duration_ms = max(
            0.0,
            (self.metrics.completed_at - self.metrics.started_at).total_seconds() * 1000,
        )
        self.metrics.models = sorted(
            self._models.values(), key=lambda item: (item.provider, item.model)
        )
        self.metrics.workers = sorted(self._workers.values(), key=lambda item: item.worker)
        return self.metrics

    def write_metrics(self, repository_root: str | Path) -> Path:
        """Write one bounded metrics JSON artifact."""
        root = Path(repository_root).expanduser().resolve()
        path = root / METRICS_DIR / f"{self.metrics.run_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.finish().model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path

    def benchmark(
        self,
        repository_root: str | Path,
        *,
        inventory: RepositoryInventory | None,
        dependency_graph: JavaDependencyGraph | None,
        boundaries: ServiceBoundaryReport | None,
        run: MultiServiceMigrationRun,
    ) -> tuple[MigrationBenchmark, Path, Path, Path]:
        """Build and write JSON plus concise Markdown benchmark artifacts."""
        metrics_path = self.write_metrics(repository_root)
        metrics = self.metrics
        states = run.services
        completed = [item.service_name for item in states if item.status.value == "completed"]
        failed = [item.service_name for item in states if item.status.value == "failed"]
        human_review = [
            item.service_name for item in states if item.status.value == "human_review"
        ]
        benchmark = MigrationBenchmark(
            run_id=run.run_id,
            repository={
                "java_classes": dependency_graph.total_java_classes if dependency_graph else None,
                "dependency_edges": (
                    dependency_graph.total_dependency_edges if dependency_graph else None
                ),
                "candidate_services": len(boundaries.candidate_services) if boundaries else None,
                "files": inventory.total_file_count if inventory else None,
            },
            migration={
                "selected_services": list(run.selected_services),
                "completed_services": completed,
                "failed_services": failed,
                "human_review_services": human_review,
            },
            execution={
                "total_tasks": metrics.tasks_created,
                "worker_executions": metrics.worker_executions,
                "verification_attempts": metrics.verification_decisions,
                "debug_attempts": metrics.repair_attempts,
            },
            ai={
                "model_calls": metrics.model_calls,
                "models": [
                    {"provider": item.provider, "model": item.model}
                    for item in metrics.models
                ],
                "input_tokens": _sum_optional(item.input_tokens for item in metrics.models),
                "output_tokens": _sum_optional(item.output_tokens for item in metrics.models),
                "latency_ms": metrics.model_latency_ms,
                "routing_decisions": metrics.routing_decisions,
            },
            timing={
                "analysis_ms": metrics.analysis_duration_ms,
                "planning_ms": metrics.planning_duration_ms,
                "extraction_ms": metrics.extraction_duration_ms,
                "verification_ms": metrics.verification_duration_ms,
                "total_ms": metrics.migration_run_duration_ms,
            },
            safety=metrics.safety,
            metrics_artifact=str(metrics_path.relative_to(Path(repository_root).resolve()).as_posix()),
        )
        root = Path(repository_root).expanduser().resolve()
        directory = root / REPORTS_DIR
        directory.mkdir(parents=True, exist_ok=True)
        json_path = directory / f"{run.run_id}-benchmark.json"
        markdown_path = directory / f"{run.run_id}-benchmark.md"
        json_path.write_text(
            json.dumps(benchmark.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        markdown_path.write_text(_benchmark_markdown(benchmark), encoding="utf-8")
        return benchmark, metrics_path, json_path, markdown_path


def _sum_optional(values: Any) -> int | None:
    collected = [value for value in values if value is not None]
    return sum(collected) if collected else None


def _safe_routing(value: dict[str, Any]) -> dict[str, Any]:
    """Keep only router-owned, secret-free decision fields in metrics."""
    allowed = {
        "requested_capability",
        "primary_provider",
        "cross_provider_fallback_enabled",
        "attempts",
        "outcome",
        "final_provider",
        "final_model",
    }
    result = {key: value[key] for key in allowed if key in value}
    attempts = result.get("attempts")
    if isinstance(attempts, list):
        result["attempts"] = [
            {
                key: item[key]
                for key in (
                    "logical_name",
                    "provider",
                    "model",
                    "status",
                    "reason",
                    "retry_count",
                    "provider_changed",
                )
                if key in item
            }
            for item in attempts
            if isinstance(item, dict)
        ]
    return result


def _benchmark_markdown(benchmark: MigrationBenchmark) -> str:
    repository = benchmark.repository
    migration = benchmark.migration
    execution = benchmark.execution
    ai = benchmark.ai
    safety = benchmark.safety
    models = ", ".join(
        f"{item['provider']}/{item['model']}" for item in ai.get("models", [])
    ) or "unknown"
    lines = [
        f"# MigrationSwarm Benchmark {benchmark.run_id}",
        "",
        "## Repository",
        "",
        f"- Java classes: {repository.get('java_classes', 'unknown')}",
        f"- Dependency edges: {repository.get('dependency_edges', 'unknown')}",
        f"- Candidate services: {repository.get('candidate_services', 'unknown')}",
        "",
        "## Migration",
        "",
        f"- Selected: {', '.join(migration.get('selected_services', [])) or 'none'}",
        f"- Completed: {len(migration.get('completed_services', []))}",
        f"- Failed: {len(migration.get('failed_services', []))}",
        f"- Human review: {len(migration.get('human_review_services', []))}",
        "",
        "## Execution",
        "",
        f"- Tasks: {execution.get('total_tasks', 0)}",
        f"- Worker executions: {execution.get('worker_executions', 0)}",
        f"- Verification attempts: {execution.get('verification_attempts', 0)}",
        f"- Repair attempts: {execution.get('debug_attempts', 0)}",
        "",
        "## AI",
        "",
        f"- Model calls: {ai.get('model_calls', 0)}",
        f"- Models: {models}",
        f"- Input tokens: {ai.get('input_tokens', 'unknown')}",
        f"- Output tokens: {ai.get('output_tokens', 'unknown')}",
        f"- Latency: {ai.get('latency_ms', 0.0):.1f} ms",
        "",
        "## Safety",
        "",
        f"- Main repository modified: {'yes' if safety.get('main_repository_modified') else 'no'}",
        f"- Isolated worktrees used: {'yes' if safety.get('isolated_worktrees_used') else 'no'}",
        f"- Automatic commits: {'yes' if safety.get('automatic_commits') else 'no'}",
        f"- Automatic pushes: {'yes' if safety.get('automatic_pushes') else 'no'}",
        "",
    ]
    return "\n".join(lines)


__all__ = ["METRICS_DIR", "REPORTS_DIR", "MetricsCollector"]
