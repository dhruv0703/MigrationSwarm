"""Unit tests for the in-memory task graph and scheduler."""

from collections.abc import Iterable
from uuid import UUID

import pytest

from migrationswarm.core.scheduler import (
    DuplicateTaskError,
    TaskDependencyCycleError,
    TaskGraph,
    TaskScheduler,
    UnknownDependencyError,
)
from migrationswarm.core.tasks import Task, TaskStateMachine, TaskStatus, TaskType

PROJECT_ID = UUID(int=999)


def make_task(
    name: str,
    dependencies: Iterable[str] = (),
    status: TaskStatus = TaskStatus.PENDING,
) -> Task:
    """Build a task with stable IDs that make ordering assertions readable."""
    return Task(
        id=UUID(int=ord(name)),
        project_id=PROJECT_ID,
        task_type=TaskType.REPOSITORY_ANALYSIS,
        title=name,
        description=f"Task {name}",
        dependencies=[UUID(int=ord(dependency)) for dependency in dependencies],
        status=status,
    )


def names(tasks: Iterable[Task]) -> list[str]:
    """Return task titles for concise assertions."""
    return [task.title for task in tasks]


def complete(task: Task) -> None:
    """Complete a ready task through the domain state machine."""
    TaskStateMachine.transition(task, TaskStatus.RUNNING)
    TaskStateMachine.transition(task, TaskStatus.VERIFYING)
    TaskStateMachine.transition(task, TaskStatus.COMPLETED)


def test_add_tasks_and_retrieve_them() -> None:
    """The graph stores task objects by ID."""
    task = make_task("A")
    graph = TaskGraph()

    graph.add_task(task)

    assert graph.get_task(task.id) is task
    assert graph.all_tasks() == (task,)


def test_duplicate_task_ids_are_rejected() -> None:
    """A graph cannot contain two tasks with the same ID."""
    task = make_task("A")
    graph = TaskGraph([task])

    with pytest.raises(DuplicateTaskError, match="Task ID already exists"):
        graph.add_task(make_task("A"))


def test_unknown_dependencies_are_rejected() -> None:
    """Every dependency must reference a task in the graph or batch."""
    with pytest.raises(UnknownDependencyError, match="unknown dependency"):
        TaskGraph([make_task("A", ["B"])])


def test_simple_dependency_chain_and_direct_relationships() -> None:
    """Direct dependencies and dependents are exposed for a task chain."""
    tasks = [make_task("A"), make_task("B", ["A"]), make_task("C", ["B"])]
    graph = TaskGraph(tasks)

    assert names(graph.get_direct_dependencies(UUID(int=ord("B")))) == ["A"]
    assert names(graph.get_direct_dependents(UUID(int=ord("B")))) == ["C"]
    assert names(graph.topological_order()) == ["A", "B", "C"]


def test_branching_dependencies_and_multiple_independent_roots() -> None:
    """Independent roots and branching edges are represented together."""
    graph = TaskGraph(
        [
            make_task("A"),
            make_task("B"),
            make_task("C", ["A"]),
            make_task("D", ["A", "B"]),
            make_task("E", ["C", "D"]),
        ]
    )

    assert names(graph.topological_order()) == ["A", "B", "C", "D", "E"]
    assert names(graph.eligible_tasks()) == ["A", "B"]


def test_cycle_detection_rejects_multi_task_cycle() -> None:
    """A batch containing a dependency cycle cannot be added."""
    with pytest.raises(TaskDependencyCycleError, match="cycle"):
        TaskGraph([make_task("A", ["B"]), make_task("B", ["A"])])


def test_self_dependency_is_rejected_as_cycle() -> None:
    """A task cannot depend on itself."""
    with pytest.raises(TaskDependencyCycleError, match="cycle"):
        TaskGraph([make_task("A", ["A"])])


def test_dependency_completion_and_graph_queries_do_not_mutate_state() -> None:
    """The graph only observes lifecycle state; it never promotes tasks itself."""
    root = make_task("A", status=TaskStatus.COMPLETED)
    dependent = make_task("B", ["A"])
    graph = TaskGraph([root, dependent])

    assert graph.all_dependencies_completed(dependent.id)
    assert names(graph.eligible_tasks()) == ["B"]
    assert dependent.status is TaskStatus.PENDING


def test_scheduler_promotes_only_unblocked_tasks() -> None:
    """The A-B-C-D-E example promotes tasks as dependencies complete."""
    graph = TaskGraph(
        [
            make_task("A"),
            make_task("B"),
            make_task("C", ["A"]),
            make_task("D", ["A", "B"]),
            make_task("E", ["C", "D"]),
        ]
    )
    scheduler = TaskScheduler(graph)

    assert names(scheduler.schedule()) == ["A", "B"]
    assert names(graph.eligible_tasks()) == []

    complete(graph.get_task(UUID(int=ord("A"))))
    assert names(scheduler.schedule()) == ["B", "C"]
    assert graph.get_task(UUID(int=ord("D"))).status is TaskStatus.PENDING
    assert graph.get_task(UUID(int=ord("E"))).status is TaskStatus.PENDING

    complete(graph.get_task(UUID(int=ord("B"))))
    assert names(scheduler.schedule()) == ["C", "D"]

    complete(graph.get_task(UUID(int=ord("C"))))
    complete(graph.get_task(UUID(int=ord("D"))))
    assert names(scheduler.schedule()) == ["E"]


def test_scheduler_promotes_multiple_independent_branches() -> None:
    """All independent roots can become ready in one scheduling pass."""
    graph = TaskGraph([make_task("A"), make_task("B"), make_task("F")])

    promoted = TaskScheduler(graph).promote_ready_tasks()

    assert names(promoted) == ["A", "B", "F"]
    assert all(task.status is TaskStatus.READY for task in promoted)


def test_scheduler_uses_state_machine_for_promotion() -> None:
    """Promotion delegates lifecycle mutation to the configured state machine."""
    calls: list[tuple[UUID, TaskStatus]] = []

    class SpyStateMachine(TaskStateMachine):
        @classmethod
        def transition(cls, task: Task, target: TaskStatus) -> Task:
            calls.append((task.id, target))
            return super().transition(task, target)

    task = make_task("A")
    graph = TaskGraph([task])
    TaskScheduler(graph, state_machine=SpyStateMachine).promote_ready_tasks()

    assert calls == [(task.id, TaskStatus.READY)]
    assert task.status is TaskStatus.READY
