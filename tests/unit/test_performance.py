"""Tests for bounded performance measurement and reporting contracts."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Barrier, Event, Lock, Thread
from time import sleep
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from migrationswarm.cli.main import app
from migrationswarm.core.observability.performance import (
    PerformanceCollector,
    TaskTiming,
)
from migrationswarm.performance.benchmark import (
    PerformanceConfiguration,
    calculate_speedup,
    classify_bottlenecks,
)
from migrationswarm.performance.synthetic import generate_synthetic_repository

RUNNER = CliRunner()


def test_stage_timings_are_recorded_with_nonnegative_monotonic_duration() -> None:
    collector = PerformanceCollector(service_worker_count=2)
    with collector.stage("analysis"):
        pass
    collector.record_task(TaskTiming(task_type="TEST", execution_ms=1.0))
    snapshot = collector.snapshot()
    assert snapshot.stage_timings[0].duration_ms >= 0
    assert snapshot.stage_timings[0].ended_at >= snapshot.stage_timings[0].started_at
    assert snapshot.task_timings[0].execution_ms == 1.0


def test_service_concurrency_measurement_proves_independent_overlap_and_dependency_wait() -> None:
    collector = PerformanceCollector(service_worker_count=2)
    barrier = Barrier(2)
    dependency_finished = Event()
    independent_count = 0
    events: list[str] = []
    events_lock = Lock()

    def independent(name: str) -> None:
        nonlocal independent_count
        collector.service_started(name)
        with events_lock:
            events.append(f"{name}:start")
        barrier.wait(timeout=2)
        sleep(0.005)
        with events_lock:
            events.append(f"{name}:finish")
        collector.service_finished(name, completed=True)
        with events_lock:
            independent_count += 1
        if independent_count == 2:
            dependency_finished.set()

    def dependent() -> None:
        dependency_finished.wait(timeout=2)
        collector.service_started("C")
        with events_lock:
            events.append("C:start")
        collector.service_finished("C", completed=True)

    threads = [
        Thread(target=independent, args=("A",)),
        Thread(target=independent, args=("B",)),
        Thread(target=dependent),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert collector.snapshot().max_observed_concurrent_services == 2
    assert events.index("C:start") > events.index("A:finish")
    assert events.index("C:start") > events.index("B:finish")


def test_worker_configuration_and_speedup_are_bounded_and_deterministic() -> None:
    assert PerformanceConfiguration(
        name="parallel", service_workers=2, task_workers=2, verification_workers=1
    ).service_workers == 2
    with pytest.raises(ValueError):
        PerformanceConfiguration(
            name="invalid", service_workers=4, task_workers=1, verification_workers=1
        )
    assert calculate_speedup(100.0, 50.0) == 2.0
    assert calculate_speedup(50.0, 100.0) == 0.5
    with pytest.raises(ValueError):
        calculate_speedup(100.0, 0.0)


def test_bottleneck_classifier_ranks_controlled_durations() -> None:
    report = classify_bottlenecks({"MAVEN_BUILD": 65.0, "MODEL_LATENCY": 20.0, "QUEUE_WAIT": 15.0})
    assert report[0]["category"] == "MAVEN_BUILD"
    assert report[0]["share"] == pytest.approx(0.65)


@pytest.mark.parametrize("class_count", [25, 100, 500])
def test_synthetic_repository_generator_has_requested_size(
    tmp_path: Path, class_count: int
) -> None:
    root = generate_synthetic_repository(tmp_path / str(class_count), class_count)
    assert len(list(root.rglob("*.java"))) == class_count


def test_performance_report_serialization_has_no_private_paths(tmp_path: Path) -> None:
    payload = {"environment": {"platform": "test"}, "summary": "safe"}
    path = tmp_path / "summary.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert str(tmp_path) not in path.read_text(encoding="utf-8")


def test_perf_cli_rejects_unknown_fixture_without_running_benchmark() -> None:
    result = RUNNER.invoke(app, ["perf", "--fixture", "unknown"])
    assert result.exit_code == 2
    assert "unknown performance fixture" in result.output.lower()


def test_perf_cli_prints_measurement_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = PerformanceConfiguration(
        name="sequential", service_workers=1, task_workers=1, verification_workers=1
    )
    measurement = SimpleNamespace(
        configuration=config,
        total_duration_ms=10.0,
        max_concurrent_services=1,
    )
    report = SimpleNamespace(
        sequential=measurement,
        parallel=[],
        speedups={},
        synthetic_repositories=[],
        bottlenecks=[],
    )
    monkeypatch.setattr(
        "migrationswarm.cli.main.run_performance_benchmark",
        lambda *args, **kwargs: (report, tmp_path / "summary.json", tmp_path / "summary.md"),
    )
    result = RUNNER.invoke(app, ["perf", "--offline"])
    assert result.exit_code == 0
    assert "MigrationSwarm Performance" in result.output
    assert "sequential" in result.output
