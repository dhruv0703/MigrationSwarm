"""Secret-free metrics and benchmark contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ModelMetrics(BaseModel):
    """Aggregated metrics for one provider model."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    calls: int = Field(default=0, ge=0)
    total_latency_ms: float = Field(default=0.0, ge=0.0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class WorkerMetrics(BaseModel):
    """Aggregated executions for one worker or agent."""

    model_config = ConfigDict(extra="forbid")

    worker: str = Field(min_length=1)
    executions: int = Field(default=0, ge=0)
    successes: int = Field(default=0, ge=0)
    failures: int = Field(default=0, ge=0)


class MigrationMetrics(BaseModel):
    """Secret-free counters and timings for one demonstration run."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID = Field(default_factory=uuid4)
    started_at: datetime = Field(default_factory=_utc_now)
    completed_at: datetime | None = None
    tasks_created: int = Field(default=0, ge=0)
    tasks_completed: int = Field(default=0, ge=0)
    tasks_failed: int = Field(default=0, ge=0)
    model_calls: int = Field(default=0, ge=0)
    model_latency_ms: float = Field(default=0.0, ge=0.0)
    worker_executions: int = Field(default=0, ge=0)
    verification_decisions: int = Field(default=0, ge=0)
    repair_attempts: int = Field(default=0, ge=0)
    worktrees_created: int = Field(default=0, ge=0)
    services_extracted: int = Field(default=0, ge=0)
    analysis_duration_ms: float = Field(default=0.0, ge=0.0)
    planning_duration_ms: float = Field(default=0.0, ge=0.0)
    extraction_duration_ms: float = Field(default=0.0, ge=0.0)
    verification_duration_ms: float = Field(default=0.0, ge=0.0)
    migration_run_duration_ms: float = Field(default=0.0, ge=0.0)
    models: list[ModelMetrics] = Field(default_factory=list)
    routing_decisions: list[dict[str, Any]] = Field(default_factory=list)
    workers: list[WorkerMetrics] = Field(default_factory=list)
    stage_durations_ms: dict[str, float] = Field(default_factory=dict)
    safety: dict[str, bool] = Field(
        default_factory=lambda: {
            "main_repository_modified": False,
            "isolated_worktrees_used": True,
            "automatic_commits": False,
            "automatic_pushes": False,
        }
    )

    @field_validator("started_at", "completed_at")
    @classmethod
    def require_aware_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("metrics timestamps must be timezone-aware")
        return value.astimezone(UTC)


class MigrationBenchmark(BaseModel):
    """Bounded report combining deterministic repository and run metrics."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    generated_at: datetime = Field(default_factory=_utc_now)
    repository: dict[str, Any] = Field(default_factory=dict)
    migration: dict[str, Any] = Field(default_factory=dict)
    execution: dict[str, Any] = Field(default_factory=dict)
    ai: dict[str, Any] = Field(default_factory=dict)
    timing: dict[str, Any] = Field(default_factory=dict)
    safety: dict[str, Any] = Field(default_factory=dict)
    metrics_artifact: str | None = None

    @field_validator("generated_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("benchmark timestamp must be timezone-aware")
        return value.astimezone(UTC)


class LiveValidationReport(BaseModel):
    """Secret-free report for one manually executed live validation run."""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    generated_at: datetime = Field(default_factory=_utc_now)
    repository: str
    provider: str
    models: list[dict[str, Any]] = Field(default_factory=list)
    analysis: dict[str, Any] = Field(default_factory=dict)
    model_behavior: dict[str, Any] = Field(default_factory=dict)
    migration: dict[str, Any] = Field(default_factory=dict)
    repair: dict[str, Any] = Field(default_factory=dict)
    verification: dict[str, Any] = Field(default_factory=dict)
    safety: dict[str, Any] = Field(default_factory=dict)
    hardening: dict[str, Any] = Field(default_factory=dict)
    benchmark_comparison: dict[str, Any] = Field(default_factory=dict)

    @field_validator("generated_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("live report timestamp must be timezone-aware")
        return value.astimezone(UTC)
