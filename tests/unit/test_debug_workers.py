"""Focused tests for explicit bounded debug dispatch and re-verification."""

import json
from pathlib import Path
from uuid import UUID, uuid4

import fakeredis

from migrationswarm.core.agents import (
    AgentCapability,
    AgentContext,
    AgentRegistry,
    AgentResult,
    BaseAgent,
)
from migrationswarm.core.orchestrator import FailureCategory
from migrationswarm.core.repair import RepairAttempt, RepairAttemptStatus
from migrationswarm.core.tasks import Task, TaskStatus, TaskType
from migrationswarm.core.verification import VerificationCoordinator
from migrationswarm.persistence.db import Database
from migrationswarm.persistence.db.repositories import (
    AgentExecutionRepository,
    ProjectRepository,
    RepairAttemptRepository,
    TaskRepository,
)
from migrationswarm.persistence.mapping import Project
from migrationswarm.persistence.redis import AgentHeartbeat, ReadyTaskQueue, TaskLock
from migrationswarm.workers.debugging import DebugDispatcher, DebugQueue, DebugWorker
from migrationswarm.workers.dispatcher import TaskDispatcher
from migrationswarm.workers.models import WorkerConfig
from migrationswarm.workers.verification import (
    VerificationEvidenceLoader,
    VerificationQueue,
    VerificationWorker,
)


class FakeDebugAgent(BaseAgent):
    name = "debug"
    capabilities = frozenset({AgentCapability.DEBUGGING})

    def execute(self, task: Task, context: AgentContext) -> AgentResult:
        del context
        return AgentResult(
            task_id=task.id,
            agent_name=self.name,
            success=True,
            summary="Applied a deterministic fake repair.",
            artifacts=[".migrationswarm/debug-results/fake.json"],
            metadata={"model_provider": "fake", "model_name": "fake-debug"},
        )


class FakeVerifier(BaseAgent):
    name = "build_verification"
    capabilities = frozenset({AgentCapability.VERIFICATION})

    def __init__(self, result_path: Path, log_path: Path) -> None:
        self.result_path = result_path
        self.log_path = log_path

    def execute(self, task: Task, context: AgentContext) -> AgentResult:
        del context
        payload = {
            "task_id": str(task.id),
            "workspace_path": "isolated",
            "build_system": "maven",
            "commands_run": [
                {
                    "command": ["mvn", "test"],
                    "status": "passed",
                    "exit_code": 0,
                    "duration_ms": 1,
                }
            ],
            "status": "passed",
            "exit_code": 0,
            "duration_ms": 1,
            "test_summary": {"tests_run": 1, "failures": 0, "errors": 0, "skipped": 0},
            "changed_files": ["services/example/pom.xml"],
        }
        self.result_path.write_text(
            json.dumps({**payload, "artifacts": {"log": str(self.log_path)}}),
            encoding="utf-8",
        )
        self.log_path.write_text("fake verification log", encoding="utf-8")
        return AgentResult(
            task_id=task.id,
            agent_name=self.name,
            success=True,
            summary="Fake verification passed.",
            artifacts=[str(self.result_path), str(self.log_path)],
            metadata={"verification_result": payload},
        )


def _task(
    project_id: UUID,
    status: TaskStatus,
    *,
    task_type: TaskType = TaskType.CODE_REFACTOR,
) -> Task:
    return Task(
        project_id=project_id,
        task_type=task_type,
        title="Extract example service",
        description="Run the bounded service extraction.",
        status=status,
        assigned_agent="worker",
        metadata={"target_service_directory": "services/example"},
    )


def _failed_artifacts(root: Path, task: Task) -> None:
    result = root / ".migrationswarm" / "verification-results" / f"{task.id}.json"
    log = root / ".migrationswarm" / "verification-results" / f"{task.id}.log"
    result.parent.mkdir(parents=True)
    result.write_text(
        json.dumps(
            {
                "task_id": str(task.id),
                "task_type": task.task_type.value,
                "workspace_path": "isolated",
                "build_system": "maven",
                "commands_run": [
                    {
                        "command": ["mvn", "test"],
                        "status": "failed",
                        "exit_code": 1,
                        "duration_ms": 1,
                    }
                ],
                "status": "failed",
                "exit_code": 1,
                "duration_ms": 1,
                "stdout_summary": "COMPILATION ERROR: cannot find symbol",
                "test_summary": {"tests_run": 0, "failures": 0, "errors": 0, "skipped": 0},
                "changed_files": ["services/example/pom.xml"],
                "warnings": [],
                "artifacts": {"json": str(result), "log": str(log)},
            }
        ),
        encoding="utf-8",
    )
    log.write_text("bounded test log", encoding="utf-8")
    decision = root / ".migrationswarm" / "verification-decisions" / f"{task.id}.json"
    decision.parent.mkdir(parents=True, exist_ok=True)
    decision.write_text(
        json.dumps(
            {
                "task_id": str(task.id),
                "decision": "failed",
                "reasons": ["compilation failed"],
                "warnings": [],
                "evidence_summary": {},
            }
        ),
        encoding="utf-8",
    )


def test_debug_dispatch_is_stable_and_uses_a_separate_queue(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    database.create_all_for_tests()
    project = Project(name="example", repository_path=tmp_path)
    ProjectRepository(database.session_factory).create(project)
    original = _task(project.id, TaskStatus.FAILED)
    TaskRepository(database.session_factory).create(original)
    _failed_artifacts(tmp_path, original)
    repairs = RepairAttemptRepository(database.session_factory)
    queue = DebugQueue(fakeredis.FakeRedis(decode_responses=True))
    dispatcher = DebugDispatcher(
        TaskRepository(database.session_factory),
        repairs,
        queue,
        repository_root=tmp_path,
    )

    first = dispatcher.dispatch(project.id)
    second = dispatcher.dispatch(project.id)

    assert first.enqueued_count == 1
    assert second.enqueued_count == 0
    debug_id = first.debug_task_ids[0]
    debug_task = TaskRepository(database.session_factory).get_required(debug_id)
    assert debug_task.task_type is TaskType.DEBUG
    assert debug_task.dependencies == [original.id]
    assert debug_task.metadata["original_task_id"] == str(original.id)
    assert queue.key == "migrationswarm:debug-tasks"
    assert repairs.list_for_original(original.id)[0].failure_category == FailureCategory.COMPILATION


def test_successful_debug_repair_reverifies_and_completes_original(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    database.create_all_for_tests()
    project = Project(name="example", repository_path=tmp_path)
    projects = ProjectRepository(database.session_factory)
    projects.create(project)
    original = _task(project.id, TaskStatus.FAILED)
    downstream = _task(project.id, TaskStatus.PENDING)
    downstream.dependencies = [original.id]
    tasks = TaskRepository(database.session_factory)
    tasks.create(original)
    tasks.create(downstream)
    _failed_artifacts(tmp_path, original)
    client = fakeredis.FakeRedis(decode_responses=True)
    repairs = RepairAttemptRepository(database.session_factory)
    dispatcher = DebugDispatcher(tasks, repairs, DebugQueue(client), repository_root=tmp_path)
    dispatcher.dispatch(project.id)
    execution = AgentExecutionRepository(database.session_factory)
    verification_dir = tmp_path / ".migrationswarm" / "verification-results"
    verification_json = verification_dir / f"{original.id}.json"
    verification_log = verification_dir / f"{original.id}.log"
    registry = AgentRegistry()
    registry.register(FakeDebugAgent())
    worker = DebugWorker(
        tasks,
        execution,
        repairs,
        DebugQueue(client),
        VerificationQueue(client),
        TaskLock(client, prefix="test-debug-lock"),
        AgentHeartbeat(client),
        registry,
        config=WorkerConfig(worker_id="debug-test"),
        verification_agent=FakeVerifier(verification_json, verification_log),
    )

    result = worker.run_once()
    worker_result = VerificationWorker(
        tasks,
        execution,
        VerificationQueue(client),
        TaskLock(client, prefix="test-verification-lock"),
        AgentHeartbeat(client),
        coordinator=VerificationCoordinator(tmp_path),
        evidence_loader=VerificationEvidenceLoader(tmp_path, execution),
        config=WorkerConfig(worker_id="verification-test"),
    ).run_once()

    assert result.success is True
    assert worker_result.executed is True
    assert tasks.get_required(original.id).status is TaskStatus.COMPLETED
    assert tasks.get_required(downstream.id).status is TaskStatus.PENDING
    TaskDispatcher(tasks, ReadyTaskQueue(client)).dispatch(project.id)
    assert tasks.get_required(downstream.id).status is TaskStatus.READY
    assert repairs.list_for_original(original.id)[0].status is RepairAttemptStatus.PASSED


def test_exhausted_repair_requires_human_review(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'state.db'}")
    database.create_all_for_tests()
    project = Project(name="example", repository_path=tmp_path)
    ProjectRepository(database.session_factory).create(project)
    original = _task(project.id, TaskStatus.FAILED)
    tasks = TaskRepository(database.session_factory)
    tasks.create(original)
    _failed_artifacts(tmp_path, original)
    repairs = RepairAttemptRepository(database.session_factory)
    debug_id = uuid4()
    repairs.create(
        RepairAttempt(
            original_task_id=original.id,
            debug_task_id=debug_id,
            attempt_number=1,
            failure_category=FailureCategory.COMPILATION.value,
            status=RepairAttemptStatus.FAILED,
        )
    )
    dispatcher = DebugDispatcher(
        tasks,
        repairs,
        DebugQueue(fakeredis.FakeRedis(decode_responses=True)),
        repository_root=tmp_path,
        max_attempts=1,
    )

    dispatcher.dispatch(project.id)

    assert tasks.get_required(original.id).status is TaskStatus.HUMAN_REVIEW
    assert repairs.list_for_original(original.id)[0].status is RepairAttemptStatus.EXHAUSTED
