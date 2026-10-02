"""Tests for deterministic verification dispatch and worker lifecycle."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import fakeredis
import pytest
from typer.testing import CliRunner

import migrationswarm.cli.main as cli_module
from migrationswarm.agents.build_verification import (
    BuildCommandResult,
    BuildSystem,
    BuildVerificationResult,
    VerificationStatus,
)
from migrationswarm.agents.build_verification import (
    TestSummary as VerificationTestSummary,
)
from migrationswarm.core.agents import (
    AgentCapability,
    AgentContext,
    AgentRegistry,
    AgentResult,
    BaseAgent,
)
from migrationswarm.core.tasks.enums import TaskStatus, TaskType
from migrationswarm.core.tasks.models import Task
from migrationswarm.core.tasks.state_machine import TaskStateMachine
from migrationswarm.core.verification import VerificationCoordinator
from migrationswarm.persistence.db import Database
from migrationswarm.persistence.db.repositories import (
    AgentExecutionRepository,
    ProjectRepository,
    TaskRepository,
)
from migrationswarm.persistence.mapping import Project
from migrationswarm.persistence.redis import AgentHeartbeat, ReadyTaskQueue, TaskLock
from migrationswarm.workers import SwarmCoordinator, WorkerConfig
from migrationswarm.workers.dispatcher import TaskDispatcher
from migrationswarm.workers.verification import (
    EvidenceLoadResult,
    VerificationDispatcher,
    VerificationEvidenceLoader,
    VerificationQueue,
    VerificationWorker,
    VerificationWorkerManager,
)


class SuccessAgent(BaseAgent):
    name = "verification-test-agent"
    capabilities = frozenset({AgentCapability.REPOSITORY_ANALYSIS})

    def execute(self, task: Task, context: AgentContext) -> AgentResult:
        return AgentResult(
            task_id=task.id,
            agent_name=self.name,
            success=True,
            summary="deterministic execution",
        )


RUNNER = CliRunner()


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'verification-workers.db'}")
    database.create_all_for_tests()
    return database


def make_project() -> Project:
    now = datetime.now(UTC)
    return Project(
        id=uuid4(),
        name="verification project",
        repository_path=Path("C:/repositories/verification-project"),
        created_at=now,
        updated_at=now,
    )


def make_task(
    project_id: UUID,
    *,
    status: TaskStatus = TaskStatus.VERIFYING,
    dependencies: list[UUID] | None = None,
    agent: str = "verification-test-agent",
) -> Task:
    return Task(
        project_id=project_id,
        task_type=TaskType.REPOSITORY_ANALYSIS,
        title="verification task",
        description="deterministic verification test",
        status=status,
        dependencies=list(dependencies or []),
        assigned_agent=agent,
    )


def persist(database: Database, project: Project, tasks: list[Task]) -> None:
    ProjectRepository(database.session_factory).create(project)
    repository = TaskRepository(database.session_factory)
    for task in tasks:
        repository.create(task)


def write_evidence(root: Path, task_id: UUID, *, passed: bool = True) -> None:
    artifact_dir = root / ".migrationswarm" / "verification-results"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    json_path = artifact_dir / f"{task_id}.json"
    log_path = artifact_dir / f"{task_id}.log"
    log_path.write_text("verification log\n", encoding="utf-8")
    result = BuildVerificationResult(
        task_id=task_id,
        workspace_path=str(root),
        build_system=BuildSystem.MAVEN,
        commands_run=[
            BuildCommandResult(
                command=["mvn", "test"],
                status=VerificationStatus.PASSED if passed else VerificationStatus.FAILED,
                exit_code=0 if passed else 1,
                duration_ms=1,
            )
        ],
        status=VerificationStatus.PASSED if passed else VerificationStatus.FAILED,
        exit_code=0 if passed else 1,
        duration_ms=1,
        test_summary=VerificationTestSummary(
            tests_run=1,
            failures=0 if passed else 1,
            errors=0,
            skipped=0,
        ),
    )
    payload = result.model_dump(mode="json")
    payload["artifacts"] = {
        "json": str(json_path.relative_to(root).as_posix()),
        "log": str(log_path.relative_to(root).as_posix()),
    }
    json_path.write_text(json.dumps(payload), encoding="utf-8")


def make_verification_worker(
    database: Database,
    client: fakeredis.FakeRedis,
    root: Path,
    *,
    worker_id: str = "verification-1",
) -> VerificationWorker:
    task_repository = TaskRepository(database.session_factory)
    execution_repository = AgentExecutionRepository(database.session_factory)
    queue = VerificationQueue(client)
    return VerificationWorker(
        task_repository,
        execution_repository,
        queue,
        TaskLock(client, prefix="test-verification-lock"),
        AgentHeartbeat(client),
        VerificationCoordinator(root),
        VerificationEvidenceLoader(root, execution_repository),
        config=WorkerConfig(worker_id=worker_id, poll_interval_seconds=0.01),
    )


def test_verification_queue_is_separate_and_deduplicated() -> None:
    client = fakeredis.FakeRedis(decode_responses=True)
    task_id = uuid4()
    ready = ReadyTaskQueue(client)
    verification = VerificationQueue(client)
    assert ready.enqueue(task_id) is True
    assert verification.enqueue(task_id) is True
    assert verification.enqueue(task_id) is False
    assert ready.size() == 1
    assert verification.size() == 1
    assert ready.key != verification.key


def test_verification_dispatches_verifying_tasks_and_suppresses_duplicates(
    database: Database,
) -> None:
    project = make_project()
    task = make_task(project.id)
    persist(database, project, [task])
    client = fakeredis.FakeRedis(decode_responses=True)
    dispatcher = VerificationDispatcher(
        TaskRepository(database.session_factory), VerificationQueue(client)
    )

    first = dispatcher.dispatch(project.id)
    second = dispatcher.dispatch(project.id)
    assert first.enqueued_task_ids == [task.id]
    assert second.enqueued_task_ids == []
    assert (
        TaskRepository(database.session_factory).get_required(task.id).status
        is TaskStatus.VERIFYING
    )


def test_stale_non_verifying_item_is_skipped(database: Database, tmp_path: Path) -> None:
    project = make_project()
    task = make_task(project.id, status=TaskStatus.READY)
    persist(database, project, [task])
    client = fakeredis.FakeRedis(decode_responses=True)
    queue = VerificationQueue(client)
    queue.enqueue(task.id)
    result = make_verification_worker(database, client, tmp_path).run_once()
    assert result.skipped_reason == "database_status_ready"


@pytest.mark.parametrize(
    "passed, expected_status",
    [(True, TaskStatus.COMPLETED), (False, TaskStatus.FAILED)],
)
def test_verification_worker_applies_pass_or_fail_and_persists_decision(
    database: Database,
    tmp_path: Path,
    passed: bool,
    expected_status: TaskStatus,
) -> None:
    project = make_project()
    task = make_task(project.id)
    persist(database, project, [task])
    write_evidence(tmp_path, task.id, passed=passed)
    client = fakeredis.FakeRedis(decode_responses=True)
    queue = VerificationQueue(client)
    queue.enqueue(task.id)
    result = make_verification_worker(database, client, tmp_path).run_once()

    assert result.executed is True
    assert (
        TaskRepository(database.session_factory).get_required(task.id).status
        is expected_status
    )
    assert result.decision_artifact is not None
    assert Path(tmp_path / result.decision_artifact).is_file()
    assert len(AgentExecutionRepository(database.session_factory).list_for_task(task.id)) == 1


@pytest.mark.parametrize("corrupt", [False, True])
def test_missing_or_corrupt_evidence_remains_verifying(
    database: Database,
    tmp_path: Path,
    corrupt: bool,
) -> None:
    project = make_project()
    task = make_task(project.id)
    persist(database, project, [task])
    if corrupt:
        artifact_dir = tmp_path / ".migrationswarm" / "verification-results"
        artifact_dir.mkdir(parents=True)
        (artifact_dir / f"{task.id}.json").write_text("{not-json", encoding="utf-8")
    client = fakeredis.FakeRedis(decode_responses=True)
    VerificationQueue(client).enqueue(task.id)
    result = make_verification_worker(database, client, tmp_path).run_once()

    assert result.decision is not None
    assert result.decision.value == "insufficient_evidence"
    assert (
        TaskRepository(database.session_factory).get_required(task.id).status
        is TaskStatus.VERIFYING
    )
    assert (tmp_path / ".migrationswarm" / "verification-decisions" / f"{task.id}.json").is_file()


def test_lock_is_released_after_pass_fail_and_error(database: Database, tmp_path: Path) -> None:
    project = make_project()
    tasks = [make_task(project.id), make_task(project.id)]
    persist(database, project, tasks)
    write_evidence(tmp_path, tasks[0].id, passed=True)
    write_evidence(tmp_path, tasks[1].id, passed=False)
    client = fakeredis.FakeRedis(decode_responses=True)
    queue = VerificationQueue(client)
    lock = TaskLock(client, prefix="test-verification-lock")
    for task in tasks:
        queue.enqueue(task.id)
        worker = make_verification_worker(database, client, tmp_path)
        worker.run_once()
        assert lock.acquire(task.id, owner_token="next-owner", ttl_seconds=30) == "next-owner"
        lock.release(task.id, "next-owner")


class RaisingEvidenceLoader(VerificationEvidenceLoader):
    def load(self, task_id: UUID) -> EvidenceLoadResult:
        raise RuntimeError("evidence loader failure")


def test_verification_worker_error_is_isolated_and_releases_lock(
    database: Database, tmp_path: Path
) -> None:
    project = make_project()
    task = make_task(project.id)
    persist(database, project, [task])
    client = fakeredis.FakeRedis(decode_responses=True)
    VerificationQueue(client).enqueue(task.id)
    worker = make_verification_worker(database, client, tmp_path)
    worker.evidence_loader = RaisingEvidenceLoader(tmp_path)

    result = worker.run_once()

    assert result.error_type == "RuntimeError"
    lock = TaskLock(client, prefix="test-verification-lock")
    assert lock.acquire(task.id, owner_token="after-error", ttl_seconds=30) == "after-error"
    lock.release(task.id, "after-error")


def test_dependent_task_waits_for_completed_not_verifying(
    database: Database, tmp_path: Path
) -> None:
    project = make_project()
    first = make_task(project.id, status=TaskStatus.PENDING)
    dependent = make_task(
        project.id,
        status=TaskStatus.PENDING,
        dependencies=[first.id],
    )
    persist(database, project, [first, dependent])
    client = fakeredis.FakeRedis(decode_responses=True)
    execution_dispatcher = TaskDispatcher(
        TaskRepository(database.session_factory), ReadyTaskQueue(client)
    )
    assert execution_dispatcher.dispatch(project.id).promoted_task_ids == [first.id]
    assert (
        TaskRepository(database.session_factory).get_required(dependent.id).status
        is TaskStatus.PENDING
    )

    loaded = TaskRepository(database.session_factory).get_required(first.id)
    TaskStateMachine.transition(loaded, TaskStatus.RUNNING)
    TaskStateMachine.transition(loaded, TaskStatus.VERIFYING)
    TaskRepository(database.session_factory).update(loaded)
    assert execution_dispatcher.dispatch(project.id).promoted_task_ids == []

    TaskStateMachine.transition(loaded, TaskStatus.COMPLETED)
    TaskRepository(database.session_factory).update(loaded)
    assert execution_dispatcher.dispatch(project.id).promoted_task_ids == [dependent.id]


def test_coordinator_runs_execution_then_verification_and_unlocks_dependency(
    database: Database,
    tmp_path: Path,
) -> None:
    project = make_project()
    first = make_task(project.id, status=TaskStatus.PENDING)
    dependent = make_task(project.id, status=TaskStatus.PENDING, dependencies=[first.id])
    persist(database, project, [first, dependent])
    write_evidence(tmp_path, first.id)
    write_evidence(tmp_path, dependent.id)
    client = fakeredis.FakeRedis(decode_responses=True)
    registry = AgentRegistry()
    registry.register(SuccessAgent())
    coordinator = SwarmCoordinator(
        TaskRepository(database.session_factory),
        AgentExecutionRepository(database.session_factory),
        ReadyTaskQueue(client),
        TaskLock(client),
        AgentHeartbeat(client),
        registry,
        max_workers=1,
        verification_workers=1,
        worker_config=WorkerConfig(poll_interval_seconds=0.01),
        workspace_path=tmp_path,
    )

    result = coordinator.run(project.id, poll_interval_seconds=0.01)

    statuses = {
        task.id: TaskRepository(database.session_factory).get_required(task.id).status
        for task in (first, dependent)
    }
    assert statuses == {first.id: TaskStatus.COMPLETED, dependent.id: TaskStatus.COMPLETED}
    assert result.pending_task_ids == []
    assert result.verification_workers[0].tasks_executed == 2


def test_coordinator_dry_run_does_not_mutate_or_enqueue(database: Database, tmp_path: Path) -> None:
    project = make_project()
    task = make_task(project.id, status=TaskStatus.PENDING)
    persist(database, project, [task])
    client = fakeredis.FakeRedis(decode_responses=True)
    registry = AgentRegistry()
    registry.register(SuccessAgent())
    coordinator = SwarmCoordinator(
        TaskRepository(database.session_factory),
        AgentExecutionRepository(database.session_factory),
        ReadyTaskQueue(client),
        TaskLock(client),
        AgentHeartbeat(client),
        registry,
        workspace_path=tmp_path,
    )

    result = coordinator.dry_run(project.id)

    assert result.dry_run is True
    assert result.dispatches[0].promoted_task_ids == []
    assert ReadyTaskQueue(client).size() == 0
    assert (
        TaskRepository(database.session_factory).get_required(task.id).status
        is TaskStatus.PENDING
    )


def test_verification_worker_count_is_bounded() -> None:
    with pytest.raises(ValueError):
        factory = cast(Callable[[WorkerConfig], VerificationWorker], lambda config: object())
        VerificationWorkerManager(factory, max_workers=3)


def test_swarm_status_shows_durable_counts_and_safe_heartbeats(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = Project(
        id=uuid4(),
        name="status project",
        repository_path=tmp_path,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    ProjectRepository(database.session_factory).create(project)
    repository = TaskRepository(database.session_factory)
    for status in TaskStatus:
        repository.create(make_task(project.id, status=status))
    redis_value = fakeredis.FakeRedis(decode_responses=True)
    AgentHeartbeat(redis_value).set_heartbeat(
        "worker-status", status="idle", process_id=123, ttl_seconds=30
    )

    class FakeSettings:
        database_url = database.engine.url.render_as_string(hide_password=False)
        redis_url = "redis://localhost:6379/0"

    class FakeRedisClient:
        def __init__(self, url: str) -> None:
            self.client = redis_value

        def close(self) -> None:
            pass

    monkeypatch.setattr(cli_module, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(cli_module, "RedisClient", FakeRedisClient)
    result = RUNNER.invoke(cli_module.app, ["swarm-status", str(tmp_path)])

    assert result.exit_code == 0
    for status in TaskStatus:
        assert f"{status.name}=1" in result.stdout
    assert "worker=worker-status status=idle pid=123" in result.stdout
    assert str(tmp_path) not in result.stdout
