from __future__ import annotations

"""Read-only bootstrap for the TSN DSS MCP adapter."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..engine.sqlite.db import configure_connection
from ..engine.sqlite.migrations import CURRENT_SCHEMA_VERSION, get_user_version


class McpBootstrapError(RuntimeError):
    """The MCP server cannot safely open the requested TSN DSS database."""


def resolve_database_path(
    *,
    projects_root: str | Path = "projects",
    database_path: str | Path | None = None,
) -> Path:
    return Path(database_path) if database_path is not None else Path(projects_root) / "tsn_dss.db"


def open_readonly_database(
    *,
    projects_root: str | Path = "projects",
    database_path: str | Path | None = None,
) -> sqlite3.Connection:
    path = resolve_database_path(projects_root=projects_root, database_path=database_path)
    if not path.exists():
        raise McpBootstrapError(f"TSN DSS database does not exist: {path}")
    if not path.is_file():
        raise McpBootstrapError(f"TSN DSS database path is not a file: {path}")

    resolved = path.resolve()
    uri = f"file:{resolved.as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as error:
        raise McpBootstrapError(f"Could not open TSN DSS database read-only: {error}") from error

    try:
        configure_connection(connection)
        version = get_user_version(connection)
        if version < CURRENT_SCHEMA_VERSION:
            raise McpBootstrapError(
                f"TSN DSS database schema is older than this build: {version} < {CURRENT_SCHEMA_VERSION}. "
                "Open it with TSN DSS first if you intend to migrate it."
            )
        if version > CURRENT_SCHEMA_VERSION:
            raise McpBootstrapError(
                f"TSN DSS database schema is newer than this build: {version} > {CURRENT_SCHEMA_VERSION}."
            )
        return connection
    except Exception:
        connection.close()
        raise


@contextmanager
def readonly_database(
    *,
    projects_root: str | Path = "projects",
    database_path: str | Path | None = None,
) -> Iterator[sqlite3.Connection]:
    connection = open_readonly_database(projects_root=projects_root, database_path=database_path)
    try:
        yield connection
    finally:
        connection.close()
