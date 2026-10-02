"""SQLAlchemy engine and transaction helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from migrationswarm.persistence.db.models import Base


def create_engine_for_url(database_url: str, *, echo: bool = False) -> Engine:
    """Create a synchronous SQLAlchemy engine for SQLite or PostgreSQL."""
    connect_args: dict[str, Any] = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    return create_engine(database_url, echo=echo, future=True, connect_args=connect_args)


class Database:
    """Own an engine and session factory without imposing persistence on the domain."""

    def __init__(self, database_url: str, *, echo: bool = False) -> None:
        self.database_url = database_url
        self.engine = create_engine_for_url(database_url, echo=echo)
        self.session_factory = sessionmaker(
            bind=self.engine, autoflush=False, expire_on_commit=False, class_=Session
        )

    def create_all_for_tests(self) -> None:
        """Create isolated test tables; application initialization uses Alembic instead."""
        Base.metadata.create_all(self.engine)

    def check_connectivity(self) -> bool:
        """Run a minimal connectivity query."""
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return True

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Yield one transaction-scoped session and propagate failures."""
        with self.session_factory.begin() as session:
            yield session

    def dispose(self) -> None:
        self.engine.dispose()


__all__ = ["Database", "create_engine_for_url"]
