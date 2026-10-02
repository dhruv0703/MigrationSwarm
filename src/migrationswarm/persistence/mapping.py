"""Explicit mappings between Pydantic domain state and database records."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from migrationswarm.core.agents import AgentResult
from migrationswarm.core.orchestrator.models import (
    MigrationRun,
    MigrationRunEvent,
    MigrationRunEventType,
    MigrationRunStatus,
)
from migrationswarm.core.orchestrator.multi_service import (
    MultiServiceMigrationRun,
    MultiServiceMigrationStatus,
    ServiceMigrationDependency,
    ServiceMigrationState,
)
from migrationswarm.core.repair import RepairAttempt, RepairAttemptStatus
from migrationswarm.core.security import redact_secrets
from migrationswarm.core.tasks import Task, TaskStatus, TaskType
from migrationswarm.persistence.db.models import (
    AgentExecutionRecord,
    MigrationRunEventRecord,
    MigrationRunRecord,
    MultiMigrationRunRecord,
    ProjectRecord,
    RepairAttemptRecord,
    TaskRecord,
)


def utc(value: datetime) -> datetime:
    """Normalize database timestamps to aware UTC values."""
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class Project(BaseModel):
    """Durable project identity used by persistence callers."""

    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1)
    repository_path: Path
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AgentExecution(BaseModel):
    """Durable, secret-free summary of one agent execution."""

    model_config = ConfigDict(extra="forbid")

    execution_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    agent_name: str
    started_at: datetime
    completed_at: datetime
    success: bool
    summary: str
    artifacts: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


def project_record(project: Project) -> ProjectRecord:
    return ProjectRecord(
        id=str(project.id),
        name=project.name,
        repository_path=str(project.repository_path),
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


def project_domain(record: ProjectRecord) -> Project:
    return Project(
        id=UUID(record.id),
        name=record.name,
        repository_path=Path(record.repository_path),
        created_at=utc(record.created_at),
        updated_at=utc(record.updated_at),
    )


def task_record(task: Task) -> TaskRecord:
    return TaskRecord(
        id=str(task.id),
        project_id=str(task.project_id),
        task_type=task.task_type.value,
        title=task.title,
        description=task.description,
        status=task.status.value,
        assigned_agent=task.assigned_agent,
        metadata_json=secret_free(task.metadata),
        attempt=task.attempt,
        max_attempts=task.max_attempts,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


def task_domain(record: TaskRecord, dependencies: list[str]) -> Task:
    return Task(
        id=UUID(record.id),
        project_id=UUID(record.project_id),
        task_type=TaskType(record.task_type),
        title=record.title,
        description=record.description,
        status=TaskStatus(record.status),
        dependencies=[UUID(value) for value in dependencies],
        assigned_agent=record.assigned_agent,
        metadata=dict(record.metadata_json),
        attempt=record.attempt,
        max_attempts=record.max_attempts,
        created_at=utc(record.created_at),
        updated_at=utc(record.updated_at),
    )


def repair_attempt_record(attempt: RepairAttempt) -> RepairAttemptRecord:
    return RepairAttemptRecord(
        id=str(attempt.id),
        original_task_id=str(attempt.original_task_id),
        debug_task_id=str(attempt.debug_task_id),
        attempt_number=attempt.attempt_number,
        failure_category=attempt.failure_category,
        status=attempt.status.value,
        started_at=attempt.started_at,
        completed_at=attempt.completed_at,
        verification_artifact_before=attempt.verification_artifact_before,
        verification_artifact_after=attempt.verification_artifact_after,
        debug_artifact=attempt.debug_artifact,
        model_provider=attempt.model_provider,
        model_name=attempt.model_name,
    )


def repair_attempt_domain(record: RepairAttemptRecord) -> RepairAttempt:
    return RepairAttempt(
        id=UUID(record.id),
        original_task_id=UUID(record.original_task_id),
        debug_task_id=UUID(record.debug_task_id),
        attempt_number=record.attempt_number,
        failure_category=record.failure_category,
        status=RepairAttemptStatus(record.status),
        started_at=utc(record.started_at),
        completed_at=utc(record.completed_at) if record.completed_at else None,
        verification_artifact_before=record.verification_artifact_before,
        verification_artifact_after=record.verification_artifact_after,
        debug_artifact=record.debug_artifact,
        model_provider=record.model_provider,
        model_name=record.model_name,
    )


def multi_run_record(run: MultiServiceMigrationRun) -> MultiMigrationRunRecord:
    return MultiMigrationRunRecord(
        run_id=str(run.run_id),
        project_id=str(run.project_id),
        repository_root=str(run.repository_root),
        selected_services=list(run.selected_services),
        dependencies=[item.model_dump(mode="json") for item in run.dependencies],
        status=run.status.value,
        service_states=[item.model_dump(mode="json") for item in run.services],
        started_at=run.started_at,
        completed_at=run.completed_at,
        warnings=secret_free(run.warnings),
    )


def multi_run_domain(record: MultiMigrationRunRecord) -> MultiServiceMigrationRun:
    return MultiServiceMigrationRun(
        run_id=UUID(record.run_id),
        project_id=UUID(record.project_id),
        repository_root=Path(record.repository_root),
        selected_services=list(record.selected_services),
        dependencies=[
            ServiceMigrationDependency.model_validate(item)
            for item in record.dependencies
        ],
        status=MultiServiceMigrationStatus(record.status),
        services=[ServiceMigrationState.model_validate(item) for item in record.service_states],
        started_at=utc(record.started_at),
        completed_at=utc(record.completed_at) if record.completed_at else None,
        warnings=list(record.warnings),
    )


def migration_run_record(run: MigrationRun, project_id: UUID | None) -> MigrationRunRecord:
    return MigrationRunRecord(
        run_id=str(run.run_id),
        project_id=str(project_id or run.project_id) if (project_id or run.project_id) else None,
        repository_root=str(run.repository_root),
        selected_service=run.selected_service,
        task_id=str(run.task_id),
        worktree_path=str(run.worktree_path) if run.worktree_path else None,
        started_at=run.started_at,
        completed_at=run.completed_at,
        status=run.status.value,
        current_stage=run.current_stage,
        generated_files=list(run.generated_files),
        verification_status=run.verification_status,
        final_task_status=run.final_task_status.value if run.final_task_status else None,
        warnings=list(run.warnings),
        failure_reason=run.failure_reason,
        debug_attempts=run.debug_attempts,
        max_debug_attempts=run.max_debug_attempts,
    )


def migration_event_record(
    run_id: UUID, event: MigrationRunEvent, sequence: int
) -> MigrationRunEventRecord:
    return MigrationRunEventRecord(
        run_id=str(run_id),
        sequence=sequence,
        event_type=event.event_type.value,
        occurred_at=event.occurred_at,
        stage=event.stage,
        payload=secret_free(event.payload),
    )


def migration_run_domain(
    record: MigrationRunRecord, events: list[MigrationRunEventRecord]
) -> MigrationRun:
    return MigrationRun(
        run_id=UUID(record.run_id),
        repository_root=Path(record.repository_root),
        selected_service=record.selected_service,
        task_id=UUID(record.task_id),
        project_id=UUID(record.project_id) if record.project_id else None,
        worktree_path=Path(record.worktree_path) if record.worktree_path else None,
        started_at=utc(record.started_at),
        completed_at=utc(record.completed_at) if record.completed_at else None,
        status=MigrationRunStatus(record.status),
        current_stage=record.current_stage,
        events=[
            MigrationRunEvent(
                event_type=MigrationRunEventType(event.event_type),
                occurred_at=utc(event.occurred_at),
                stage=event.stage,
                payload=dict(event.payload),
            )
            for event in sorted(events, key=lambda item: item.sequence)
        ],
        generated_files=list(record.generated_files),
        verification_status=record.verification_status,
        final_task_status=TaskStatus(record.final_task_status)
        if record.final_task_status
        else None,
        warnings=list(record.warnings),
        failure_reason=record.failure_reason,
        debug_attempts=record.debug_attempts,
        max_debug_attempts=record.max_debug_attempts,
    )


def execution_record(result: AgentResult) -> AgentExecutionRecord:
    return AgentExecutionRecord(
        execution_id=str(uuid4()),
        task_id=str(result.task_id),
        agent_name=result.agent_name,
        started_at=result.started_at,
        completed_at=result.completed_at,
        success=result.success,
        summary=result.summary,
        artifacts=list(result.artifacts),
        metadata_json=secret_free(result.metadata),
    )


def execution_domain(record: AgentExecutionRecord) -> AgentExecution:
    return AgentExecution(
        execution_id=UUID(record.execution_id),
        task_id=UUID(record.task_id),
        agent_name=record.agent_name,
        started_at=utc(record.started_at),
        completed_at=utc(record.completed_at),
        success=record.success,
        summary=record.summary,
        artifacts=list(record.artifacts),
        metadata=dict(record.metadata_json),
    )


_SECRET_WORDS = ("api_key", "apikey", "secret", "password", "token", "credential")
_UNBOUNDED_WORDS = (
    "prompt",
    "messages",
    "complete_content",
    "source",
    "full_log",
    "stdout",
    "stderr",
)


def secret_free(value: Any, *, key: str = "") -> Any:
    """Remove secret, prompt, source, and unbounded log fields before persistence."""
    value = redact_secrets(value)
    lowered = key.lower()
    if any(word in lowered for word in (*_SECRET_WORDS, *_UNBOUNDED_WORDS)):
        return None
    if isinstance(value, dict):
        return {
            str(item_key): secret_free(item_value, key=str(item_key))
            for item_key, item_value in value.items()
            if not any(
                word in str(item_key).lower() for word in (*_SECRET_WORDS, *_UNBOUNDED_WORDS)
            )
        }
    if isinstance(value, list):
        return [secret_free(item, key=key) for item in value]
    if isinstance(value, str):
        return value[:4_000]
    return value


__all__ = [
    "AgentExecution",
    "Project",
    "execution_domain",
    "execution_record",
    "migration_event_record",
    "migration_run_domain",
    "migration_run_record",
    "multi_run_domain",
    "multi_run_record",
    "project_domain",
    "project_record",
    "repair_attempt_domain",
    "repair_attempt_record",
    "secret_free",
    "task_domain",
    "task_record",
    "utc",
]
