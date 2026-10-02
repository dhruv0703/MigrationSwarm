"""Agent contracts, registry, and synchronous worker runtime."""

from migrationswarm.core.agents.base import BaseAgent
from migrationswarm.core.agents.capabilities import AgentCapability
from migrationswarm.core.agents.exceptions import (
    AgentAssignmentError,
    AgentRegistryError,
    DuplicateAgentError,
    TaskNotReadyError,
    UnknownAgentError,
    UnsupportedCapabilityError,
)
from migrationswarm.core.agents.mapping import (
    TASK_TYPE_TO_CAPABILITY,
    required_capability,
)
from migrationswarm.core.agents.models import AgentContext, AgentResult
from migrationswarm.core.agents.registry import AgentRegistry
from migrationswarm.core.agents.runtime import WorkerRuntime

__all__ = [
    "AgentAssignmentError",
    "AgentCapability",
    "AgentContext",
    "AgentRegistry",
    "AgentRegistryError",
    "AgentResult",
    "BaseAgent",
    "DuplicateAgentError",
    "TASK_TYPE_TO_CAPABILITY",
    "TaskNotReadyError",
    "UnknownAgentError",
    "UnsupportedCapabilityError",
    "WorkerRuntime",
    "required_capability",
]
