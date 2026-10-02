"""Optional PostgreSQL integration coverage."""

import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from migrationswarm.persistence.db import Database
from migrationswarm.persistence.db.migrations import upgrade_database
from migrationswarm.persistence.db.repositories import ProjectRepository
from migrationswarm.persistence.mapping import Project

POSTGRES_URL = os.environ.get("MIGRATIONSWARM_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="Set MIGRATIONSWARM_TEST_POSTGRES_URL for PostgreSQL integration tests",
)


def test_postgres_migration_and_project_round_trip() -> None:
    assert POSTGRES_URL is not None
    upgrade_database(POSTGRES_URL)
    database = Database(POSTGRES_URL)
    project = Project(
        id=uuid4(),
        name="PostgreSQL integration",
        repository_path=Path("/workspace/example"),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    ProjectRepository(database.session_factory).create(project)
    assert ProjectRepository(database.session_factory).get_required(project.id).id == project.id
    database.dispose()
