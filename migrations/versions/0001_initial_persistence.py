"""Initial durable persistence schema."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial_persistence"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("repository_path", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("task_type", sa.String(length=80), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("assigned_agent", sa.String(length=255), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tasks_project_id", "tasks", ["project_id"])
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index("ix_tasks_assigned_agent", "tasks", ["assigned_agent"])
    op.create_table(
        "task_dependencies",
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("dependency_id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("task_id", "dependency_id"),
        sa.UniqueConstraint("task_id", "dependency_id", name="uq_task_dependency"),
    )
    op.create_table(
        "migration_runs",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("repository_root", sa.Text(), nullable=False),
        sa.Column("selected_service", sa.String(length=255), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("worktree_path", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("current_stage", sa.String(length=100), nullable=False),
        sa.Column("generated_files", sa.JSON(), nullable=False),
        sa.Column("verification_status", sa.String(length=50), nullable=True),
        sa.Column("final_task_status", sa.String(length=50), nullable=True),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("debug_attempts", sa.Integer(), nullable=False),
        sa.Column("max_debug_attempts", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index("ix_migration_runs_project_id", "migration_runs", ["project_id"])
    op.create_index("ix_migration_runs_selected_service", "migration_runs", ["selected_service"])
    op.create_index("ix_migration_runs_task_id", "migration_runs", ["task_id"])
    op.create_index("ix_migration_runs_status", "migration_runs", ["status"])
    op.create_table(
        "migration_run_events",
        sa.Column("event_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stage", sa.String(length=100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["migration_runs.run_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_run_event_sequence"),
    )
    op.create_index("ix_migration_run_events_run_id", "migration_run_events", ["run_id"])
    op.create_table(
        "agent_executions",
        sa.Column("execution_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("agent_name", sa.String(length=255), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("artifacts", sa.JSON(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("execution_id"),
    )
    op.create_index("ix_agent_executions_task_id", "agent_executions", ["task_id"])
    op.create_index("ix_agent_executions_agent_name", "agent_executions", ["agent_name"])
    op.create_table(
        "repair_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("original_task_id", sa.String(length=36), nullable=False),
        sa.Column("debug_task_id", sa.String(length=36), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("failure_category", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verification_artifact_before", sa.Text(), nullable=True),
        sa.Column("verification_artifact_after", sa.Text(), nullable=True),
        sa.Column("debug_artifact", sa.Text(), nullable=True),
        sa.Column("model_provider", sa.String(length=120), nullable=True),
        sa.Column("model_name", sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("debug_task_id"),
        sa.UniqueConstraint(
            "original_task_id", "attempt_number", name="uq_repair_original_attempt"
        ),
    )
    op.create_index("ix_repair_attempts_original_task_id", "repair_attempts", ["original_task_id"])
    op.create_index("ix_repair_attempts_status", "repair_attempts", ["status"])
    op.create_table(
        "multi_migration_runs",
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("repository_root", sa.Text(), nullable=False),
        sa.Column("selected_services", sa.JSON(), nullable=False),
        sa.Column("dependencies", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("service_states", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index("ix_multi_migration_runs_project_id", "multi_migration_runs", ["project_id"])
    op.create_index("ix_multi_migration_runs_status", "multi_migration_runs", ["status"])


def downgrade() -> None:
    op.drop_index("ix_multi_migration_runs_status", table_name="multi_migration_runs")
    op.drop_index("ix_multi_migration_runs_project_id", table_name="multi_migration_runs")
    op.drop_table("multi_migration_runs")
    op.drop_index("ix_repair_attempts_status", table_name="repair_attempts")
    op.drop_index("ix_repair_attempts_original_task_id", table_name="repair_attempts")
    op.drop_table("repair_attempts")
    op.drop_index("ix_agent_executions_agent_name", table_name="agent_executions")
    op.drop_index("ix_agent_executions_task_id", table_name="agent_executions")
    op.drop_table("agent_executions")
    op.drop_index("ix_migration_run_events_run_id", table_name="migration_run_events")
    op.drop_table("migration_run_events")
    op.drop_index("ix_migration_runs_status", table_name="migration_runs")
    op.drop_index("ix_migration_runs_task_id", table_name="migration_runs")
    op.drop_index("ix_migration_runs_selected_service", table_name="migration_runs")
    op.drop_index("ix_migration_runs_project_id", table_name="migration_runs")
    op.drop_table("migration_runs")
    op.drop_table("task_dependencies")
    op.drop_index("ix_tasks_assigned_agent", table_name="tasks")
    op.drop_index("ix_tasks_status", table_name="tasks")
    op.drop_index("ix_tasks_project_id", table_name="tasks")
    op.drop_table("tasks")
    op.drop_table("projects")
