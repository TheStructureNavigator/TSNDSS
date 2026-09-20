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
            _ensure_backward_compatible_columns(connection)
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


def _ensure_backward_compatible_columns(connection: sqlite3.Connection) -> None:
    """Apply additive v0.1 columns needed by newer app builds against existing local databases."""
    site_columns = _table_columns(connection, "sites")
    additions = {
        "lp_artificial_brightness_mcd_m2": "REAL",
        "lp_natural_sky_ratio": "REAL",
        "lp_estimated_total_brightness_mcd_m2": "REAL",
        "lp_estimated_sqm_mag_arcsec2": "REAL",
        "lp_estimated_bortle_class": "INTEGER",
        "lp_dataset_name": "TEXT",
        "lp_provider_name": "TEXT",
        "lp_source": "TEXT",
        "lp_source_unit": "TEXT",
        "lp_data_kind": "TEXT",
        "lp_updated_at": "TEXT",
    }
    for column_name, column_type in additions.items():
        if column_name not in site_columns:
            connection.execute(f"ALTER TABLE sites ADD COLUMN {column_name} {column_type};")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS site_horizon_profile_points (
            site_id TEXT NOT NULL,
            azimuth_deg REAL NOT NULL CHECK (azimuth_deg >= 0 AND azimuth_deg < 360),
            min_altitude_deg REAL NOT NULL CHECK (min_altitude_deg >= 0 AND min_altitude_deg <= 90),
            PRIMARY KEY (site_id, azimuth_deg),
            FOREIGN KEY (site_id) REFERENCES sites(id)
                ON UPDATE CASCADE
                ON DELETE CASCADE
        );
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_site_horizon_profile_site
        ON site_horizon_profile_points (site_id, azimuth_deg);
        """
    )
    connection.commit()


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table_name});").fetchall()}
