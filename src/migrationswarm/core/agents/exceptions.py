"""Domain-specific agent registry and worker runtime exceptions."""


class AgentRegistryError(ValueError):
    """Base exception for agent registry failures."""


class DuplicateAgentError(AgentRegistryError):
    """Raised when an agent name is already registered."""


class UnknownAgentError(AgentRegistryError):
    """Raised when an agent name is not registered."""


class UnsupportedCapabilityError(AgentRegistryError):
    """Raised when an agent cannot perform a task's required capability."""


class AgentAssignmentError(AgentRegistryError):
    """Raised when a task has no assigned agent."""


class TaskNotReadyError(AgentRegistryError):
    """Raised when a worker receives a task that is not ready to execute."""
