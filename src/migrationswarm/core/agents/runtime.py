"""Synchronous worker runtime for executing assigned tasks."""

from migrationswarm.core.agents.exceptions import (
    AgentAssignmentError,
    TaskNotReadyError,
    UnsupportedCapabilityError,
)
from migrationswarm.core.agents.mapping import required_capability
from migrationswarm.core.agents.models import AgentContext, AgentResult
from migrationswarm.core.agents.registry import AgentRegistry
from migrationswarm.core.scheduler.scheduler import TaskScheduler
from migrationswarm.core.tasks.enums import TaskStatus
from migrationswarm.core.tasks.models import Task
from migrationswarm.core.tasks.state_machine import TaskStateMachine


class WorkerRuntime:
    """Coordinate scheduling, agent selection, execution, and task transitions."""

    def __init__(
        self,
        scheduler: TaskScheduler,
        registry: AgentRegistry,
        state_machine: type[TaskStateMachine] | None = None,
    ) -> None:
        self.scheduler = scheduler
        self.registry = registry
        self.state_machine = state_machine or scheduler.state_machine

    def execute(self, task: Task, context: AgentContext | None = None) -> AgentResult:
        """Execute one ready task and stop successful work at VERIFYING."""
        self._ensure_ready(task)
        if task.assigned_agent is None:
            raise AgentAssignmentError(f"Task has no assigned agent: {task.id}")

        agent = self.registry.get(task.assigned_agent)
        capability = required_capability(task.task_type)
        if not agent.supports(capability):
            raise UnsupportedCapabilityError(
                f"Agent {agent.name} does not support capability {capability.value}"
            )

        self.state_machine.transition(task, TaskStatus.RUNNING)
        execution_task = task.model_copy(deep=True)
        execution_context = self._execution_context(task, execution_task, context)
        try:
            result = agent.execute(execution_task, execution_context)
        except Exception:
            self.state_machine.transition(task, TaskStatus.FAILED)
            raise

        if result.success:
            self.state_machine.transition(task, TaskStatus.VERIFYING)
        else:
            self.state_machine.transition(task, TaskStatus.FAILED)
        return result

    def run(self, task: Task, context: AgentContext | None = None) -> AgentResult:
        """Alias for execute to make the runtime's worker role explicit."""
        return self.execute(task, context)

    def _ensure_ready(self, task: Task) -> None:
        if task.status is not TaskStatus.READY:
            raise TaskNotReadyError(
                f"Task must be READY before execution; current status is {task.status.value}"
            )
        ready_ids = {ready_task.id for ready_task in self.scheduler.get_ready_tasks()}
        if task.id not in ready_ids:
            raise TaskNotReadyError(f"Task is not available from the scheduler: {task.id}")

    @staticmethod
    def _execution_context(
        task: Task,
        execution_task: Task,
        context: AgentContext | None,
    ) -> AgentContext:
        if context is None:
            return AgentContext(project_id=task.project_id, task=execution_task)
        return context.model_copy(deep=True, update={"task": execution_task})
