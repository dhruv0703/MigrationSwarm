"""SQLAlchemy database adapters."""

from migrationswarm.persistence.db.models import Base
from migrationswarm.persistence.db.session import Database, create_engine_for_url


def __getattr__(name: str) -> object:
    """Load repositories lazily to keep mapping and ORM imports acyclic."""
    if name in {
        "AgentExecutionRepository",
        "MigrationRunRepository",
        "ProjectRepository",
        "TaskRepository",
        "RepairAttemptRepository",
        "MultiMigrationRunRepository",
    }:
        from migrationswarm.persistence.db import repositories

        return getattr(repositories, name)
    raise AttributeError(name)


__all__ = [
    "AgentExecutionRepository",
    "Base",
    "Database",
    "MigrationRunRepository",
    "ProjectRepository",
    "RepairAttemptRepository",
    "MultiMigrationRunRepository",
    "TaskRepository",
    "create_engine_for_url",
]
