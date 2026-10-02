"""Synchronous task readiness scheduler."""

from typing import ClassVar

from migrationswarm.core.scheduler.graph import TaskGraph
from migrationswarm.core.tasks.enums import TaskStatus
from migrationswarm.core.tasks.models import Task
from migrationswarm.core.tasks.state_machine import TaskStateMachine


class TaskScheduler:
    """Promote dependency-ready pending tasks without executing them."""

    _default_state_machine: ClassVar[type[TaskStateMachine]] = TaskStateMachine

    def __init__(
        self,
        graph: TaskGraph,
        state_machine: type[TaskStateMachine] | None = None,
    ) -> None:
        self.graph = graph
        self.state_machine = state_machine or self._default_state_machine

    def promote_ready_tasks(self) -> tuple[Task, ...]:
        """Promote all currently eligible pending tasks to READY."""
        eligible_tasks = self.graph.eligible_tasks()
        for task in eligible_tasks:
            self.state_machine.transition(task, TaskStatus.READY)
        return eligible_tasks

    def get_ready_tasks(self) -> tuple[Task, ...]:
        """Return all READY tasks in deterministic dependency order."""
        return tuple(
            task for task in self.graph.topological_order() if task.status is TaskStatus.READY
        )

    def schedule(self) -> tuple[Task, ...]:
        """Promote eligible tasks and return all tasks currently ready to execute."""
        self.promote_ready_tasks()
        return self.get_ready_tasks()
