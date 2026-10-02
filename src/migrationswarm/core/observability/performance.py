"""Small, thread-safe performance timing contracts."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import Lock
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _utc_now() -> datetime:
    return datetime.now(UTC)


class StageTiming(BaseModel):
    """One monotonic-duration measurement for a named execution stage."""

    model_config = ConfigDict(extra="forbid")

    stage: str = Field(min_length=1)
    started_at: datetime
    ended_at: datetime
    duration_ms: float = Field(ge=0.0)
    queue_wait_ms: float = Field(default=0.0, ge=0.0)
    execution_ms: float = Field(default=0.0, ge=0.0)
    verification_ms: float = Field(default=0.0, ge=0.0)
    repair_ms: float = Field(default=0.0, ge=0.0)
    worktree_creation_ms: float = Field(default=0.0, ge=0.0)
    model_latency_ms: float = Field(default=0.0, ge=0.0)

    @field_validator("started_at", "ended_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("performance timestamps must be timezone-aware")
        return value.astimezone(UTC)


class TaskTiming(BaseModel):
    """Measured timing summary for one task execution."""

    model_config = ConfigDict(extra="forbid")

    task_id: str | None = None
    task_type: str = Field(min_length=1)
    queue_wait_ms: float = Field(default=0.0, ge=0.0)
    execution_ms: float = Field(default=0.0, ge=0.0)
    verification_ms: float = Field(default=0.0, ge=0.0)
    repair_ms: float = Field(default=0.0, ge=0.0)


class WorkerTiming(BaseModel):
    """Measured aggregate for one worker category."""

    model_config = ConfigDict(extra="forbid")

    worker_type: str = Field(min_length=1)
    configured_workers: int = Field(ge=1)
    executions: int = Field(default=0, ge=0)
    execution_ms: float = Field(default=0.0, ge=0.0)
    utilization: float | None = Field(default=None, ge=0.0)


class ServiceTiming(BaseModel):
    """Timing summary for one bounded service workflow."""

    model_config = ConfigDict(extra="forbid")

    service_name: str = Field(min_length=1)
    started_at: datetime
    ended_at: datetime
    duration_ms: float = Field(ge=0.0)
    queue_wait_ms: float = Field(default=0.0, ge=0.0)
    execution_ms: float = Field(default=0.0, ge=0.0)
    verification_ms: float = Field(default=0.0, ge=0.0)
    repair_ms: float = Field(default=0.0, ge=0.0)
    worktree_creation_ms: float = Field(default=0.0, ge=0.0)
    completed: bool = False

    @field_validator("started_at", "ended_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("service timing timestamps must be timezone-aware")
        return value.astimezone(UTC)


class MemorySnapshot(BaseModel):
    """Portable best-effort process memory observation."""

    model_config = ConfigDict(extra="forbid")

    baseline_rss_bytes: int | None = Field(default=None, ge=0)
    peak_rss_bytes: int | None = Field(default=None, ge=0)
    after_analysis_rss_bytes: int | None = Field(default=None, ge=0)
    available: bool = False


class PerformanceSnapshot(BaseModel):
    """Secret-free, serializable performance data for one benchmark run."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID = Field(default_factory=uuid4)
    generated_at: datetime = Field(default_factory=_utc_now)
    stage_timings: list[StageTiming] = Field(default_factory=list)
    task_timings: list[TaskTiming] = Field(default_factory=list)
    worker_timings: list[WorkerTiming] = Field(default_factory=list)
    service_timings: list[ServiceTiming] = Field(default_factory=list)
    model_latency_ms: float = Field(default=0.0, ge=0.0)
    execution_worker_count: int = Field(default=1, ge=1)
    verification_worker_count: int = Field(default=1, ge=1)
    service_worker_count: int = Field(default=1, ge=1)
    max_observed_concurrent_tasks: int = Field(default=0, ge=0)
    max_observed_concurrent_services: int = Field(default=0, ge=0)
    max_queue_depth: int = Field(default=0, ge=0)
    memory: MemorySnapshot = Field(default_factory=MemorySnapshot)

    @field_validator("generated_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("performance timestamp must be timezone-aware")
        return value.astimezone(UTC)


class PerformanceCollector:
    """Collect monotonic timings without introducing a tracing dependency."""

    def __init__(
        self,
        *,
        run_id: UUID | None = None,
        execution_worker_count: int = 1,
        verification_worker_count: int = 1,
        service_worker_count: int = 1,
    ) -> None:
        for label, count in (
            ("execution_worker_count", execution_worker_count),
            ("verification_worker_count", verification_worker_count),
            ("service_worker_count", service_worker_count),
        ):
            if count < 1:
                raise ValueError(f"{label} must be positive")
        self.run_id = run_id or uuid4()
        self.execution_worker_count = execution_worker_count
        self.verification_worker_count = verification_worker_count
        self.service_worker_count = service_worker_count
        self._lock = Lock()
        self._stages: list[StageTiming] = []
        self._services: list[ServiceTiming] = []
        self._service_queued_at: dict[str, float] = {}
        self._service_starts: dict[str, tuple[float, datetime, float]] = {}
        self._active_services = 0
        self._max_active_services = 0
        self._max_queue_depth = 0
        self._model_latency_ms = 0.0
        self._task_timings: list[TaskTiming] = []
        self._worker_timings: list[WorkerTiming] = []

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        """Measure a stage with monotonic time while retaining UTC observability."""
        started = monotonic()
        started_at = _utc_now()
        try:
            yield
        finally:
            ended = monotonic()
            ended_at = _utc_now()
            self.record_stage(name, started_at, ended_at, (ended - started) * 1000)

    def record_stage(
        self,
        name: str,
        started_at: datetime,
        ended_at: datetime,
        duration_ms: float,
        **details: float,
    ) -> None:
        """Record a stage duration, clamping only clock-resolution noise."""
        with self._lock:
            self._stages.append(
                StageTiming(
                    stage=name,
                    started_at=started_at,
                    ended_at=ended_at,
                    duration_ms=max(0.0, duration_ms),
                    **{key: max(0.0, value) for key, value in details.items()},
                )
            )

    def record_model_latency(self, latency_ms: float) -> None:
        with self._lock:
            self._model_latency_ms += max(0.0, latency_ms)

    def observe_queue_depth(self, depth: int) -> None:
        with self._lock:
            self._max_queue_depth = max(self._max_queue_depth, depth)

    def service_started(self, service_name: str) -> None:
        with self._lock:
            started = monotonic()
            queued_at = self._service_queued_at.pop(service_name, started)
            queue_wait_ms = max(0.0, (started - queued_at) * 1000)
            self._service_starts[service_name] = (started, _utc_now(), queue_wait_ms)
            self._active_services += 1
            self._max_active_services = max(self._max_active_services, self._active_services)

    def service_queued(self, service_name: str) -> None:
        """Mark a service as waiting for a dependency or worker slot."""
        with self._lock:
            self._service_queued_at.setdefault(service_name, monotonic())

    def service_finished(
        self,
        service_name: str,
        *,
        completed: bool,
        verification_ms: float = 0.0,
        repair_ms: float = 0.0,
        worktree_creation_ms: float = 0.0,
    ) -> None:
        with self._lock:
            started, started_at, queue_wait_ms = self._service_starts.pop(
                service_name, (monotonic(), _utc_now(), 0.0)
            )
            ended = monotonic()
            ended_at = _utc_now()
            self._active_services = max(0, self._active_services - 1)
            duration_ms = max(0.0, (ended - started) * 1000)
            self._services.append(
                ServiceTiming(
                    service_name=service_name,
                    started_at=started_at,
                    ended_at=ended_at,
                    duration_ms=duration_ms,
                    queue_wait_ms=queue_wait_ms,
                    execution_ms=duration_ms,
                    verification_ms=max(0.0, verification_ms),
                    repair_ms=max(0.0, repair_ms),
                    worktree_creation_ms=max(0.0, worktree_creation_ms),
                    completed=completed,
                )
            )

    def record_task(self, timing: TaskTiming) -> None:
        with self._lock:
            self._task_timings.append(timing)

    def record_worker(self, timing: WorkerTiming) -> None:
        with self._lock:
            self._worker_timings.append(timing)

    def snapshot(self, *, memory: MemorySnapshot | None = None) -> PerformanceSnapshot:
        with self._lock:
            return PerformanceSnapshot(
                run_id=self.run_id,
                stage_timings=list(self._stages),
                task_timings=list(self._task_timings),
                worker_timings=list(self._worker_timings),
                service_timings=list(self._services),
                model_latency_ms=self._model_latency_ms,
                execution_worker_count=self.execution_worker_count,
                verification_worker_count=self.verification_worker_count,
                service_worker_count=self.service_worker_count,
                max_observed_concurrent_tasks=self._max_active_services,
                max_observed_concurrent_services=self._max_active_services,
                max_queue_depth=self._max_queue_depth,
                memory=memory or MemorySnapshot(),
            )


def memory_snapshot(
    *, baseline: int | None = None, after_analysis: int | None = None
) -> MemorySnapshot:
    """Return best-effort RSS data without making psutil a project dependency."""
    rss: int | None = None
    try:
        resource_module: Any = __import__("resource")
        value = int(resource_module.getrusage(resource_module.RUSAGE_SELF).ru_maxrss)
        rss = value * 1024 if value < 10_000_000 else value
    except (ImportError, OSError):
        try:
            psutil_module: Any = __import__("psutil")
            rss = int(psutil_module.Process().memory_info().rss)
        except (ImportError, OSError):
            rss = None
    return MemorySnapshot(
        baseline_rss_bytes=baseline if baseline is not None else rss,
        peak_rss_bytes=rss,
        after_analysis_rss_bytes=after_analysis if after_analysis is not None else rss,
        available=rss is not None,
    )


__all__ = [
    "MemorySnapshot",
    "PerformanceCollector",
    "PerformanceSnapshot",
    "ServiceTiming",
    "StageTiming",
    "TaskTiming",
    "WorkerTiming",
    "memory_snapshot",
]
