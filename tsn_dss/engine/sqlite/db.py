from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

EXPECTED_USER_VERSION = 1
SQL_DIR = Path(__file__).resolve().parent.parent.parent.parent / "sqlite"
DEFAULT_SCHEMA_PATH = SQL_DIR / "schema.sql"
DEFAULT_SEED_PATH = SQL_DIR / "seed.sql"


class SchemaVersionError(RuntimeError):
    pass


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
    connection = connect_database(db_path)
    try:
        if _has_user_schema(connection):
            _assert_expected_version(connection)
            return connection

        schema_sql = Path(schema_path).read_text(encoding="utf-8")
        connection.executescript(schema_sql)

        if seed_path is not None:
            seed_sql = Path(seed_path).read_text(encoding="utf-8")
            connection.executescript(seed_sql)

        _assert_expected_version(connection)
        return connection
    except Exception:
        connection.close()
        raise


def get_user_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version;").fetchone()[0])


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


def _has_user_schema(connection: sqlite3.Connection) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type IN ('table', 'view')
          AND name NOT LIKE 'sqlite_%'
        LIMIT 1;
        """
    ).fetchone()
    return row is not None


def _assert_expected_version(connection: sqlite3.Connection) -> None:
    version = get_user_version(connection)
    if version != EXPECTED_USER_VERSION:
        raise SchemaVersionError(
            f"Unsupported TSN DSS schema version: expected {EXPECTED_USER_VERSION}, got {version}."
        )
