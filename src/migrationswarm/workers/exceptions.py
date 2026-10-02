"""Exceptions raised by the bounded worker runtime."""


class WorkerError(RuntimeError):
    """Base exception for worker coordination errors."""


class WorkerConfigurationError(WorkerError, ValueError):
    """Raised when a worker configuration cannot be used safely."""


class WorkerExecutionError(WorkerError):
    """Raised when a worker cannot complete a durable coordination step."""
