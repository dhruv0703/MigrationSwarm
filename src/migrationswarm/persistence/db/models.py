"""SQLAlchemy persistence models kept separate from the domain models."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base metadata for Alembic and isolated test databases."""


class ProjectRecord(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    repository_path: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    tasks: Mapped[list[TaskRecord]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    migration_runs: Mapped[list[MigrationRunRecord]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class TaskRecord(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_type: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    assigned_agent: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    project: Mapped[ProjectRecord] = relationship(back_populates="tasks")
    dependencies: Mapped[list[TaskDependencyRecord]] = relationship(
        foreign_keys="TaskDependencyRecord.task_id",
        cascade="all, delete-orphan",
        order_by="TaskDependencyRecord.dependency_id",
    )


class TaskDependencyRecord(Base):
    __tablename__ = "task_dependencies"
    __table_args__ = (UniqueConstraint("task_id", "dependency_id", name="uq_task_dependency"),)

    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    dependency_id: Mapped[str] = mapped_column(String(36), primary_key=True)


class MigrationRunRecord(Base):
    __tablename__ = "migration_runs"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    repository_root: Mapped[str] = mapped_column(Text, nullable=False)
    selected_service: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    worktree_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    current_stage: Mapped[str] = mapped_column(String(100), nullable=False)
    generated_files: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    verification_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    final_task_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    warnings: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    debug_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_debug_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    project: Mapped[ProjectRecord | None] = relationship(back_populates="migration_runs")
    events: Mapped[list[MigrationRunEventRecord]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="MigrationRunEventRecord.sequence",
    )


class MigrationRunEventRecord(Base):
    __tablename__ = "migration_run_events"
    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_run_event_sequence"),)

    event_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("migration_runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    stage: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    run: Mapped[MigrationRunRecord] = relationship(back_populates="events")


class AgentExecutionRecord(Base):
    __tablename__ = "agent_executions"

    execution_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid4())
    )
    task_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    agent_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    success: Mapped[bool] = mapped_column(nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    artifacts: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class RepairAttemptRecord(Base):
    __tablename__ = "repair_attempts"
    __table_args__ = (
        UniqueConstraint(
            "original_task_id", "attempt_number", name="uq_repair_original_attempt"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    original_task_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    debug_task_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    failure_category: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    verification_artifact_before: Mapped[str | None] = mapped_column(Text, nullable=True)
    verification_artifact_after: Mapped[str | None] = mapped_column(Text, nullable=True)
    debug_artifact: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_provider: Mapped[str | None] = mapped_column(String(120), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(255), nullable=True)


class MultiMigrationRunRecord(Base):
    __tablename__ = "multi_migration_runs"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    repository_root: Mapped[str] = mapped_column(Text, nullable=False)
    selected_services: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    dependencies: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    service_states: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    warnings: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)


__all__ = [
    "AgentExecutionRecord",
    "Base",
    "MigrationRunEventRecord",
    "MigrationRunRecord",
    "MultiMigrationRunRecord",
    "ProjectRecord",
    "RepairAttemptRecord",
    "TaskDependencyRecord",
    "TaskRecord",
]
