"""Small transaction-scoped repositories for durable MigrationSwarm state."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from migrationswarm.core.agents import AgentResult
from migrationswarm.core.orchestrator.models import MigrationRun
from migrationswarm.core.orchestrator.multi_service import MultiServiceMigrationRun
from migrationswarm.core.repair import RepairAttempt
from migrationswarm.core.tasks import Task, TaskStatus
from migrationswarm.persistence.db.models import (
    AgentExecutionRecord,
    MigrationRunEventRecord,
    MigrationRunRecord,
    MultiMigrationRunRecord,
    ProjectRecord,
    RepairAttemptRecord,
    TaskDependencyRecord,
    TaskRecord,
)
from migrationswarm.persistence.exceptions import (
    PersistenceError,
    PersistenceIntegrityError,
    RecordNotFoundError,
)
from migrationswarm.persistence.mapping import (
    AgentExecution,
    Project,
    execution_domain,
    execution_record,
    migration_event_record,
    migration_run_domain,
    migration_run_record,
    multi_run_domain,
    multi_run_record,
    project_domain,
    project_record,
    repair_attempt_domain,
    repair_attempt_record,
    secret_free,
    task_domain,
    task_record,
)

SessionFactory = sessionmaker[Session]


class _Repository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self.session_factory = session_factory

    def _integrity(self, error: IntegrityError) -> PersistenceIntegrityError:
        return PersistenceIntegrityError("Persistence constraint was violated")

    @staticmethod
    def _database_error(error: SQLAlchemyError) -> PersistenceError:
        return PersistenceError("Database operation failed")


class ProjectRepository(_Repository):
    """Persist project identity and repository location."""

    def create(self, project: Project) -> Project:
        try:
            with self.session_factory.begin() as session:
                session.add(project_record(project))
            return project
        except IntegrityError as error:
            raise self._integrity(error) from error
        except SQLAlchemyError as error:
            raise self._database_error(error) from error

    def get(self, project_id: UUID) -> Project | None:
        try:
            with self.session_factory() as session:
                record = session.get(ProjectRecord, str(project_id))
                return project_domain(record) if record else None
        except SQLAlchemyError as error:
            raise self._database_error(error) from error

    def get_required(self, project_id: UUID) -> Project:
        project = self.get(project_id)
        if project is None:
            raise RecordNotFoundError(f"Project does not exist: {project_id}")
        return project

    def find_by_repository_path(self, repository_path: str) -> Project | None:
        """Return the project registered for a repository path, if present."""
        try:
            with self.session_factory() as session:
                record = session.scalar(
                    select(ProjectRecord).where(ProjectRecord.repository_path == repository_path)
                )
                return project_domain(record) if record else None
        except SQLAlchemyError as error:
            raise self._database_error(error) from error

    def update(self, project: Project) -> Project:
        try:
            with self.session_factory.begin() as session:
                record = session.get(ProjectRecord, str(project.id))
                if record is None:
                    raise RecordNotFoundError(f"Project does not exist: {project.id}")
                record.name = project.name
                record.repository_path = str(project.repository_path)
                record.created_at = project.created_at
                record.updated_at = project.updated_at
            return project
        except RecordNotFoundError:
            raise
        except SQLAlchemyError as error:
            raise self._database_error(error) from error


class TaskRepository(_Repository):
    """Persist tasks and their dependency edges separately."""

    def create(self, task: Task) -> Task:
        try:
            with self.session_factory.begin() as session:
                self._validate_dependencies(session, task)
                record = task_record(task)
                record.dependencies = [
                    TaskDependencyRecord(task_id=str(task.id), dependency_id=str(dependency))
                    for dependency in task.dependencies
                ]
                session.add(record)
            return task
        except IntegrityError as error:
            raise self._integrity(error) from error
        except PersistenceIntegrityError:
            raise
        except SQLAlchemyError as error:
            raise self._database_error(error) from error

    def get(self, task_id: UUID) -> Task | None:
        try:
            with self.session_factory() as session:
                record = session.get(TaskRecord, str(task_id))
                if record is None:
                    return None
                dependencies = session.scalars(
                    select(TaskDependencyRecord.dependency_id)
                    .where(TaskDependencyRecord.task_id == str(task_id))
                    .order_by(TaskDependencyRecord.dependency_id)
                ).all()
                return task_domain(record, list(dependencies))
        except SQLAlchemyError as error:
            raise self._database_error(error) from error

    def get_required(self, task_id: UUID) -> Task:
        task = self.get(task_id)
        if task is None:
            raise RecordNotFoundError(f"Task does not exist: {task_id}")
        return task

    def update(self, task: Task) -> Task:
        try:
            with self.session_factory.begin() as session:
                record = session.get(TaskRecord, str(task.id))
                if record is None:
                    raise RecordNotFoundError(f"Task does not exist: {task.id}")
                self._validate_dependencies(session, task)
                record.project_id = str(task.project_id)
                record.task_type = task.task_type.value
                record.title = task.title
                record.description = task.description
                record.status = task.status.value
                record.assigned_agent = task.assigned_agent
                record.metadata_json = secret_free(task.metadata)
                record.attempt = task.attempt
                record.max_attempts = task.max_attempts
                record.created_at = task.created_at
                record.updated_at = task.updated_at
                session.execute(
                    delete(TaskDependencyRecord).where(TaskDependencyRecord.task_id == str(task.id))
                )
                record.dependencies = [
                    TaskDependencyRecord(task_id=str(task.id), dependency_id=str(dependency))
                    for dependency in task.dependencies
                ]
            return task
        except RecordNotFoundError:
            raise
        except IntegrityError as error:
            raise self._integrity(error) from error
        except PersistenceIntegrityError:
            raise
        except SQLAlchemyError as error:
            raise self._database_error(error) from error

    def list(
        self,
        *,
        project_id: UUID | None = None,
        status: TaskStatus | None = None,
        assigned_agent: str | None = None,
    ) -> list[Task]:
        try:
            with self.session_factory() as session:
                query = select(TaskRecord).order_by(TaskRecord.created_at, TaskRecord.id)
                if project_id is not None:
                    query = query.where(TaskRecord.project_id == str(project_id))
                if status is not None:
                    query = query.where(TaskRecord.status == status.value)
                if assigned_agent is not None:
                    query = query.where(TaskRecord.assigned_agent == assigned_agent)
                records = session.scalars(query).all()
                result: list[Task] = []
                for record in records:
                    dependencies = session.scalars(
                        select(TaskDependencyRecord.dependency_id)
                        .where(TaskDependencyRecord.task_id == record.id)
                        .order_by(TaskDependencyRecord.dependency_id)
                    ).all()
                    result.append(task_domain(record, list(dependencies)))
                return result
        except SQLAlchemyError as error:
            raise self._database_error(error) from error

    @staticmethod
    def _validate_dependencies(session: Session, task: Task) -> None:
        if not task.dependencies:
            return
        expected = {str(value) for value in task.dependencies}
        existing = set(
            session.scalars(select(TaskRecord.id).where(TaskRecord.id.in_(expected))).all()
        )
        missing = expected - existing
        if missing:
            raise PersistenceIntegrityError("Task dependency does not exist: " + sorted(missing)[0])


class MigrationRunRepository(_Repository):
    """Persist migration runs and their ordered events."""

    def create(self, run: MigrationRun, *, project_id: UUID | None = None) -> MigrationRun:
        try:
            with self.session_factory.begin() as session:
                record = migration_run_record(run, project_id)
                record.events = [
                    migration_event_record(run.run_id, event, sequence)
                    for sequence, event in enumerate(run.events)
                ]
                session.add(record)
            return run
        except IntegrityError as error:
            raise self._integrity(error) from error
        except SQLAlchemyError as error:
            raise self._database_error(error) from error

    def get(self, run_id: UUID) -> MigrationRun | None:
        try:
            with self.session_factory() as session:
                record = session.get(MigrationRunRecord, str(run_id))
                if record is None:
                    return None
                events = session.scalars(
                    select(MigrationRunEventRecord)
                    .where(MigrationRunEventRecord.run_id == str(run_id))
                    .order_by(MigrationRunEventRecord.sequence)
                ).all()
                return migration_run_domain(record, list(events))
        except SQLAlchemyError as error:
            raise self._database_error(error) from error

    def get_required(self, run_id: UUID) -> MigrationRun:
        run = self.get(run_id)
        if run is None:
            raise RecordNotFoundError(f"Migration run does not exist: {run_id}")
        return run

    def update(self, run: MigrationRun, *, project_id: UUID | None = None) -> MigrationRun:
        try:
            with self.session_factory.begin() as session:
                record = session.get(MigrationRunRecord, str(run.run_id))
                if record is None:
                    raise RecordNotFoundError(f"Migration run does not exist: {run.run_id}")
                updated = migration_run_record(run, project_id)
                for field in (
                    "project_id",
                    "repository_root",
                    "selected_service",
                    "task_id",
                    "worktree_path",
                    "started_at",
                    "completed_at",
                    "status",
                    "current_stage",
                    "generated_files",
                    "verification_status",
                    "final_task_status",
                    "warnings",
                    "failure_reason",
                    "debug_attempts",
                    "max_debug_attempts",
                ):
                    setattr(record, field, getattr(updated, field))
                session.execute(
                    delete(MigrationRunEventRecord).where(
                        MigrationRunEventRecord.run_id == str(run.run_id)
                    )
                )
                record.events = [
                    migration_event_record(run.run_id, event, sequence)
                    for sequence, event in enumerate(run.events)
                ]
            return run
        except RecordNotFoundError:
            raise
        except SQLAlchemyError as error:
            raise self._database_error(error) from error

    def latest_for_project_service(
        self, project_id: UUID, service_name: str
    ) -> MigrationRun | None:
        try:
            with self.session_factory() as session:
                record = session.scalars(
                    select(MigrationRunRecord)
                    .where(
                        MigrationRunRecord.project_id == str(project_id),
                        MigrationRunRecord.selected_service == service_name,
                    )
                    .order_by(MigrationRunRecord.started_at.desc())
                    .limit(1)
                ).first()
                if record is None:
                    return None
                events = session.scalars(
                    select(MigrationRunEventRecord)
                    .where(MigrationRunEventRecord.run_id == record.run_id)
                    .order_by(MigrationRunEventRecord.sequence)
                ).all()
                return migration_run_domain(record, list(events))
        except SQLAlchemyError as error:
            raise self._database_error(error) from error


class AgentExecutionRepository(_Repository):
    """Persist bounded agent execution results without prompts or secrets."""

    def create(self, execution: AgentExecution | AgentResult) -> AgentExecution:
        domain = execution if isinstance(execution, AgentExecution) else None
        record = (
            execution_record(execution)
            if isinstance(execution, AgentResult)
            else AgentExecutionRecord(
                execution_id=str(execution.execution_id),
                task_id=str(execution.task_id),
                agent_name=execution.agent_name,
                started_at=execution.started_at,
                completed_at=execution.completed_at,
                success=execution.success,
                summary=execution.summary,
                artifacts=list(execution.artifacts),
                metadata_json=secret_free(execution.metadata),
            )
        )
        if record.execution_id is None:
            from uuid import uuid4

            record.execution_id = str(uuid4())
        if domain is None:
            domain = execution_domain(record)
        try:
            with self.session_factory.begin() as session:
                session.add(record)
            return domain
        except IntegrityError as error:
            raise self._integrity(error) from error
        except SQLAlchemyError as error:
            raise self._database_error(error) from error

    def get(self, execution_id: UUID) -> AgentExecution | None:
        try:
            with self.session_factory() as session:
                record = session.get(AgentExecutionRecord, str(execution_id))
                return execution_domain(record) if record else None
        except SQLAlchemyError as error:
            raise self._database_error(error) from error

    def list_for_task(self, task_id: UUID) -> list[AgentExecution]:
        try:
            with self.session_factory() as session:
                records = session.scalars(
                    select(AgentExecutionRecord)
                    .where(AgentExecutionRecord.task_id == str(task_id))
                    .order_by(AgentExecutionRecord.started_at, AgentExecutionRecord.execution_id)
                ).all()
                return [execution_domain(record) for record in records]
        except SQLAlchemyError as error:
            raise self._database_error(error) from error


class RepairAttemptRepository(_Repository):
    """Persist one row per explicit, bounded DEBUG task attempt."""

    def create(self, attempt: RepairAttempt) -> RepairAttempt:
        try:
            with self.session_factory.begin() as session:
                session.add(repair_attempt_record(attempt))
            return attempt
        except IntegrityError as error:
            raise self._integrity(error) from error
        except SQLAlchemyError as error:
            raise self._database_error(error) from error

    def get(self, attempt_id: UUID) -> RepairAttempt | None:
        try:
            with self.session_factory() as session:
                record = session.get(RepairAttemptRecord, str(attempt_id))
                return repair_attempt_domain(record) if record else None
        except SQLAlchemyError as error:
            raise self._database_error(error) from error

    def update(self, attempt: RepairAttempt) -> RepairAttempt:
        try:
            with self.session_factory.begin() as session:
                record = session.get(RepairAttemptRecord, str(attempt.id))
                if record is None:
                    raise RecordNotFoundError(f"Repair attempt does not exist: {attempt.id}")
                updated = repair_attempt_record(attempt)
                for field in (
                    "original_task_id", "debug_task_id", "attempt_number", "failure_category",
                    "status", "started_at", "completed_at", "verification_artifact_before",
                    "verification_artifact_after", "debug_artifact", "model_provider", "model_name",
                ):
                    setattr(record, field, getattr(updated, field))
            return attempt
        except RecordNotFoundError:
            raise
        except SQLAlchemyError as error:
            raise self._database_error(error) from error

    def list_for_original(self, original_task_id: UUID) -> list[RepairAttempt]:
        try:
            with self.session_factory() as session:
                records = session.scalars(
                    select(RepairAttemptRecord)
                    .where(RepairAttemptRecord.original_task_id == str(original_task_id))
                    .order_by(RepairAttemptRecord.attempt_number, RepairAttemptRecord.id)
                ).all()
                return [repair_attempt_domain(record) for record in records]
        except SQLAlchemyError as error:
            raise self._database_error(error) from error

    def latest_for_original(self, original_task_id: UUID) -> RepairAttempt | None:
        attempts = self.list_for_original(original_task_id)
        return attempts[-1] if attempts else None

    def count_for_original(self, original_task_id: UUID) -> int:
        return len(self.list_for_original(original_task_id))


class MultiMigrationRunRepository(_Repository):
    """Persist one bounded multi-service run and its per-service snapshots."""

    def create(self, run: MultiServiceMigrationRun) -> MultiServiceMigrationRun:
        try:
            with self.session_factory.begin() as session:
                session.add(multi_run_record(run))
            return run
        except IntegrityError as error:
            raise self._integrity(error) from error
        except SQLAlchemyError as error:
            raise self._database_error(error) from error

    def get(self, run_id: UUID) -> MultiServiceMigrationRun | None:
        try:
            with self.session_factory() as session:
                record = session.get(MultiMigrationRunRecord, str(run_id))
                return multi_run_domain(record) if record else None
        except SQLAlchemyError as error:
            raise self._database_error(error) from error

    def update(self, run: MultiServiceMigrationRun) -> MultiServiceMigrationRun:
        try:
            with self.session_factory.begin() as session:
                record = session.get(MultiMigrationRunRecord, str(run.run_id))
                if record is None:
                    raise RecordNotFoundError(f"Multi-service run does not exist: {run.run_id}")
                updated = multi_run_record(run)
                for field in (
                    "project_id", "repository_root", "selected_services", "dependencies",
                    "status", "service_states", "started_at", "completed_at", "warnings",
                ):
                    setattr(record, field, getattr(updated, field))
            return run
        except RecordNotFoundError:
            raise
        except SQLAlchemyError as error:
            raise self._database_error(error) from error

    def latest_for_repository(self, repository_root: str) -> MultiServiceMigrationRun | None:
        try:
            with self.session_factory() as session:
                record = session.scalars(
                    select(MultiMigrationRunRecord)
                    .where(MultiMigrationRunRecord.repository_root == repository_root)
                    .order_by(MultiMigrationRunRecord.started_at.desc())
                    .limit(1)
                ).first()
                return multi_run_domain(record) if record else None
        except SQLAlchemyError as error:
            raise self._database_error(error) from error


__all__ = [
    "AgentExecutionRepository",
    "MigrationRunRepository",
    "MultiMigrationRunRepository",
    "ProjectRepository",
    "RepairAttemptRepository",
    "TaskRepository",
]
