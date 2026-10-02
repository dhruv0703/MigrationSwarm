"""SQLite and fake-Redis tests for durable and transient persistence adapters."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import fakeredis
import pytest
from sqlalchemy.exc import IntegrityError
from typer.testing import CliRunner

import migrationswarm.cli.main as cli_module
from migrationswarm.core.agents import AgentResult
from migrationswarm.core.orchestrator import (
    MigrationRun,
    MigrationRunEventType,
    MigrationRunStatus,
)
from migrationswarm.core.tasks import Task, TaskStatus, TaskType
from migrationswarm.persistence.db import Database
from migrationswarm.persistence.db.migrations import current_revision, upgrade_database
from migrationswarm.persistence.db.models import ProjectRecord
from migrationswarm.persistence.db.repositories import (
    AgentExecutionRepository,
    MigrationRunRepository,
    ProjectRepository,
    TaskRepository,
)
from migrationswarm.persistence.exceptions import PersistenceIntegrityError, RecordNotFoundError
from migrationswarm.persistence.mapping import Project
from migrationswarm.persistence.redis import AgentHeartbeat, ReadyTaskQueue, TaskLock
from migrationswarm.persistence.service import MigrationStateService

RUNNER = CliRunner()


@pytest.fixture
def database(tmp_path: Path) -> Database:
    value = Database(f"sqlite:///{tmp_path / 'state.db'}")
    value.create_all_for_tests()
    return value


def make_project() -> Project:
    now = datetime.now(UTC)
    return Project(
        id=uuid4(),
        name="Example project",
        repository_path=Path("C:/repositories/example"),
        created_at=now,
        updated_at=now,
    )


def make_task(
    project_id: UUID,
    *,
    status: TaskStatus = TaskStatus.PENDING,
    dependencies: list[UUID] | None = None,
) -> Task:
    return Task(
        project_id=project_id,
        task_type=TaskType.REPOSITORY_ANALYSIS,
        title="Analyze repository",
        description="Create repository inventory.",
        status=status,
        dependencies=list(dependencies or []),
        assigned_agent="repository_analysis",
    )


def make_run(task: Task, project_id: UUID) -> MigrationRun:
    run = MigrationRun(
        repository_root=Path("C:/repositories/example"),
        selected_service="Greeting",
        task_id=task.id,
        project_id=project_id,
        status=MigrationRunStatus.EXTRACTING,
    )
    run.record(MigrationRunEventType.EXTRACTION_STARTED, "extracting")
    run.record(MigrationRunEventType.EXTRACTION_COMPLETED, "extracting", {"count": 2})
    return run


def test_project_task_and_dependency_round_trip(database: Database) -> None:
    project = make_project()
    projects = ProjectRepository(database.session_factory)
    tasks = TaskRepository(database.session_factory)
    projects.create(project)
    root = make_task(project.id)
    child = make_task(project.id, dependencies=[root.id])
    tasks.create(root)
    tasks.create(child)

    loaded = tasks.get_required(child.id)
    assert loaded.id == child.id
    assert loaded.project_id == project.id
    assert loaded.dependencies == [root.id]
    assert projects.get_required(project.id).repository_path == project.repository_path


def test_task_queries_and_status_update_persist(database: Database) -> None:
    project = make_project()
    projects = ProjectRepository(database.session_factory)
    tasks = TaskRepository(database.session_factory)
    projects.create(project)
    task = make_task(project.id)
    tasks.create(task)
    task.status = TaskStatus.READY
    tasks.update(task)

    assert tasks.list(project_id=project.id, status=TaskStatus.READY)[0].id == task.id
    assert tasks.list(assigned_agent=task.assigned_agent)[0].status is TaskStatus.READY


def test_missing_records_and_duplicate_errors_are_explicit(database: Database) -> None:
    projects = ProjectRepository(database.session_factory)
    project = make_project()
    assert projects.get(project.id) is None
    with pytest.raises(RecordNotFoundError):
        projects.get_required(project.id)
    projects.create(project)
    with pytest.raises(PersistenceIntegrityError):
        projects.create(project)


def test_unknown_task_dependency_is_rejected(database: Database) -> None:
    project = make_project()
    ProjectRepository(database.session_factory).create(project)
    with pytest.raises(PersistenceIntegrityError):
        TaskRepository(database.session_factory).create(
            make_task(project.id, dependencies=[uuid4()])
        )


def test_migration_run_and_event_order_round_trip(database: Database) -> None:
    project = make_project()
    ProjectRepository(database.session_factory).create(project)
    task = make_task(project.id)
    TaskRepository(database.session_factory).create(task)
    runs = MigrationRunRepository(database.session_factory)
    run = make_run(task, project.id)
    runs.create(run)
    loaded = runs.get_required(run.run_id)

    assert loaded.project_id == project.id
    assert [event.event_type for event in loaded.events] == [
        MigrationRunEventType.EXTRACTION_STARTED,
        MigrationRunEventType.EXTRACTION_COMPLETED,
    ]
    assert all(event.occurred_at.tzinfo is not None for event in loaded.events)
    latest = runs.latest_for_project_service(project.id, "Greeting")
    assert latest is not None
    assert latest.run_id == run.run_id


def test_agent_execution_is_secret_free_and_round_trips(database: Database) -> None:
    task_id = uuid4()
    result = AgentResult(
        task_id=task_id,
        agent_name="test-agent",
        success=True,
        summary="Completed safely.",
        artifacts=[".migrationswarm/result.json"],
        metadata={
            "model_provider": "groq",
            "model_name": "safe-model",
            "api_key": "do-not-store",
            "prompt": "do-not-store",
        },
    )
    repository = AgentExecutionRepository(database.session_factory)
    stored = repository.create(result)
    loaded = repository.get(stored.execution_id)
    assert loaded is not None
    assert loaded.metadata == {"model_provider": "groq", "model_name": "safe-model"}
    assert "do-not-store" not in str(loaded.model_dump())


def test_transaction_rolls_back_and_database_errors_propagate(database: Database) -> None:
    project = make_project()
    repository = ProjectRepository(database.session_factory)
    repository.create(project)
    with pytest.raises(IntegrityError):
        with database.session() as session:
            session.add(
                ProjectRecord(
                    id=str(project.id),
                    name="duplicate",
                    repository_path="duplicate",
                    created_at=project.created_at,
                    updated_at=project.updated_at,
                )
            )
    assert repository.get_required(project.id).name == project.name


def test_state_service_persists_lifecycle_updates(database: Database) -> None:
    projects = ProjectRepository(database.session_factory)
    tasks = TaskRepository(database.session_factory)
    runs = MigrationRunRepository(database.session_factory)
    executions = AgentExecutionRepository(database.session_factory)
    service = MigrationStateService(projects, tasks, runs, executions)
    project = make_project()
    task = make_task(project.id)
    run = make_run(task, project.id)

    service.persist_project(project)
    service.persist_task(task)
    service.persist_run(run)
    task.status = TaskStatus.READY
    service.persist_task(task)
    run.status = MigrationRunStatus.COMPLETED
    service.persist_run(run)

    assert tasks.get_required(task.id).status is TaskStatus.READY
    assert runs.get_required(run.run_id).status is MigrationRunStatus.COMPLETED


def test_redis_queue_lock_and_heartbeat() -> None:
    client = fakeredis.FakeRedis(decode_responses=True)
    task_id = uuid4()
    queue = ReadyTaskQueue(client)
    assert queue.enqueue(task_id) is True
    assert queue.enqueue(task_id) is False
    assert queue.dequeue() == task_id
    assert queue.dequeue() is None

    lock = TaskLock(client)
    owner = lock.acquire(task_id, owner_token="owner", ttl_seconds=30)
    assert owner == "owner"
    assert lock.acquire(task_id, owner_token="other", ttl_seconds=30) is None
    assert lock.release(task_id, "other") is False
    assert lock.ttl(task_id) > 0
    assert lock.release(task_id, "owner") is True

    heartbeat = AgentHeartbeat(client)
    value = heartbeat.set_heartbeat("agent", task_id=task_id, ttl_seconds=30)
    assert heartbeat.get_heartbeat("agent") == value
    assert heartbeat.ttl("agent") > 0
    client.delete("migrationswarm:heartbeat:agent")
    assert heartbeat.get_heartbeat("agent") is None


def test_alembic_upgrade_creates_schema(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'alembic.db'}"
    upgrade_database(url)
    assert current_revision(url) == "0001_initial_persistence"


def test_cli_database_and_redis_status_do_not_print_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSettings:
        database_url = "sqlite:///:memory:"
        redis_url = "redis://:secret@localhost:6379/0"

    class FakeDatabase:
        def __init__(self, url: str) -> None:
            self.url = url

        def check_connectivity(self) -> bool:
            return True

        def dispose(self) -> None:
            pass

    class FakeRedis:
        def __init__(self, url: str) -> None:
            self.url = url

        def ping(self) -> bool:
            return True

        def close(self) -> None:
            pass

    monkeypatch.setattr(cli_module, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(cli_module, "Database", FakeDatabase)
    monkeypatch.setattr(cli_module, "current_revision", lambda url: "0001_initial_persistence")
    monkeypatch.setattr(cli_module, "RedisClient", FakeRedis)
    database = RUNNER.invoke(cli_module.app, ["db-status"])
    redis_status = RUNNER.invoke(cli_module.app, ["redis-status"])

    assert database.exit_code == 0
    assert redis_status.exit_code == 0
    assert "0001_initial_persistence" in database.stdout
    assert "secret" not in redis_status.stdout


def test_cli_database_upgrade_invokes_alembic(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeSettings:
        database_url = "sqlite:///./test.db"
        redis_url = "redis://localhost:6379/0"

    calls: list[str] = []
    monkeypatch.setattr(cli_module, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(cli_module, "upgrade_database", calls.append)
    result = RUNNER.invoke(cli_module.app, ["db-upgrade"])

    assert result.exit_code == 0
    assert calls == [FakeSettings.database_url]
