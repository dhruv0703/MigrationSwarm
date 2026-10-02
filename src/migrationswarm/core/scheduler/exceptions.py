"""Domain-specific dependency graph exceptions."""


class TaskGraphError(ValueError):
    """Base exception for invalid task graph operations."""


class DuplicateTaskError(TaskGraphError):
    """Raised when a task ID already exists in a graph."""


class UnknownDependencyError(TaskGraphError):
    """Raised when a task references a task ID absent from the graph."""


class TaskDependencyCycleError(TaskGraphError):
    """Raised when task dependencies contain a cycle."""
