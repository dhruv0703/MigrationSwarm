"""Tests for bounded durable task workers and swarm coordination."""

from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic
from uuid import UUID, uuid4

import fakeredis
import pytest

from migrationswarm.core.agents.base import BaseAgent
from migrationswarm.core.agents.capabilities import AgentCapability
from migrationswarm.core.agents.models import AgentContext, AgentResult
from migrationswarm.core.agents.registry import AgentRegistry
from migrationswarm.core.tasks.enums import TaskStatus, TaskType
from migrationswarm.core.tasks.models import Task
from migrationswarm.core.tasks.state_machine import TaskStateMachine
from migrationswarm.persistence.db import Database
from migrationswarm.persistence.db.repositories import (
    AgentExecutionRepository,
    ProjectRepository,
    TaskRepository,
)
from migrationswarm.persistence.mapping import Project
from migrationswarm.persistence.redis import AgentHeartbeat, ReadyTaskQueue, TaskLock
from migrationswarm.workers import (
    SwarmCoordinator,
    TaskDispatcher,
    Worker,
    WorkerConfig,
    WorkerManager,
    WorkerStatus,
)


class BlockingSuccessAgent(BaseAgent):
    name = "fake-success"
    capabilities = frozenset({AgentCapability.REPOSITORY_ANALYSIS})

    def __init__(self, *, barrier: Event | None = None) -> None:
        self.barrier = barrier
        self.started = Event()
        self.active = 0
        self.max_active = 0
        self._lock = Lock()

    def execute(self, task: Task, context: AgentContext) -> AgentResult:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.started.set()
        if self.barrier is not None:
            self.barrier.wait(timeout=5)
        with self._lock:
            self.active -= 1
        return AgentResult(
            task_id=task.id,
            agent_name=self.name,
            success=True,
            summary=f"executed {task.title}",
        )


class FailingAgent(BaseAgent):
    name = "fake-failing"
    capabilities = frozenset({AgentCapability.REPOSITORY_ANALYSIS})

    def execute(self, task: Task, context: AgentContext) -> AgentResult:
        return AgentResult(
            task_id=task.id,
            agent_name=self.name,
            success=False,
            summary="deterministic failure",
        )


class ExceptionAgent(BaseAgent):
    name = "fake-exception"
    capabilities = frozenset({AgentCapability.REPOSITORY_ANALYSIS})

    def execute(self, task: Task, context: AgentContext) -> AgentResult:
        raise RuntimeError("expected fake exception")


class StatusMutatingAgent(BaseAgent):
    name = "fake-mutating"
    capabilities = frozenset({AgentCapability.REPOSITORY_ANALYSIS})

    def execute(self, task: Task, context: AgentContext) -> AgentResult:
        task.status = TaskStatus.COMPLETED
        return AgentResult(
            task_id=task.id,
            agent_name=self.name,
            success=True,
            summary="mutated copy",
        )


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'workers.db'}")
    database.create_all_for_tests()
    return database


def make_project() -> Project:
    now = datetime.now(UTC)
    return Project(
        id=uuid4(),
        name="worker project",
        repository_path=Path("C:/repositories/worker-project"),
        created_at=now,
        updated_at=now,
    )


def make_task(
    project_id: UUID,
    *,
    status: TaskStatus = TaskStatus.PENDING,
    dependencies: list[UUID] | None = None,
    agent: str = "fake-success",
) -> Task:
    return Task(
        project_id=project_id,
        task_type=TaskType.REPOSITORY_ANALYSIS,
        title="worker task",
        description="deterministic worker test",
        status=status,
        dependencies=list(dependencies or []),
        assigned_agent=agent,
    )


def make_worker(
    database: Database,
    client: fakeredis.FakeRedis,
    registry: AgentRegistry,
    *,
    worker_id: str = "worker-1",
    heartbeat_interval_seconds: float = 0.01,
) -> Worker:
    return Worker(
        TaskRepository(database.session_factory),
        AgentExecutionRepository(database.session_factory),
        ReadyTaskQueue(client),
        TaskLock(client),
        AgentHeartbeat(client),
        registry,
        config=WorkerConfig(
            worker_id=worker_id,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            poll_interval_seconds=0.01,
        ),
    )


def persist_project_and_tasks(database: Database, project: Project, tasks: list[Task]) -> None:
    ProjectRepository(database.session_factory).create(project)
    repository = TaskRepository(database.session_factory)
    for task in tasks:
        repository.create(task)


def test_worker_config_and_claim_defaults_are_bounded() -> None:
    config = WorkerConfig()
    assert config.worker_id
    assert config.heartbeat_ttl_seconds > 0
    assert config.lock_ttl_seconds > 0


def test_dispatcher_promotes_only_dependency_ready_tasks(database: Database) -> None:
    project = make_project()
    root = make_task(project.id)
    child = make_task(project.id, dependencies=[root.id])
    persist_project_and_tasks(database, project, [root, child])
    client = fakeredis.FakeRedis(decode_responses=True)
    dispatcher = TaskDispatcher(
        TaskRepository(database.session_factory), ReadyTaskQueue(client)
    )

    first = dispatcher.dispatch(project.id)
    assert first.promoted_task_ids == [root.id]
    assert first.enqueued_task_ids == [root.id]
    assert (
        TaskRepository(database.session_factory).get_required(child.id).status
        is TaskStatus.PENDING
    )

    loaded_root = TaskRepository(database.session_factory).get_required(root.id)
    TaskStateMachine.transition(loaded_root, TaskStatus.RUNNING)
    TaskStateMachine.transition(loaded_root, TaskStatus.VERIFYING)
    TaskStateMachine.transition(loaded_root, TaskStatus.COMPLETED)
    TaskRepository(database.session_factory).update(loaded_root)
    second = dispatcher.dispatch(project.id, include_existing_ready=False)
    assert second.promoted_task_ids == [child.id]
    assert second.enqueued_task_ids == [child.id]


def test_worker_success_persists_verifying_and_execution(database: Database) -> None:
    project = make_project()
    task = make_task(project.id, status=TaskStatus.READY)
    persist_project_and_tasks(database, project, [task])
    client = fakeredis.FakeRedis(decode_responses=True)
    queue = ReadyTaskQueue(client)
    queue.enqueue(task.id)
    registry = AgentRegistry()
    registry.register(BlockingSuccessAgent())
    result = make_worker(database, client, registry).run_once()

    assert result.executed is True
    assert result.success is True
    assert (
        TaskRepository(database.session_factory).get_required(task.id).status
        is TaskStatus.VERIFYING
    )
    assert len(AgentExecutionRepository(database.session_factory).list_for_task(task.id)) == 1


def test_failing_agent_persists_failed_task(database: Database) -> None:
    project = make_project()
    task = make_task(project.id, status=TaskStatus.READY, agent="fake-failing")
    persist_project_and_tasks(database, project, [task])
    client = fakeredis.FakeRedis(decode_responses=True)
    ReadyTaskQueue(client).enqueue(task.id)
    registry = AgentRegistry()
    registry.register(FailingAgent())
    result = make_worker(database, client, registry).run_once()

    assert result.success is False
    assert (
        TaskRepository(database.session_factory).get_required(task.id).status
        is TaskStatus.FAILED
    )


def test_exception_agent_fails_task_but_worker_returns_explicit_error(database: Database) -> None:
    project = make_project()
    task = make_task(project.id, status=TaskStatus.READY, agent="fake-exception")
    persist_project_and_tasks(database, project, [task])
    client = fakeredis.FakeRedis(decode_responses=True)
    ReadyTaskQueue(client).enqueue(task.id)
    registry = AgentRegistry()
    registry.register(ExceptionAgent())
    result = make_worker(database, client, registry).run_once()

    assert result.error_type == "RuntimeError"
    assert (
        TaskRepository(database.session_factory).get_required(task.id).status
        is TaskStatus.FAILED
    )
    execution = AgentExecutionRepository(database.session_factory).list_for_task(task.id)
    assert execution[0].success is False


def test_stale_queue_item_is_skipped_from_database_state(database: Database) -> None:
    project = make_project()
    task = make_task(project.id, status=TaskStatus.COMPLETED)
    persist_project_and_tasks(database, project, [task])
    client = fakeredis.FakeRedis(decode_responses=True)
    ReadyTaskQueue(client).enqueue(task.id)
    registry = AgentRegistry()
    agent = BlockingSuccessAgent()
    registry.register(agent)

    result = make_worker(database, client, registry).run_once()

    assert result.skipped_reason == "database_status_completed"
    assert not agent.started.is_set()


def test_lock_contention_skips_without_execution(database: Database) -> None:
    project = make_project()
    task = make_task(project.id, status=TaskStatus.READY)
    persist_project_and_tasks(database, project, [task])
    client = fakeredis.FakeRedis(decode_responses=True)
    queue = ReadyTaskQueue(client)
    queue.enqueue(task.id)
    TaskLock(client).acquire(task.id, owner_token="other-worker", ttl_seconds=30)
    registry = AgentRegistry()
    agent = BlockingSuccessAgent()
    registry.register(agent)

    result = make_worker(database, client, registry).run_once()

    assert result.skipped_reason == "lock_contended"
    assert not agent.started.is_set()


def test_agents_receive_a_copy_and_cannot_bypass_task_lifecycle(database: Database) -> None:
    project = make_project()
    task = make_task(project.id, status=TaskStatus.READY, agent="fake-mutating")
    persist_project_and_tasks(database, project, [task])
    client = fakeredis.FakeRedis(decode_responses=True)
    ReadyTaskQueue(client).enqueue(task.id)
    registry = AgentRegistry()
    registry.register(StatusMutatingAgent())

    make_worker(database, client, registry).run_once()

    assert (
        TaskRepository(database.session_factory).get_required(task.id).status
        is TaskStatus.VERIFYING
    )


def test_two_workers_execute_independent_tasks_concurrently(database: Database) -> None:
    project = make_project()
    first = make_task(project.id, status=TaskStatus.READY)
    second = make_task(project.id, status=TaskStatus.READY)
    dependent = make_task(project.id, dependencies=[first.id, second.id])
    persist_project_and_tasks(database, project, [first, second, dependent])
    client = fakeredis.FakeRedis(decode_responses=True)
    queue = ReadyTaskQueue(client)
    queue.enqueue(first.id)
    queue.enqueue(second.id)
    barrier = Event()
    registry = AgentRegistry()
    agent = BlockingSuccessAgent(barrier=barrier)
    registry.register(agent)
    worker_one = make_worker(database, client, registry, worker_id="worker-a")
    worker_two = make_worker(database, client, registry, worker_id="worker-b")
    results: list[object] = []

    threads = [
        Thread(target=lambda: results.append(worker_one.run_once())),
        Thread(target=lambda: results.append(worker_two.run_once())),
    ]
    for thread in threads:
        thread.start()
    deadline = monotonic() + 5
    while agent.max_active < 2 and monotonic() < deadline:
        pass
    assert agent.max_active == 2
    barrier.set()
    for thread in threads:
        thread.join(timeout=5)
    assert len(results) == 2
    assert agent.max_active == 2

    task_repository = TaskRepository(database.session_factory)
    for task_id in (first.id, second.id):
        loaded = task_repository.get_required(task_id)
        TaskStateMachine.transition(loaded, TaskStatus.COMPLETED)
        task_repository.update(loaded)
    dispatch = TaskDispatcher(task_repository, queue).dispatch(
        project.id, include_existing_ready=False
    )
    assert dispatch.promoted_task_ids == [dependent.id]
    worker_one.run_once()
    assert task_repository.get_required(dependent.id).status is TaskStatus.VERIFYING


def test_swarm_coordinator_runs_a_finite_pool_and_aggregates_status(database: Database) -> None:
    project = make_project()
    task = make_task(project.id, status=TaskStatus.PENDING)
    persist_project_and_tasks(database, project, [task])
    client = fakeredis.FakeRedis(decode_responses=True)
    registry = AgentRegistry()
    registry.register(BlockingSuccessAgent())
    coordinator = SwarmCoordinator(
        TaskRepository(database.session_factory),
        AgentExecutionRepository(database.session_factory),
        ReadyTaskQueue(client),
        TaskLock(client),
        AgentHeartbeat(client),
        registry,
        max_workers=1,
        worker_config=WorkerConfig(poll_interval_seconds=0.01),
    )

    result = coordinator.run(project.id, poll_interval_seconds=0.01)

    assert result.completed_task_ids == []
    assert result.verifying_task_ids == [task.id]
    assert result.workers[0].tasks_executed == 1


def test_heartbeat_contains_worker_identity_status_and_ttl(database: Database) -> None:
    client = fakeredis.FakeRedis(decode_responses=True)
    worker = make_worker(database, client, AgentRegistry())
    worker._heartbeat(WorkerStatus.IDLE, force=True)
    heartbeat = AgentHeartbeat(client).get_heartbeat(worker.worker_id)
    assert heartbeat is not None
    assert heartbeat.worker_id == worker.worker_id
    assert heartbeat.process_id is not None
    assert heartbeat.status == "idle"
    assert AgentHeartbeat(client).ttl(worker.worker_id) > 0


def test_worker_manager_limits_worker_count() -> None:
    with pytest.raises(ValueError):
        WorkerManager(workers=[], max_workers=0)
