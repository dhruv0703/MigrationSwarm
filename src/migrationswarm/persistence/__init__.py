"""Durable database and transient coordination adapters."""

from migrationswarm.persistence.exceptions import (
    DatabaseConnectionError,
    PersistenceError,
    RecordNotFoundError,
    RedisCoordinationError,
)


def __getattr__(name: str) -> object:
    """Load mapping/service exports lazily to keep domain imports acyclic."""
    if name in {"AgentExecution", "Project"}:
        from migrationswarm.persistence import mapping

        return getattr(mapping, name)
    if name == "MigrationStateService":
        from migrationswarm.persistence.service import MigrationStateService

        return MigrationStateService
    raise AttributeError(name)


__all__ = [
    "AgentExecution",
    "DatabaseConnectionError",
    "MigrationStateService",
    "PersistenceError",
    "Project",
    "RecordNotFoundError",
    "RedisCoordinationError",
]
