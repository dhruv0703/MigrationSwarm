"""A lightweight, deterministic in-memory task dependency graph."""

from collections.abc import Iterable, Mapping
from heapq import heappop, heappush
from uuid import UUID

from migrationswarm.core.scheduler.exceptions import (
    DuplicateTaskError,
    TaskDependencyCycleError,
    UnknownDependencyError,
)
from migrationswarm.core.tasks.enums import TaskStatus
from migrationswarm.core.tasks.models import Task


class TaskGraph:
    """Store tasks and their dependency edges without changing task state."""

    def __init__(self, tasks: Iterable[Task] | None = None) -> None:
        self._tasks: dict[UUID, Task] = {}
        self._dependencies: dict[UUID, tuple[UUID, ...]] = {}
        if tasks is not None:
            self.add_tasks(tasks)

    def add_task(self, task: Task) -> None:
        """Add one task after validating its dependency references and cycles."""
        self.add_tasks([task])

    def add_tasks(self, tasks: Iterable[Task]) -> None:
        """Add tasks atomically, validating the complete candidate graph."""
        candidates = list(tasks)
        candidate_ids: set[UUID] = set()
        for task in candidates:
            if task.id in self._tasks or task.id in candidate_ids:
                raise DuplicateTaskError(f"Task ID already exists: {task.id}")
            candidate_ids.add(task.id)

        all_task_ids = set(self._tasks) | candidate_ids
        candidate_dependencies = dict(self._dependencies)
        for task in candidates:
            dependencies = tuple(dict.fromkeys(task.dependencies))
            if task.id in dependencies:
                raise TaskDependencyCycleError(
                    f"Task dependency cycle detected: {task.id} -> {task.id}"
                )
            for dependency_id in dependencies:
                if dependency_id not in all_task_ids:
                    raise UnknownDependencyError(
                        f"Task {task.id} references unknown dependency {dependency_id}"
                    )
            candidate_dependencies[task.id] = dependencies

        cycle = self._find_cycle(candidate_dependencies)
        if cycle is not None:
            cycle_text = " -> ".join(str(task_id) for task_id in cycle)
            raise TaskDependencyCycleError(f"Task dependency cycle detected: {cycle_text}")

        for task in candidates:
            self._tasks[task.id] = task
            self._dependencies[task.id] = candidate_dependencies[task.id]

    def get_task(self, task_id: UUID) -> Task:
        """Return a task by ID."""
        return self._tasks[task_id]

    def all_tasks(self) -> tuple[Task, ...]:
        """Return all tasks in deterministic ID order."""
        return tuple(self._tasks[task_id] for task_id in self._sorted_ids(self._tasks))

    def get_direct_dependencies(self, task_id: UUID) -> tuple[Task, ...]:
        """Return the direct dependency tasks for a task."""
        self._require_task(task_id)
        return tuple(self._tasks[dependency_id] for dependency_id in self._dependencies[task_id])

    def get_direct_dependents(self, task_id: UUID) -> tuple[Task, ...]:
        """Return tasks that directly depend on the given task."""
        self._require_task(task_id)
        dependent_ids = (
            candidate_id
            for candidate_id, dependencies in self._dependencies.items()
            if task_id in dependencies
        )
        return tuple(self._tasks[dependent_id] for dependent_id in self._sorted_ids(dependent_ids))

    def all_dependencies_completed(self, task_id: UUID) -> bool:
        """Return whether every direct dependency has completed."""
        return all(
            dependency.status is TaskStatus.COMPLETED
            for dependency in self.get_direct_dependencies(task_id)
        )

    def eligible_tasks(self) -> tuple[Task, ...]:
        """Return pending tasks whose direct dependencies are all completed."""
        return tuple(
            task
            for task in self.topological_order()
            if task.status is TaskStatus.PENDING and self.all_dependencies_completed(task.id)
        )

    def topological_order(self) -> tuple[Task, ...]:
        """Return tasks in deterministic dependency-first topological order."""
        cycle = self._find_cycle(self._dependencies)
        if cycle is not None:
            cycle_text = " -> ".join(str(task_id) for task_id in cycle)
            raise TaskDependencyCycleError(f"Task dependency cycle detected: {cycle_text}")

        indegree = {
            task_id: len(dependencies)
            for task_id, dependencies in self._dependencies.items()
        }
        dependents: dict[UUID, list[UUID]] = {task_id: [] for task_id in self._tasks}
        for task_id, dependencies in self._dependencies.items():
            for dependency_id in dependencies:
                dependents[dependency_id].append(task_id)

        queue: list[tuple[str, UUID]] = []
        for task_id, dependency_count in indegree.items():
            if dependency_count == 0:
                heappush(queue, (str(task_id), task_id))

        ordered_ids: list[UUID] = []
        while queue:
            _, task_id = heappop(queue)
            ordered_ids.append(task_id)
            for dependent_id in sorted(dependents[task_id], key=str):
                indegree[dependent_id] -= 1
                if indegree[dependent_id] == 0:
                    heappush(queue, (str(dependent_id), dependent_id))

        if len(ordered_ids) != len(self._tasks):
            raise TaskDependencyCycleError("Task dependency cycle detected")
        return tuple(self._tasks[task_id] for task_id in ordered_ids)

    def _require_task(self, task_id: UUID) -> None:
        if task_id not in self._tasks:
            raise KeyError(f"Unknown task ID: {task_id}")

    @staticmethod
    def _sorted_ids(task_ids: Iterable[UUID]) -> list[UUID]:
        return sorted(task_ids, key=str)

    @staticmethod
    def _find_cycle(
        dependencies: Mapping[UUID, tuple[UUID, ...]],
    ) -> tuple[UUID, ...] | None:
        visiting: set[UUID] = set()
        visited: set[UUID] = set()
        path: list[UUID] = []

        def visit(task_id: UUID) -> tuple[UUID, ...] | None:
            if task_id in visiting:
                cycle_start = path.index(task_id)
                return tuple(path[cycle_start:] + [task_id])
            if task_id in visited:
                return None

            visiting.add(task_id)
            path.append(task_id)
            for dependency_id in sorted(dependencies[task_id], key=str):
                cycle = visit(dependency_id)
                if cycle is not None:
                    return cycle
            path.pop()
            visiting.remove(task_id)
            visited.add(task_id)
            return None

        for task_id in sorted(dependencies, key=str):
            cycle = visit(task_id)
            if cycle is not None:
                return cycle
        return None
