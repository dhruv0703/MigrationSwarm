"""Optional persistence facade used as a hook around existing domain operations."""

from __future__ import annotations

from uuid import UUID

from migrationswarm.core.agents import AgentResult
from migrationswarm.core.orchestrator.models import MigrationRun
from migrationswarm.core.repair import RepairAttempt
from migrationswarm.core.tasks import Task, TaskStateMachine, TaskStatus
from migrationswarm.persistence.db.repositories import (
    AgentExecutionRepository,
    MigrationRunRepository,
    ProjectRepository,
    RepairAttemptRepository,
    TaskRepository,
)
from migrationswarm.persistence.mapping import Project


class MigrationStateService:
    """Persist state after domain operations without replacing domain services."""

    def __init__(
        self,
        project_repository: ProjectRepository,
        task_repository: TaskRepository,
        migration_run_repository: MigrationRunRepository,
        agent_execution_repository: AgentExecutionRepository,
        repair_attempt_repository: RepairAttemptRepository | None = None,
    ) -> None:
        self.projects = project_repository
        self.tasks = task_repository
        self.runs = migration_run_repository
        self.executions = agent_execution_repository
        self.repair_attempts = repair_attempt_repository

    def persist_project(self, project: Project) -> Project:
        existing = self.projects.get(project.id)
        return self.projects.update(project) if existing else self.projects.create(project)

    def persist_task(self, task: Task) -> Task:
        existing = self.tasks.get(task.id)
        return self.tasks.update(task) if existing else self.tasks.create(task)

    def persist_run(self, run: MigrationRun, *, project_id: UUID | None = None) -> MigrationRun:
        existing = self.runs.get(run.run_id)
        if existing:
            return self.runs.update(run, project_id=project_id)
        return self.runs.create(run, project_id=project_id)

    def persist_agent_result(self, result: AgentResult) -> None:
        self.executions.create(result)

    def persist_repair_attempt(self, attempt: RepairAttempt) -> RepairAttempt:
        if self.repair_attempts is None:
            raise RuntimeError("Repair attempt persistence is not configured")
        existing = self.repair_attempts.get(attempt.id)
        return (
            self.repair_attempts.update(attempt)
            if existing
            else self.repair_attempts.create(attempt)
        )

    def transition_task(self, task: Task, target: TaskStatus) -> Task:
        """Apply the existing state machine, then persist the resulting state."""
        TaskStateMachine.transition(task, target)
        self.persist_task(task)
        return task


__all__ = ["MigrationStateService"]
