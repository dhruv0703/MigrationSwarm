"""Domain-specific task lifecycle exceptions."""


class TaskTransitionError(ValueError):
    """Raised when a task lifecycle transition is not allowed."""


class TaskRetryLimitError(TaskTransitionError):
    """Raised when a failed task has no retries remaining."""
