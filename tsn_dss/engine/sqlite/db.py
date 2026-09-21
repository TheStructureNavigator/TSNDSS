from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .migrations import (
    CURRENT_SCHEMA_VERSION,
    SchemaVersionError,
    get_user_version,
    initialize_schema,
)

EXPECTED_USER_VERSION = CURRENT_SCHEMA_VERSION  # kept as a compatibility alias
SQL_DIR = Path(__file__).resolve().parent.parent.parent.parent / "sqlite"
DEFAULT_SCHEMA_PATH = SQL_DIR / "schema.sql"
DEFAULT_SEED_PATH = SQL_DIR / "seed.sql"


def connect_database(db_path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(Path(db_path))
    configure_connection(connection)
    return connection


def configure_connection(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON;")


def initialize_database(
    db_path: str | Path,
    *,
    schema_path: str | Path = DEFAULT_SCHEMA_PATH,
    seed_path: str | Path | None = None,
) -> sqlite3.Connection:
    """Open a database and bring its schema to the current version.

    This is the single schema initialization path (see ``migrations``): a fresh
    database gets the v1 baseline, a legacy v1 database is normalized, and any
    registered forward migrations are applied. ``seed_path`` is only applied to a
    database that was just created.
    """
    connection = connect_database(db_path)
    try:
        result = initialize_schema(connection, baseline_path=schema_path)
        if result.created and seed_path is not None:
            seed_sql = Path(seed_path).read_text(encoding="utf-8")
            connection.executescript(seed_sql)
        if get_user_version(connection) != CURRENT_SCHEMA_VERSION:
            raise SchemaVersionError(
                f"Unexpected schema version after initialization: expected {CURRENT_SCHEMA_VERSION}, "
                f"got {get_user_version(connection)}."
            )
        return connection
    except Exception:
        connection.close()
        raise


def integrity_check(connection: sqlite3.Connection) -> str:
    return str(connection.execute("PRAGMA integrity_check;").fetchone()[0])


def foreign_key_violations(connection: sqlite3.Connection) -> list[tuple]:
    return [tuple(row) for row in connection.execute("PRAGMA foreign_key_check;").fetchall()]


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    if connection.in_transaction:
        raise RuntimeError("Nested transactions are not supported in TSN DSS v0.1.")

    connection.execute("BEGIN;")
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()
