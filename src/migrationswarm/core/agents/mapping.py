"""Central mapping from task types to required agent capabilities."""

from typing import Final

from migrationswarm.core.agents.capabilities import AgentCapability
from migrationswarm.core.tasks.enums import TaskType

TASK_TYPE_TO_CAPABILITY: Final[dict[TaskType, AgentCapability]] = {
    TaskType.REPOSITORY_ANALYSIS: AgentCapability.REPOSITORY_ANALYSIS,
    TaskType.DEPENDENCY_ANALYSIS: AgentCapability.DEPENDENCY_ANALYSIS,
    TaskType.ARCHITECTURE_ANALYSIS: AgentCapability.ARCHITECTURE_ANALYSIS,
    TaskType.SERVICE_BOUNDARY_ANALYSIS: AgentCapability.SERVICE_BOUNDARY_ANALYSIS,
    TaskType.MIGRATION_PLANNING: AgentCapability.MIGRATION_PLANNING,
    TaskType.CODE_REFACTOR: AgentCapability.CODE_REFACTOR,
    TaskType.TEST: AgentCapability.TESTING,
    TaskType.DEBUG: AgentCapability.DEBUGGING,
    TaskType.VERIFY: AgentCapability.VERIFICATION,
    TaskType.SERVICE_EXTRACTION: AgentCapability.SERVICE_EXTRACTION,
}


def required_capability(task_type: TaskType) -> AgentCapability:
    """Return the capability required to execute a task type."""
    return TASK_TYPE_TO_CAPABILITY[task_type]
