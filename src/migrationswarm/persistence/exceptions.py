"""Persistence and coordination errors."""


class PersistenceError(RuntimeError):
    """Base exception for durable persistence failures."""


class DatabaseConnectionError(PersistenceError):
    """Raised when a database connection cannot be established."""


class RecordNotFoundError(PersistenceError):
    """Raised when a requested durable record does not exist."""


class PersistenceIntegrityError(PersistenceError):
    """Raised when a persistence constraint is violated."""


class RedisCoordinationError(PersistenceError):
    """Raised when transient Redis coordination fails."""
