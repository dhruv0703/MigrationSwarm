"""Small in-process metrics and benchmark artifacts for demonstrations."""

from typing import TYPE_CHECKING

from migrationswarm.core.observability.performance import (
    MemorySnapshot,
    PerformanceCollector,
    PerformanceSnapshot,
    ServiceTiming,
    StageTiming,
    TaskTiming,
    WorkerTiming,
    memory_snapshot,
)

if TYPE_CHECKING:
    from migrationswarm.core.observability.collector import MetricsCollector
    from migrationswarm.core.observability.models import (
        LiveValidationReport,
        MigrationBenchmark,
        MigrationMetrics,
        ModelMetrics,
        WorkerMetrics,
    )


def __getattr__(name: str) -> object:
    """Load legacy metrics exports lazily to avoid an orchestrator import cycle."""
    if name == "MetricsCollector":
        from migrationswarm.core.observability.collector import MetricsCollector

        return MetricsCollector
    if name in {
        "LiveValidationReport",
        "MigrationBenchmark",
        "MigrationMetrics",
        "ModelMetrics",
        "WorkerMetrics",
    }:
        from migrationswarm.core.observability import models

        return getattr(models, name)
    raise AttributeError(name)

__all__ = [
    "MetricsCollector",
    "MigrationBenchmark",
    "LiveValidationReport",
    "MigrationMetrics",
    "ModelMetrics",
    "WorkerMetrics",
    "MemorySnapshot",
    "PerformanceCollector",
    "PerformanceSnapshot",
    "ServiceTiming",
    "StageTiming",
    "TaskTiming",
    "WorkerTiming",
    "memory_snapshot",
]
