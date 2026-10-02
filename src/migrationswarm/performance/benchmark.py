"""Reproducible, offline performance characterization for MigrationSwarm."""

from __future__ import annotations

import json
import os
import platform
import shutil
import statistics
import subprocess
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from migrationswarm.agents.architecture_analysis import ArchitectureAnalysisAgent
from migrationswarm.agents.dependency_analysis import DependencyAnalysisAgent
from migrationswarm.agents.repository_analysis import RepositoryAnalysisAgent
from migrationswarm.core.observability.performance import (
    MemorySnapshot,
    PerformanceCollector,
    PerformanceSnapshot,
    memory_snapshot,
)
from migrationswarm.core.scheduler import TaskGraph, TaskScheduler
from migrationswarm.core.security import write_json_atomic, write_text_atomic
from migrationswarm.core.tasks import Task, TaskType
from migrationswarm.performance.synthetic import generate_synthetic_repository
from migrationswarm.persistence.db.repositories import TaskRepository
from migrationswarm.persistence.db.session import Database
from migrationswarm.persistence.redis.queue import ReadyTaskQueue

PERFORMANCE_ARTIFACT_DIR = Path("artifacts") / "performance"
DEFAULT_ITERATIONS = 7


class PerformanceConfiguration(BaseModel):
    """One bounded worker configuration used by the offline benchmark."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    service_workers: int = Field(ge=1, le=3)
    task_workers: int = Field(ge=1, le=4)
    verification_workers: int = Field(ge=1, le=2)


class TimingStatistics(BaseModel):
    """Distribution summary that avoids claims from one tiny timing sample."""

    model_config = ConfigDict(extra="forbid")

    iterations: int = Field(ge=1)
    median_ms: float = Field(ge=0.0)
    minimum_ms: float = Field(ge=0.0)
    maximum_ms: float = Field(ge=0.0)
    p95_ms: float | None = Field(default=None, ge=0.0)


class MicrobenchmarkResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    status: str = Field(min_length=1)
    timing: TimingStatistics | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class SyntheticResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    class_count: int = Field(ge=1)
    edge_count: int = Field(ge=0)
    node_count: int = Field(ge=0)
    repository_scan_ms: TimingStatistics
    dependency_parse_ms: TimingStatistics
    architecture_graph_ms: TimingStatistics


class ConfigurationMeasurement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    configuration: PerformanceConfiguration
    total_duration_ms: float = Field(ge=0.0)
    analysis_duration_ms: float = Field(ge=0.0)
    planning_duration_ms: float = Field(ge=0.0)
    migration_duration_ms: float = Field(ge=0.0)
    verification_duration_ms: float = Field(ge=0.0)
    repair_duration_ms: float = Field(ge=0.0)
    services_completed: int = Field(ge=0)
    tasks_completed: int = Field(ge=0)
    build_results: dict[str, str] = Field(default_factory=dict)
    max_concurrent_tasks: int = Field(ge=0)
    max_concurrent_services: int = Field(ge=0)
    max_queue_depth: int = Field(ge=0)
    worker_utilization: dict[str, float | None] = Field(default_factory=dict)
    worker_scope: str = Field(min_length=1)
    memory: MemorySnapshot
    snapshot: PerformanceSnapshot


class PerformanceReport(BaseModel):
    """Machine-readable performance artifact with no local private paths."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID = Field(default_factory=uuid4)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    environment: dict[str, Any] = Field(default_factory=dict)
    sequential: ConfigurationMeasurement
    parallel: list[ConfigurationMeasurement] = Field(default_factory=list)
    speedups: dict[str, float] = Field(default_factory=dict)
    bottlenecks: list[dict[str, Any]] = Field(default_factory=list)
    synthetic_repositories: list[SyntheticResult] = Field(default_factory=list)
    microbenchmarks: list[MicrobenchmarkResult] = Field(default_factory=list)
    safety: dict[str, bool] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)

    @field_validator("generated_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("performance report timestamp must be timezone-aware")
        return value.astimezone(UTC)


def run_performance_benchmark(
    fixture: str | Path,
    *,
    output_root: str | Path = PERFORMANCE_ARTIFACT_DIR,
    iterations: int = DEFAULT_ITERATIONS,
) -> tuple[PerformanceReport, Path, Path]:
    """Run bounded offline measurements and write JSON/Markdown artifacts."""
    if iterations < 3 or iterations > 15:
        raise ValueError("iterations must be between 3 and 15")
    source = Path(fixture).expanduser().resolve()
    if not source.is_dir():
        raise ValueError("performance fixture must be a directory")
    configurations = [
        PerformanceConfiguration(
            name="sequential", service_workers=1, task_workers=1, verification_workers=1
        ),
        PerformanceConfiguration(
            name="parallel-2", service_workers=2, task_workers=2, verification_workers=1
        ),
        PerformanceConfiguration(
            name="parallel-3", service_workers=3, task_workers=2, verification_workers=2
        ),
    ]
    measurements = [
        _measure_configuration(source, configuration) for configuration in configurations
    ]
    sequential = measurements[0]
    parallel = measurements[1:]
    speedups = {
        measurement.configuration.name: round(
            calculate_speedup(sequential.total_duration_ms, measurement.total_duration_ms), 4
        )
        for measurement in parallel
        if measurement.total_duration_ms > 0
    }
    synthetic = _synthetic_results(iterations)
    microbenchmarks = _microbenchmarks(iterations)
    report = PerformanceReport(
        sequential=sequential,
        parallel=parallel,
        speedups=speedups,
        bottlenecks=_bottleneck_report(measurements),
        synthetic_repositories=synthetic,
        microbenchmarks=microbenchmarks,
        environment=_environment(),
        safety={"main_repository_modified": False, "live_provider_contacted": False},
        limitations=[
            "Offline timings exclude live model latency and provider rate limits.",
            "The included commerce fixture is intentionally small and is not an "
            "enterprise scalability claim.",
            "Task and verification worker counts are recorded as bounded configuration "
            "dimensions; the offline demo exercises service-level orchestration, while "
            "queue and persistence paths are measured separately.",
            "Maven build time, filesystem state, and local machine load can dominate results.",
        ],
    )
    directory = Path(output_root) / str(report.run_id)
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "summary.json"
    markdown_path = directory / "summary.md"
    write_json_atomic(json_path, report.model_dump(mode="json"))
    write_text_atomic(markdown_path, render_performance_markdown(report))
    return report, json_path, markdown_path


def _measure_configuration(
    source: Path, configuration: PerformanceConfiguration
) -> ConfigurationMeasurement:
    with tempfile.TemporaryDirectory(prefix="migrationswarm-perf-") as temporary:
        repository = Path(temporary) / source.name
        shutil.copytree(source, repository)
        performance = PerformanceCollector(
            execution_worker_count=configuration.task_workers,
            verification_worker_count=configuration.verification_workers,
            service_worker_count=configuration.service_workers,
        )
        baseline = memory_snapshot()
        started = monotonic()
        from migrationswarm.demo import run_demo

        result = run_demo(
            repository,
            live=False,
            service_workers=configuration.service_workers,
            task_workers=configuration.task_workers,
            verification_workers=configuration.verification_workers,
            performance=performance,
        )
        total_ms = (monotonic() - started) * 1000
        memory = memory_snapshot(
            baseline=baseline.baseline_rss_bytes,
            after_analysis=baseline.after_analysis_rss_bytes,
        )
        snapshot = performance.snapshot(memory=memory)
        metrics = json.loads((repository / result.metrics_path).read_text(encoding="utf-8"))
        benchmark = json.loads((repository / result.benchmark_path).read_text(encoding="utf-8"))
        stages = _stage_totals(snapshot)
        build_results = {
            item.service_name: ("passed" if item.completed else "failed")
            for item in snapshot.service_timings
        }
        return ConfigurationMeasurement(
            configuration=configuration,
            total_duration_ms=max(0.0, total_ms),
            analysis_duration_ms=stages.get(
                "analysis",
                _sum_stage(
                    snapshot,
                    "repository_analysis",
                    "dependency_analysis",
                    "architecture_analysis",
                ),
            ),
            planning_duration_ms=stages.get("planning", _sum_stage(snapshot, "planning")),
            migration_duration_ms=stages.get("migration", _sum_stage(snapshot, "migration")),
            verification_duration_ms=_sum_stage(snapshot, "maven_build", "verification"),
            repair_duration_ms=_sum_stage(snapshot, "repair"),
            services_completed=len(benchmark["migration"].get("completed_services", [])),
            tasks_completed=int(metrics.get("tasks_completed", 0)),
            build_results=build_results,
            max_concurrent_tasks=snapshot.max_observed_concurrent_tasks,
            max_concurrent_services=snapshot.max_observed_concurrent_services,
            max_queue_depth=snapshot.max_queue_depth,
            worker_utilization={
                "service": min(
                    1.0,
                    sum(item.duration_ms for item in snapshot.service_timings)
                    / max(1.0, total_ms * configuration.service_workers),
                ),
                "task": None,
                "verification": None,
            },
            worker_scope="service orchestration measured; task/verification counts configured",
            memory=memory,
            snapshot=snapshot,
        )


def _stage_totals(snapshot: PerformanceSnapshot) -> dict[str, float]:
    totals: dict[str, float] = {}
    for timing in snapshot.stage_timings:
        totals[timing.stage] = totals.get(timing.stage, 0.0) + timing.duration_ms
    return totals


def _sum_stage(snapshot: PerformanceSnapshot, *names: str) -> float:
    return sum(item.duration_ms for item in snapshot.stage_timings if item.stage in names)


def _synthetic_results(iterations: int) -> list[SyntheticResult]:
    results: list[SyntheticResult] = []
    for class_count in (25, 100, 500):
        with tempfile.TemporaryDirectory(prefix="migrationswarm-synthetic-") as temporary:
            root = generate_synthetic_repository(Path(temporary), class_count)

            def repository_operation(root: Path = root) -> Any:
                return RepositoryAnalysisAgent().analyze(root)

            def dependency_operation(root: Path = root) -> Any:
                return DependencyAnalysisAgent().analyze(root)

            repository_times = _timed(
                repository_operation, min(3, iterations)
            )
            dependency_times = _timed(
                dependency_operation, min(3, iterations)
            )
            graph = DependencyAnalysisAgent().analyze(root)

            def architecture_operation(graph: Any = graph) -> Any:
                return ArchitectureAnalysisAgent().analyze_graph(graph)

            architecture_times = _timed(
                architecture_operation, min(3, iterations)
            )
            report, graph_data = ArchitectureAnalysisAgent().analyze_graph(graph)
            del report
            results.append(
                SyntheticResult(
                    class_count=class_count,
                    edge_count=graph.total_dependency_edges,
                    node_count=graph_data.number_of_nodes(),
                    repository_scan_ms=_stats(repository_times),
                    dependency_parse_ms=_stats(dependency_times),
                    architecture_graph_ms=_stats(architecture_times),
                )
            )
    return results


def _microbenchmarks(iterations: int) -> list[MicrobenchmarkResult]:
    results: list[MicrobenchmarkResult] = []
    with tempfile.TemporaryDirectory(prefix="migrationswarm-micro-") as temporary:
        root = generate_synthetic_repository(Path(temporary) / "repo", 25)
        dependency = DependencyAnalysisAgent().analyze(root)
        payload = dependency.model_dump(mode="json")

        def repository_operation() -> Any:
            return RepositoryAnalysisAgent().analyze(root)

        def dependency_operation() -> Any:
            return DependencyAnalysisAgent().analyze(root)

        def architecture_operation() -> Any:
            return ArchitectureAnalysisAgent().analyze_graph(dependency)

        def json_operation() -> None:
            _json_round_trip(payload)

        results.append(
            _micro("repository_analysis", repository_operation, iterations)
        )
        results.append(
            _micro("dependency_analysis", dependency_operation, iterations)
        )
        results.append(
            _micro("architecture_analysis", architecture_operation, iterations)
        )
        results.append(_micro("task_graph_scheduling", _schedule_graph, iterations))
        results.append(_redis_microbenchmark(iterations))
        results.append(_sqlite_microbenchmark(iterations))
        results.append(
            _micro(
                "json_artifact_serialization",
                json_operation,
                iterations,
            )
        )
        results.append(_sqlite_dispatch_microbenchmark(iterations))
    return results


def _schedule_graph() -> None:
    tasks: list[Task] = []
    previous: UUID | None = None
    for index in range(100):
        task = Task(
            task_type=TaskType.TEST,
            title=f"benchmark-{index}",
            description="benchmark",
            dependencies=[previous] if previous else [],
        )
        tasks.append(task)
        previous = task.id
    TaskScheduler(TaskGraph(tasks)).schedule()


def _redis_microbenchmark(iterations: int) -> MicrobenchmarkResult:
    try:
        import fakeredis
    except ImportError:
        return MicrobenchmarkResult(name="redis_ready_queue", status="unavailable")

    def operation() -> None:
        queue = ReadyTaskQueue(fakeredis.FakeRedis(decode_responses=True))
        task_ids = [uuid4() for _ in range(25)]
        for task_id in task_ids:
            queue.enqueue(task_id)
        while queue.dequeue() is not None:
            pass

    return _micro("redis_ready_queue", operation, iterations)


def _sqlite_microbenchmark(iterations: int) -> MicrobenchmarkResult:
    with tempfile.TemporaryDirectory(prefix="migrationswarm-sqlite-") as temporary:
        database = Database(f"sqlite:///{Path(temporary) / 'state.db'}")
        database.create_all_for_tests()
        repository = TaskRepository(database.session_factory)
        project_id = uuid4()

        def operation() -> None:
            task = Task(
                project_id=project_id,
                task_type=TaskType.TEST,
                title="sqlite-round-trip",
                description="benchmark",
            )
            repository.create(task)
            repository.get(task.id)

        result = _micro("sqlite_persistence_round_trip", operation, iterations)
        database.dispose()
        return result


def _sqlite_dispatch_microbenchmark(iterations: int) -> MicrobenchmarkResult:
    with tempfile.TemporaryDirectory(prefix="migrationswarm-dispatch-") as temporary:
        database = Database(f"sqlite:///{Path(temporary) / 'state.db'}")
        database.create_all_for_tests()
        repository = TaskRepository(database.session_factory)
        project_id = uuid4()
        for _ in range(25):
            repository.create(
                Task(
                    project_id=project_id,
                    task_type=TaskType.TEST,
                    title="dispatch",
                    description="benchmark",
                )
            )
        try:
            import fakeredis

            queue = ReadyTaskQueue(fakeredis.FakeRedis(decode_responses=True))
        except ImportError:
            database.dispose()
            return MicrobenchmarkResult(name="worker_dispatch_overhead", status="unavailable")
        from migrationswarm.workers.dispatcher import TaskDispatcher

        dispatcher = TaskDispatcher(repository, queue)

        def dispatch_operation() -> Any:
            return dispatcher.inspect(project_id)

        result = _micro(
            "worker_dispatch_overhead",
            dispatch_operation,
            iterations,
        )
        database.dispose()
        return result


def _json_round_trip(payload: dict[str, Any]) -> None:
    json.loads(json.dumps(payload, sort_keys=True))


def _micro(name: str, operation: Callable[[], Any], iterations: int) -> MicrobenchmarkResult:
    values = _timed(operation, iterations)
    return MicrobenchmarkResult(name=name, status="completed", timing=_stats(values))


def _timed(operation: Callable[[], Any], iterations: int) -> list[float]:
    values: list[float] = []
    for _ in range(iterations):
        started = monotonic()
        operation()
        values.append(max(0.0, (monotonic() - started) * 1000))
    return values


def _stats(values: list[float]) -> TimingStatistics:
    ordered = sorted(values)
    p95_index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))
    return TimingStatistics(
        iterations=len(values),
        median_ms=statistics.median(values),
        minimum_ms=min(values),
        maximum_ms=max(values),
        p95_ms=ordered[p95_index] if len(values) >= 5 else None,
    )


def _bottleneck_report(measurements: list[ConfigurationMeasurement]) -> list[dict[str, Any]]:
    totals: dict[str, float] = {}
    for measurement in measurements:
        snapshot = measurement.snapshot
        for timing in snapshot.stage_timings:
            category = {
                "maven_build": "MAVEN_BUILD",
                "verification": "VERIFICATION",
                "worktree_creation": "WORKTREE_CREATION",
                "repair": "REPAIR",
                "repository_analysis": "REPOSITORY_ANALYSIS",
                "dependency_analysis": "DEPENDENCY_ANALYSIS",
                "planning": "MODEL_LATENCY",
            }.get(timing.stage, timing.stage.upper())
            totals[category] = totals.get(category, 0.0) + timing.duration_ms
        if snapshot.model_latency_ms:
            totals["MODEL_LATENCY"] = totals.get("MODEL_LATENCY", 0.0) + snapshot.model_latency_ms
    return classify_bottlenecks(totals)


def calculate_speedup(sequential_ms: float, parallel_ms: float) -> float:
    """Return a measured speedup; values below one are intentionally preserved."""
    if sequential_ms < 0 or parallel_ms <= 0:
        raise ValueError("durations must be nonnegative and parallel_ms must be positive")
    return sequential_ms / parallel_ms


def classify_bottlenecks(stage_durations: dict[str, float]) -> list[dict[str, Any]]:
    """Rank deterministic bottleneck categories by measured duration share."""
    totals = {category: max(0.0, duration) for category, duration in stage_durations.items()}
    total = sum(totals.values())
    return [
        {"category": category, "duration_ms": round(duration, 3), "share": duration / total}
        for category, duration in sorted(totals.items(), key=lambda item: (-item[1], item[0]))
        if total
    ]


def _environment() -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "platform": platform.system(),
        "cpu_count": os.cpu_count(),
        "java": _tool_version("java", "-version"),
        "maven": _tool_version("mvn", "-version"),
    }


def _tool_version(command: str, argument: str) -> str | None:
    executable = shutil.which(command)
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, argument],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    line = (completed.stdout or completed.stderr).splitlines()
    return line[0].strip()[:80] if line else None


def render_performance_markdown(report: PerformanceReport) -> str:
    """Render a concise report without private paths or machine identifiers."""
    lines = [
        "# MigrationSwarm Performance",
        "",
        f"- Run: `{report.run_id}`",
        f"- Python: `{report.environment.get('python', 'unknown')}`",
        f"- Platform: `{report.environment.get('platform', 'unknown')}`",
        f"- CPU count: `{report.environment.get('cpu_count', 'unknown')}`",
        "- Provider calls: `none (offline benchmark)`",
        "",
        "## Sequential vs parallel",
        "",
        "| Configuration | Service workers | Task workers | Verification workers | "
        "Duration ms | Speedup | Max services |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    measurements = [report.sequential, *report.parallel]
    for measurement in measurements:
        config = measurement.configuration
        speedup = (
            "1.0000x"
            if config.name == "sequential"
            else f"{report.speedups.get(config.name, 0.0):.4f}x"
        )
        lines.append(
            f"| {config.name} | {config.service_workers} | {config.task_workers} | "
            f"{config.verification_workers} | {measurement.total_duration_ms:.3f} | "
            f"{speedup} | {measurement.max_concurrent_services} |"
        )
    lines.extend(["", "## Bottlenecks", ""])
    for bottleneck in report.bottlenecks[:5]:
        lines.append(
            f"- `{bottleneck['category']}`: {bottleneck['duration_ms']:.3f} ms "
            f"({bottleneck['share'] * 100:.1f}% of measured stage time)"
        )
    lines.extend(
        [
            "",
            "## Synthetic repositories",
            "",
            "| Classes | Edges | Scan median ms | Parse median ms | Graph median ms |",
            "|---:|---:|---:|---:|---:|",
        ]
    )
    for synthetic in report.synthetic_repositories:
        lines.append(
            f"| {synthetic.class_count} | {synthetic.edge_count} | "
            f"{synthetic.repository_scan_ms.median_ms:.3f} | "
            f"{synthetic.dependency_parse_ms.median_ms:.3f} | "
            f"{synthetic.architecture_graph_ms.median_ms:.3f} |"
        )
    lines.extend(["", "## Microbenchmarks", ""])
    for microbenchmark in report.microbenchmarks:
        if microbenchmark.timing is None:
            lines.append(f"- `{microbenchmark.name}`: unavailable")
        else:
            lines.append(
                f"- `{microbenchmark.name}`: median {microbenchmark.timing.median_ms:.3f} ms"
            )
    lines.extend(["", "## Limitations", ""])
    lines.extend(f"- {limitation}" for limitation in report.limitations)
    return "\n".join(lines) + "\n"


__all__ = [
    "ConfigurationMeasurement",
    "MicrobenchmarkResult",
    "PerformanceConfiguration",
    "PerformanceReport",
    "SyntheticResult",
    "TimingStatistics",
    "calculate_speedup",
    "classify_bottlenecks",
    "render_performance_markdown",
    "run_performance_benchmark",
]
