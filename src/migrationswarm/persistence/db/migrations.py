"""Alembic status and upgrade helpers."""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext

from migrationswarm.persistence.db.session import create_engine_for_url
from migrationswarm.persistence.exceptions import PersistenceError


def migration_directory() -> Path:
    """Return migrations from the checkout or the installed distribution."""
    source_root = Path(__file__).resolve().parents[4] / "migrations"
    installed_root = Path(sys.prefix) / "share" / "migrationswarm" / "migrations"
    for candidate in (source_root, installed_root):
        if (candidate / "env.py").is_file() and (candidate / "versions").is_dir():
            return candidate
    raise PersistenceError("Migration assets are not available in this installation")


def alembic_config(database_url: str) -> Config:
    """Build an Alembic config without assuming the current working directory."""
    config = Config()
    config.set_main_option("script_location", str(migration_directory()))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    config.attributes["database_url"] = database_url
    return config


def upgrade_database(database_url: str) -> None:
    """Apply all committed migrations; never create tables implicitly."""
    try:
        command.upgrade(alembic_config(database_url), "head")
    except Exception as error:
        raise PersistenceError("Database migration upgrade failed") from error


def current_revision(database_url: str) -> str | None:
    """Return the current Alembic revision, or ``None`` for an empty database."""
    engine = create_engine_for_url(database_url)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    except Exception as error:
        raise PersistenceError("Could not inspect database migration status") from error
    finally:
        engine.dispose()


__all__ = ["alembic_config", "current_revision", "migration_directory", "upgrade_database"]
