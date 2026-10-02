"""Unit tests for agent contracts, registry, and worker runtime."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from migrationswarm.core.agents import (
    TASK_TYPE_TO_CAPABILITY,
    AgentAssignmentError,
    AgentCapability,
    AgentContext,
    AgentRegistry,
    AgentResult,
    BaseAgent,
    DuplicateAgentError,
    TaskNotReadyError,
    UnknownAgentError,
    UnsupportedCapabilityError,
    WorkerRuntime,
    required_capability,
)
from migrationswarm.core.scheduler import TaskGraph, TaskScheduler
from migrationswarm.core.tasks import Task, TaskStatus, TaskType

PROJECT_ID = UUID(int=1000)


def make_task(
    *,
    agent_name: str | None = "successful",
    status: TaskStatus = TaskStatus.READY,
    task_type: TaskType = TaskType.REPOSITORY_ANALYSIS,
) -> Task:
    """Build a task suitable for worker-runtime tests."""
    return Task(
        project_id=PROJECT_ID,
        task_type=task_type,
        title="Test task",
        description="Execute a test task.",
        status=status,
        assigned_agent=agent_name,
    )


def make_result(task: Task, agent_name: str, success: bool) -> AgentResult:
    """Build a result with a valid UTC timestamp range."""
    started_at = datetime.now(UTC)
    return AgentResult(
        task_id=task.id,
        agent_name=agent_name,
        success=success,
        summary="Execution completed.",
        started_at=started_at,
        completed_at=started_at,
    )


class SuccessfulFakeAgent(BaseAgent):
    """Test-only agent that returns a successful result."""

    name = "successful"
    capabilities = frozenset({AgentCapability.REPOSITORY_ANALYSIS})

    def execute(self, task: Task, context: AgentContext) -> AgentResult:
        return make_result(task, self.name, success=True)


class FailingFakeAgent(BaseAgent):
    """Test-only agent that returns a failed result."""

    name = "failing"
    capabilities = frozenset({AgentCapability.REPOSITORY_ANALYSIS})

    def execute(self, task: Task, context: AgentContext) -> AgentResult:
        return make_result(task, self.name, success=False)


class ExceptionFakeAgent(BaseAgent):
    """Test-only agent that raises during execution."""

    name = "exception"
    capabilities = frozenset({AgentCapability.REPOSITORY_ANALYSIS})

    def execute(self, task: Task, context: AgentContext) -> AgentResult:
        raise RuntimeError("fake agent failed")


class TestingFakeAgent(BaseAgent):
    """Test-only agent advertising a different capability."""

    name = "testing"
    capabilities = frozenset({AgentCapability.TESTING})

    def execute(self, task: Task, context: AgentContext) -> AgentResult:
        return make_result(task, self.name, success=True)


class BypassingFakeAgent(BaseAgent):
    """Test-only agent that attempts to mutate its execution task directly."""

    name = "bypassing"
    capabilities = frozenset({AgentCapability.REPOSITORY_ANALYSIS})

    def execute(self, task: Task, context: AgentContext) -> AgentResult:
        task.status = TaskStatus.COMPLETED
        context.task.status = TaskStatus.COMPLETED
        return make_result(task, self.name, success=True)


def make_runtime(task: Task, agent: BaseAgent) -> WorkerRuntime:
    """Build a runtime containing the supplied test agent."""
    graph = TaskGraph([task])
    scheduler = TaskScheduler(graph)
    registry = AgentRegistry()
    registry.register(agent)
    return WorkerRuntime(scheduler, registry)


def test_agent_result_defaults_are_independent() -> None:
    """Artifact and metadata collections use safe independent defaults."""
    first = AgentResult(task_id=uuid4(), agent_name="agent", success=True, summary="done")
    second = AgentResult(task_id=uuid4(), agent_name="agent", success=True, summary="done")

    first.artifacts.append("report.txt")
    first.metadata["attempt"] = 1

    assert second.artifacts == []
    assert second.metadata == {}


def test_agent_result_requires_aware_ordered_timestamps() -> None:
    """Result timestamps must be UTC-aware and completed_at cannot precede start."""
    started_at = datetime.now(UTC)

    with pytest.raises(ValueError, match="timezone-aware"):
        AgentResult(
            task_id=uuid4(),
            agent_name="agent",
            success=True,
            summary="done",
            started_at=datetime(2026, 1, 1),
            completed_at=started_at,
        )

    with pytest.raises(ValueError, match="completed_at"):
        AgentResult(
            task_id=uuid4(),
            agent_name="agent",
            success=True,
            summary="done",
            started_at=started_at,
            completed_at=started_at - timedelta(seconds=1),
        )


def test_agent_context_defaults_metadata_safely() -> None:
    """Agent contexts contain the task and independent metadata."""
    task = make_task()
    first = AgentContext(project_id=PROJECT_ID, task=task)
    second = AgentContext(project_id=PROJECT_ID, task=task)

    first.metadata["mode"] = "test"

    assert first.task is task
    assert second.metadata == {}
    assert first.workspace_path is None


def test_capability_mapping_covers_every_task_type() -> None:
    """Every task type has one centralized required capability."""
    assert set(TASK_TYPE_TO_CAPABILITY) == set(TaskType)
    assert required_capability(TaskType.TEST) is AgentCapability.TESTING
    assert required_capability(TaskType.DEBUG) is AgentCapability.DEBUGGING
    assert required_capability(TaskType.VERIFY) is AgentCapability.VERIFICATION


def test_agent_registry_registers_and_lists_agents_deterministically() -> None:
    """The registry supports multiple differently named agents."""
    registry = AgentRegistry()
    successful = SuccessfulFakeAgent()
    testing = TestingFakeAgent()

    registry.register(testing)
    registry.register(successful)

    assert registry.get(successful.name) is successful
    assert [agent.name for agent in registry.list_agents()] == ["successful", "testing"]
    assert [agent.name for agent in registry.find_by_capability(AgentCapability.TESTING)] == [
        "testing"
    ]


def test_agent_registry_rejects_duplicate_names() -> None:
    """Agent names are unique registry keys."""
    registry = AgentRegistry()
    registry.register(SuccessfulFakeAgent())

    with pytest.raises(DuplicateAgentError, match="already registered"):
        registry.register(SuccessfulFakeAgent())


def test_agent_registry_rejects_unknown_lookup() -> None:
    """Unknown agent names raise a domain-specific exception."""
    with pytest.raises(UnknownAgentError, match="Unknown agent"):
        AgentRegistry().get("missing")


def test_successful_worker_execution_stops_at_verifying() -> None:
    """A successful fake agent follows READY -> RUNNING -> VERIFYING."""
    task = make_task()
    runtime = make_runtime(task, SuccessfulFakeAgent())

    result = runtime.execute(task, AgentContext(project_id=PROJECT_ID, task=task))

    assert result.success
    assert result.task_id == task.id
    assert task.status is TaskStatus.VERIFYING


def test_failing_worker_execution_transitions_to_failed() -> None:
    """A failed result follows READY -> RUNNING -> FAILED."""
    task = make_task(agent_name="failing")
    runtime = make_runtime(task, FailingFakeAgent())

    result = runtime.execute(task)

    assert not result.success
    assert task.status is TaskStatus.FAILED


def test_agent_exception_transitions_to_failed_and_is_reraised() -> None:
    """Agent exceptions are not swallowed, while the task still fails safely."""
    task = make_task(agent_name="exception")
    runtime = make_runtime(task, ExceptionFakeAgent())

    with pytest.raises(RuntimeError, match="fake agent failed"):
        runtime.execute(task)

    assert task.status is TaskStatus.FAILED


def test_unknown_assigned_agent_is_rejected_before_execution() -> None:
    """The runtime requires an assigned name registered in the registry."""
    task = make_task(agent_name="missing")
    runtime = WorkerRuntime(TaskScheduler(TaskGraph([task])), AgentRegistry())

    with pytest.raises(UnknownAgentError, match="Unknown agent"):
        runtime.execute(task)

    assert task.status is TaskStatus.READY


def test_missing_assigned_agent_is_rejected() -> None:
    """Tasks without assignments cannot enter RUNNING."""
    task = make_task(agent_name=None)
    runtime = make_runtime(task, SuccessfulFakeAgent())

    with pytest.raises(AgentAssignmentError, match="no assigned agent"):
        runtime.execute(task)

    assert task.status is TaskStatus.READY


def test_unsupported_capability_is_rejected_before_execution() -> None:
    """The assigned agent must advertise the task type's required capability."""
    task = make_task(agent_name="testing")
    runtime = make_runtime(task, TestingFakeAgent())

    with pytest.raises(UnsupportedCapabilityError, match="does not support"):
        runtime.execute(task)

    assert task.status is TaskStatus.READY


def test_non_ready_tasks_are_rejected() -> None:
    """The runtime accepts only tasks currently available as READY."""
    task = make_task(status=TaskStatus.PENDING)
    runtime = make_runtime(task, SuccessfulFakeAgent())

    with pytest.raises(TaskNotReadyError, match="must be READY"):
        runtime.execute(task)


def test_agent_cannot_bypass_state_machine_with_direct_status_mutation() -> None:
    """The runtime isolates the agent task view and owns lifecycle transitions."""
    task = make_task(agent_name="bypassing")
    runtime = make_runtime(task, BypassingFakeAgent())

    runtime.execute(task)

    assert task.status is TaskStatus.VERIFYING
