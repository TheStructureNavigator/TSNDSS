from __future__ import annotations

"""Central SQLite schema ownership: versioning, baseline, legacy normalization, migrations.

This module is the only place that changes the database schema. Repositories
assume the schema already exists and never run DDL.

Versioning
    ``PRAGMA user_version`` is the schema version. The current production
    schema is version 7 (``CURRENT_SCHEMA_VERSION``): the v1 baseline plus the
    registered migrations. Downgrades are not
    supported, and opening a database newer than this build supports fails
    with ``SchemaVersionError``.

Fresh databases
    ``initialize_schema`` applies the baseline (``sqlite/schema.sql``, the v1
    schema) in one transaction, then replays any registered migrations. The
    baseline's own ``PRAGMA`` statements are skipped: this module owns
    ``user_version`` and foreign-key handling. There is one schema definition;
    the baseline file is it.

Legacy v1 normalization (frozen)
    Databases created by earlier builds report ``user_version = 1`` but can lack
    later additive v1 objects. ``normalize_legacy_v1`` brings them to the
    complete v1 shape without changing the version and without touching data.
    Its rule set (``LEGACY_V1_*``) describes what historical builds added and
    is FROZEN: never add rules to it. New schema changes are migrations.
    Missing tables and indexes are created from the baseline statements, and
    missing columns are added with the exact types the historical code used
    (so CHECK constraints on those columns are not retrofitted).

Migrations
    ``MIGRATIONS`` is an ordered, forward-only registry. Version N runs on a
    database at version N-1. Each version transition is atomic: its DDL, any
    Python step, the ``foreign_key_check`` and the ``user_version`` update
    commit together or not at all. A failure leaves the previous version intact
    (earlier, already-committed transitions of the same run stay applied).

    Rules for migration authors:
    - Never use ``executescript`` (it commits the open transaction). SQL is
      split with ``sqlite3.complete_statement`` and executed statement by
      statement, so triggers work.
    - Do not issue ``BEGIN``/``COMMIT``/``ROLLBACK``/``SAVEPOINT`` in a migration; the
      registry validation rejects them before anything runs.
    - A Python ``apply`` step shares the transaction and must not commit. The runner
      reports it and refuses to bump the version, but cannot undo work the step
      already committed, so keep such steps to plain statements on the connection.
    - A table rebuild (new table, copy, drop, rename) needs
      ``foreign_keys_off=True``. The pragma cannot change inside a transaction,
      so the runner sets it before ``BEGIN`` and restores it afterwards.
    - Schema-only work belongs here. Anything that reads the filesystem is a
      separate, idempotent data operation, not a migration.

Backups
    Before the first upgrade of an existing database (a real version increase),
    ``Connection.backup`` writes a consistent copy next to the database file:
    ``<db filename>.v<from-version>-<UTC YYYYmmddTHHMMSSZ>.bak`` (for example
    ``tsn_dss.db.v1-20260921T164500Z.bak``; a numeric suffix is added on a
    collision). One backup covers a multi-version run. No backup is written for
    fresh databases, no-op opens, legacy normalization, or in-memory databases.
    Restoring a backup is a manual file copy; there are no downgrade migrations.
"""

import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Sequence

BASELINE_SCHEMA_VERSION = 1
CURRENT_SCHEMA_VERSION = 7


class SchemaVersionError(RuntimeError):
    """The database schema version is unsupported by this build."""


class MigrationError(RuntimeError):
    """A schema initialization, normalization or migration step failed."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One forward step: brings a database at ``version - 1`` to ``version``."""

    version: int
    description: str
    sql: str = ""
    apply: Callable[[sqlite3.Connection], None] | None = None
    foreign_keys_off: bool = False
    # Runs first, inside the migration transaction, before any SQL. Raise ``MigrationError`` to
    # refuse the upgrade (nothing has been changed yet, and the transaction is rolled back).
    precondition: Callable[[sqlite3.Connection], None] | None = None


@dataclass(frozen=True, slots=True)
class MigrationRunResult:
    applied_versions: tuple[int, ...]
    backup_path: Path | None


@dataclass(frozen=True, slots=True)
class SchemaInitializationResult:
    created: bool
    initial_version: int
    final_version: int
    normalized_legacy: bool
    applied_migrations: tuple[int, ...]
    backup_path: Path | None


# v2: canonical Project identity. Schema only; it never reads the filesystem. Existing
# filesystem projects are registered afterwards by ProjectRegistry (engine/project_registry.py).
#
# Both new foreign keys use RESTRICT so that a Project cannot be deleted while a
# MosaicPlan (or a linked Target) depends on it, and a Target cannot be deleted while a
# Project points at it. Nothing is deleted implicitly.
_MIGRATION_2_PROJECTS_SQL = """
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    dir_key TEXT NOT NULL UNIQUE,
    target_id TEXT,
    target_label TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'archived')),
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (target_id) REFERENCES targets(id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT
);

ALTER TABLE mosaic_plans ADD COLUMN project_id TEXT
    REFERENCES projects(id) ON UPDATE CASCADE ON DELETE RESTRICT;

CREATE INDEX idx_mosaic_plans_project_id
ON mosaic_plans (project_id);
"""

def _require_no_legacy_frames(connection: sqlite3.Connection) -> None:
    """v3 replaces the legacy ``frames`` table. Refuse if it holds rows.

    Legacy frames have no Project or Capture. The application never wrote them (only tests
    did), and TSN DSS will not guess ownership for unknown historical rows, so any rows must
    be dealt with explicitly before upgrading.
    """
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'frames'").fetchone()
    if not exists:
        return
    frames = int(connection.execute("SELECT COUNT(*) FROM frames").fetchone()[0])
    if frames:
        raise MigrationError(
            f"Cannot upgrade to schema version 3: the legacy frames table contains {frames} row(s). "
            f"They have no Project or Capture, and TSN DSS does not guess ownership of historical rows. "
            f"Export or remove them, then start again. Nothing has been changed."
        )


# v3: canonical Capture and Frame. Replaces the legacy frames table, which must be empty
# (see _require_no_legacy_frames), so DROP + CREATE loses nothing. It is not a rename-swap
# because v_observation_summary references frames. foreign_keys_off is set because
# dataset_frames references frames(id); it stays valid since the new table keeps that key.
#
# frame_type is nullable: an unknown type stays NULL like every other unknown fact (origin
# keeps the explicit 'unknown' the accepted data model defines). exposure_s allows 0 because
# bias frames have a zero-second exposure. A recorded hash, its size and time are immutable.
_MIGRATION_3_CAPTURES_FRAMES_SQL = """
DROP TABLE IF EXISTS frames;

CREATE TABLE captures (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL CHECK (name <> ''),
    rel_path TEXT NOT NULL CHECK (rel_path <> ''),
    source_kind TEXT NOT NULL
        CHECK (source_kind IN ('legacy_registered', 'folder_import', 'device_import', 'acquisition')),
    source_label TEXT,
    source_path TEXT,
    import_mode TEXT
        CHECK (import_mode IS NULL OR import_mode IN ('copy', 'move', 'acquired')),
    imported_at TEXT,
    registered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    registrar_version TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT,
    UNIQUE (project_id, name),
    UNIQUE (project_id, rel_path)
);

CREATE TABLE frames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL,
    capture_id TEXT NOT NULL,
    observation_id TEXT,
    sequence_id INTEGER,
    rel_path TEXT NOT NULL
        CHECK (
            rel_path <> '' AND
            substr(rel_path, 1, 1) <> '/' AND
            instr(rel_path, char(92)) = 0
        ),
    frame_type TEXT
        CHECK (frame_type IS NULL OR frame_type IN ('light', 'dark', 'flat', 'bias', 'dark_flat')),
    origin TEXT NOT NULL DEFAULT 'unknown'
        CHECK (origin IN ('raw', 'device_stack', 'master', 'unknown')),
    stack_count INTEGER CHECK (stack_count IS NULL OR stack_count > 0),
    file_format TEXT,
    size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    content_sha256 TEXT
        CHECK (
            content_sha256 IS NULL OR
            (length(content_sha256) = 64 AND content_sha256 NOT GLOB '*[^0-9a-f]*')
        ),
    hashed_at TEXT,
    width_px INTEGER CHECK (width_px IS NULL OR width_px > 0),
    height_px INTEGER CHECK (height_px IS NULL OR height_px > 0),
    instrument_name TEXT,
    captured_at TEXT,
    captured_at_source TEXT
        CHECK (
            captured_at_source IS NULL OR
            captured_at_source IN ('fits_header', 'exif', 'device', 'filename', 'user')
        ),
    exposure_s REAL CHECK (exposure_s IS NULL OR exposure_s >= 0),
    gain REAL,
    offset_value REAL,
    iso INTEGER CHECK (iso IS NULL OR iso > 0),
    binning_x INTEGER CHECK (binning_x IS NULL OR binning_x > 0),
    binning_y INTEGER CHECK (binning_y IS NULL OR binning_y > 0),
    camera_temp_c REAL,
    filter_id TEXT,
    filter_name TEXT,
    mount_ra_deg REAL,
    mount_dec_deg REAL,
    guiding_rms_arcsec REAL,
    pointing_source_kind TEXT,
    pointing_source_id TEXT,
    pointing_label TEXT,
    planned_ra_deg REAL CHECK (planned_ra_deg IS NULL OR (planned_ra_deg >= 0 AND planned_ra_deg < 360)),
    planned_dec_deg REAL CHECK (planned_dec_deg IS NULL OR (planned_dec_deg >= -90 AND planned_dec_deg <= 90)),
    accepted INTEGER CHECK (accepted IS NULL OR accepted IN (0, 1)),
    rejection_reason TEXT,
    fwhm_px REAL CHECK (fwhm_px IS NULL OR fwhm_px >= 0),
    fwhm_arcsec REAL CHECK (fwhm_arcsec IS NULL OR fwhm_arcsec >= 0),
    eccentricity REAL CHECK (eccentricity IS NULL OR eccentricity >= 0),
    star_count INTEGER CHECK (star_count IS NULL OR star_count >= 0),
    background_median REAL,
    background_sigma REAL,
    snr_estimate REAL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
        CHECK (json_valid(metadata_json)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT,
    FOREIGN KEY (capture_id) REFERENCES captures(id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT,
    FOREIGN KEY (observation_id) REFERENCES observations(id)
        ON UPDATE CASCADE
        ON DELETE SET NULL,
    FOREIGN KEY (sequence_id) REFERENCES acquisition_sequences(id)
        ON UPDATE CASCADE
        ON DELETE SET NULL,
    FOREIGN KEY (filter_id) REFERENCES equipment(id)
        ON UPDATE CASCADE
        ON DELETE SET NULL,
    UNIQUE (project_id, rel_path)
);

CREATE INDEX idx_frames_capture
ON frames (capture_id);

CREATE INDEX idx_frames_observation
ON frames (observation_id);

CREATE INDEX idx_frames_sequence
ON frames (sequence_id);

CREATE INDEX idx_frames_filter
ON frames (filter_id);

CREATE INDEX idx_frames_captured_at
ON frames (captured_at);

CREATE INDEX idx_frames_content_sha256
ON frames (project_id, content_sha256);

CREATE INDEX idx_frames_quality
ON frames (observation_id, accepted, fwhm_arcsec, guiding_rms_arcsec);

CREATE TRIGGER trg_frames_recorded_hash_is_immutable
BEFORE UPDATE OF content_sha256, size_bytes, hashed_at ON frames
WHEN OLD.content_sha256 IS NOT NULL
 AND (
    NEW.content_sha256 IS NOT OLD.content_sha256 OR
    NEW.size_bytes IS NOT OLD.size_bytes OR
    NEW.hashed_at IS NOT OLD.hashed_at
 )
BEGIN
    SELECT RAISE(ABORT, 'A recorded frame content hash is immutable.');
END;
"""

_MIGRATION_4_CATALOG_SQL = """
CREATE TABLE catalog_objects (
    id TEXT PRIMARY KEY,
    canonical_designation TEXT NOT NULL UNIQUE CHECK (canonical_designation <> ''),
    display_name TEXT NOT NULL CHECK (display_name <> ''),
    ra_deg REAL NOT NULL CHECK (ra_deg >= 0 AND ra_deg < 360),
    dec_deg REAL NOT NULL CHECK (dec_deg >= -90 AND dec_deg <= 90),
    object_type TEXT NOT NULL CHECK (object_type <> ''),
    coordinate_frame TEXT NOT NULL CHECK (coordinate_frame <> ''),
    coordinate_epoch TEXT,
    angular_major_arcmin REAL CHECK (angular_major_arcmin IS NULL OR angular_major_arcmin > 0),
    angular_minor_arcmin REAL CHECK (angular_minor_arcmin IS NULL OR angular_minor_arcmin > 0),
    magnitude REAL,
    source_provider TEXT NOT NULL CHECK (source_provider <> ''),
    source_version TEXT NOT NULL CHECK (source_version <> ''),
    source_external_id TEXT,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE catalog_object_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    catalog_object_id TEXT NOT NULL,
    alias TEXT NOT NULL CHECK (alias <> ''),
    normalized_alias TEXT NOT NULL UNIQUE CHECK (normalized_alias <> ''),
    alias_kind TEXT NOT NULL DEFAULT 'alias' CHECK (alias_kind <> ''),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (catalog_object_id) REFERENCES catalog_objects(id)
        ON UPDATE CASCADE
        ON DELETE CASCADE
);

CREATE INDEX idx_catalog_objects_radec
ON catalog_objects (ra_deg, dec_deg);

CREATE INDEX idx_catalog_objects_type
ON catalog_objects (object_type);

CREATE INDEX idx_catalog_object_aliases_object
ON catalog_object_aliases (catalog_object_id);
"""

_MIGRATION_5_SESSIONS_SQL = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    state TEXT NOT NULL DEFAULT 'planned'
        CHECK (state IN ('planned', 'preparing', 'active', 'closing', 'completed', 'aborted')),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    final_state TEXT
        CHECK (final_state IS NULL OR final_state IN ('completed', 'aborted')),
    operator_id TEXT,
    site_id TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (site_id) REFERENCES sites(id),
    CHECK (state NOT IN ('active', 'closing', 'completed', 'aborted') OR operator_id IS NOT NULL),
    CHECK (
        (state IN ('completed', 'aborted') AND ended_at IS NOT NULL)
        OR (state NOT IN ('completed', 'aborted') AND ended_at IS NULL)
    ),
    CHECK (
        (state IN ('completed', 'aborted') AND final_state = state)
        OR (state NOT IN ('completed', 'aborted') AND final_state IS NULL)
    ),
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);

CREATE INDEX idx_sessions_state
ON sessions (state);

CREATE INDEX idx_sessions_site_id
ON sessions (site_id);
"""

_MIGRATION_6_OBSERVATION_SESSIONS_SQL = """
CREATE TABLE IF NOT EXISTS observations (
    id TEXT PRIMARY KEY,
    observation_number INTEGER UNIQUE,
    target_id TEXT NOT NULL,
    site_id TEXT,
    acquisition_plan_id TEXT,
    status TEXT NOT NULL DEFAULT 'planned'
        CHECK (
            status IN (
                'planned',
                'preparing',
                'running',
                'paused',
                'completed',
                'aborted',
                'failed'
            )
        ),
    started_at TEXT,
    finished_at TEXT,
    operator_notes TEXT,
    weather_notes TEXT,
    moon_illumination_pct REAL
        CHECK (
            moon_illumination_pct IS NULL OR
            moon_illumination_pct BETWEEN 0 AND 100
        ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (target_id) REFERENCES targets(id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT,
    FOREIGN KEY (site_id) REFERENCES sites(id)
        ON UPDATE CASCADE
        ON DELETE SET NULL,
    FOREIGN KEY (acquisition_plan_id) REFERENCES acquisition_plans(id)
        ON UPDATE CASCADE
        ON DELETE SET NULL,
    CHECK (
        finished_at IS NULL OR
        started_at IS NULL OR
        finished_at >= started_at
    )
);

INSERT INTO sessions (id, title, state, started_at, ended_at, final_state, operator_id, site_id, notes)
SELECT
    'session:legacy-development-observations',
    'Legacy development Observation compatibility bucket',
    'planned',
    '1970-01-01T00:00:00+00:00',
    NULL,
    NULL,
    NULL,
    NULL,
    'Synthetic compatibility Session for pre-Wave-2 development/test Observations. Not historical field-session provenance.'
WHERE EXISTS (SELECT 1 FROM observations)
  AND NOT EXISTS (SELECT 1 FROM sessions WHERE id = 'session:legacy-development-observations');

CREATE TABLE observations_new (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    observation_number INTEGER UNIQUE,
    target_id TEXT NOT NULL,
    site_id TEXT,
    acquisition_plan_id TEXT,
    status TEXT NOT NULL DEFAULT 'planned'
        CHECK (
            status IN (
                'planned',
                'preparing',
                'running',
                'paused',
                'completed',
                'aborted',
                'failed'
            )
        ),
    started_at TEXT,
    finished_at TEXT,
    operator_notes TEXT,
    weather_notes TEXT,
    moon_illumination_pct REAL
        CHECK (
            moon_illumination_pct IS NULL OR
            moon_illumination_pct BETWEEN 0 AND 100
        ),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT,
    FOREIGN KEY (target_id) REFERENCES targets(id)
        ON UPDATE CASCADE
        ON DELETE RESTRICT,
    FOREIGN KEY (site_id) REFERENCES sites(id)
        ON UPDATE CASCADE
        ON DELETE SET NULL,
    FOREIGN KEY (acquisition_plan_id) REFERENCES acquisition_plans(id)
        ON UPDATE CASCADE
        ON DELETE SET NULL,
    CHECK (
        finished_at IS NULL OR
        started_at IS NULL OR
        finished_at >= started_at
    )
);

INSERT INTO observations_new (
    id,
    session_id,
    observation_number,
    target_id,
    site_id,
    acquisition_plan_id,
    status,
    started_at,
    finished_at,
    operator_notes,
    weather_notes,
    moon_illumination_pct,
    created_at,
    updated_at
)
SELECT
    id,
    'session:legacy-development-observations',
    observation_number,
    target_id,
    site_id,
    acquisition_plan_id,
    status,
    started_at,
    finished_at,
    operator_notes,
    weather_notes,
    moon_illumination_pct,
    created_at,
    updated_at
FROM observations;

DROP VIEW IF EXISTS v_observation_summary;

DROP TABLE observations;
ALTER TABLE observations_new RENAME TO observations;

CREATE INDEX idx_observations_session
ON observations (session_id);

CREATE INDEX idx_observations_target
ON observations (target_id);

CREATE INDEX idx_observations_site
ON observations (site_id);

CREATE INDEX idx_observations_status
ON observations (status);

CREATE INDEX idx_observations_started_at
ON observations (started_at);

CREATE INDEX idx_observations_acquisition_plan
ON observations (acquisition_plan_id);

CREATE VIEW v_observation_summary AS
SELECT
    o.id AS observation_id,
    o.observation_number,
    o.status,
    t.catalog_id AS target_catalog_id,
    t.name AS target_name,
    s.name AS site_name,
    o.started_at,
    o.finished_at,
    COUNT(f.id) AS frame_count,
    SUM(
        CASE
            WHEN f.frame_type = 'light' AND f.accepted = 1
            THEN COALESCE(f.exposure_s, 0)
            ELSE 0
        END
    ) AS accepted_light_integration_s
FROM observations o
JOIN targets t ON t.id = o.target_id
LEFT JOIN sites s ON s.id = o.site_id
LEFT JOIN frames f ON f.observation_id = o.id
GROUP BY
    o.id,
    o.observation_number,
    o.status,
    t.catalog_id,
    t.name,
    s.name,
    o.started_at,
    o.finished_at;
"""

_MIGRATION_7_PROJECT_SESSIONS_SQL = """
CREATE TABLE project_sessions (
    project_id TEXT NOT NULL,
    session_id TEXT NOT NULL,

    PRIMARY KEY (project_id, session_id),

    FOREIGN KEY (project_id)
        REFERENCES projects(id)
        ON UPDATE CASCADE
        ON DELETE CASCADE,

    FOREIGN KEY (session_id)
        REFERENCES sessions(id)
        ON UPDATE CASCADE
        ON DELETE CASCADE
);

CREATE INDEX idx_project_sessions_session
ON project_sessions (session_id);
"""


# Production registry: ordered, forward-only.
MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=2,
        description="canonical projects table and mosaic_plans.project_id",
        sql=_MIGRATION_2_PROJECTS_SQL,
    ),
    Migration(
        version=3,
        description="canonical captures and frames",
        sql=_MIGRATION_3_CAPTURES_FRAMES_SQL,
        foreign_keys_off=True,
        precondition=_require_no_legacy_frames,
    ),
    Migration(
        version=4,
        description="canonical astronomical catalog objects",
        sql=_MIGRATION_4_CATALOG_SQL,
    ),
    Migration(
        version=5,
        description="canonical sessions",
        sql=_MIGRATION_5_SESSIONS_SQL,
    ),
    Migration(
        version=6,
        description="canonical observation session membership",
        sql=_MIGRATION_6_OBSERVATION_SESSIONS_SQL,
        foreign_keys_off=True,
    ),
    Migration(
        version=7,
        description="canonical project session association",
        sql=_MIGRATION_7_PROJECT_SESSIONS_SQL,
    ),
)

# FROZEN legacy-v1 compatibility rules. Do not extend; add a migration instead.
LEGACY_V1_TABLES: tuple[str, ...] = (
    "site_horizon_profile_points",
    "mosaic_plans",
    "mosaic_panels",
)
LEGACY_V1_INDEXES: tuple[str, ...] = (
    "idx_site_horizon_profile_site",
    "idx_mosaic_plans_project",
    "idx_mosaic_plans_status",
    "idx_mosaic_panels_plan",
    "idx_mosaic_panels_status",
)
LEGACY_V1_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("sites", "lp_artificial_brightness_mcd_m2", "REAL"),
    ("sites", "lp_natural_sky_ratio", "REAL"),
    ("sites", "lp_estimated_total_brightness_mcd_m2", "REAL"),
    ("sites", "lp_estimated_sqm_mag_arcsec2", "REAL"),
    ("sites", "lp_estimated_bortle_class", "INTEGER"),
    ("sites", "lp_dataset_name", "TEXT"),
    ("sites", "lp_provider_name", "TEXT"),
    ("sites", "lp_source", "TEXT"),
    ("sites", "lp_source_unit", "TEXT"),
    ("sites", "lp_data_kind", "TEXT"),
    ("sites", "lp_updated_at", "TEXT"),
    ("mosaic_plans", "observation_type", "TEXT"),
    ("mosaic_plans", "filter", "TEXT"),
)


def get_user_version(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA user_version;").fetchone()[0])


def latest_schema_version(migrations: Sequence[Migration] = MIGRATIONS) -> int:
    """Highest schema version reachable with the given registry."""
    return max([BASELINE_SCHEMA_VERSION, *(migration.version for migration in migrations)])


def validate_migration_registry(migrations: Sequence[Migration]) -> None:
    """Require versions BASELINE+1, BASELINE+2, ... in order with no gaps."""
    expected = BASELINE_SCHEMA_VERSION + 1
    for migration in migrations:
        if migration.version != expected:
            raise MigrationError(
                f"Migration registry must be contiguous and ordered: expected version {expected}, "
                f"got {migration.version}."
            )
        if not migration.sql.strip() and migration.apply is None:
            raise MigrationError(f"Migration {migration.version} defines neither SQL nor an apply step.")
        for statement in split_sql_statements(migration.sql):
            if _TRANSACTION_CONTROL.match(_strip_leading_comments(statement)):
                raise MigrationError(
                    f"Migration {migration.version} contains a transaction-control statement; "
                    f"the runner owns the transaction."
                )
        expected += 1


def split_sql_statements(script: str) -> list[str]:
    """Split a SQL script into single statements without breaking on inner semicolons.

    A ``;`` only ends a statement when ``sqlite3.complete_statement`` agrees, so
    semicolons in string literals, comments and trigger bodies are handled.
    """
    statements: list[str] = []
    start = 0
    position = script.find(";")
    while position != -1:
        candidate = script[start : position + 1]
        if sqlite3.complete_statement(candidate):
            if _has_sql_content(candidate):
                statements.append(candidate.strip())
            start = position + 1
        position = script.find(";", position + 1)

    if _has_sql_content(script[start:]):
        raise MigrationError("SQL script ends with an incomplete statement.")
    return statements


def load_baseline_statements(baseline_path: str | Path) -> list[str]:
    """Read the v1 baseline as executable statements, minus PRAGMA lines."""
    script = Path(baseline_path).read_text(encoding="utf-8")
    statements = [
        statement
        for statement in split_sql_statements(script)
        if not _strip_leading_comments(statement).upper().startswith("PRAGMA")
    ]
    if not any(_CREATE_OBJECT.match(_strip_leading_comments(statement)) for statement in statements):
        raise MigrationError(f"Baseline schema {baseline_path} defines no tables.")
    return statements


def legacy_v1_actions(
    connection: sqlite3.Connection,
    baseline_statements: Sequence[str],
) -> list[str]:
    """SQL statements that would bring a legacy v1 database to the complete v1 shape."""
    definitions = _baseline_definitions(baseline_statements)
    existing_tables = _object_names(connection, "table")
    existing_indexes = _object_names(connection, "index")
    actions: list[str] = []

    for table in LEGACY_V1_TABLES:
        if table not in existing_tables:
            actions.append(_definition(definitions, table))

    for table, column, column_type in LEGACY_V1_COLUMNS:
        if table in existing_tables and column not in _column_names(connection, table):
            actions.append(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")

    for index in LEGACY_V1_INDEXES:
        if index not in existing_indexes:
            actions.append(_definition(definitions, index))

    return actions


def normalize_legacy_v1(
    connection: sqlite3.Connection,
    baseline_statements: Sequence[str],
) -> bool:
    """Atomically apply frozen legacy-v1 compatibility rules. Returns True if anything changed.

    Never changes ``user_version`` and never rewrites data. A no-op does not take a
    write lock.
    """
    if not legacy_v1_actions(connection, baseline_statements):
        return False

    with _autocommit(connection):
        _require_no_transaction(connection)
        connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in legacy_v1_actions(connection, baseline_statements):
                connection.execute(statement)
                _require_open_transaction(connection, statement)
            connection.execute("COMMIT")
        except BaseException as error:
            _rollback_quietly(connection)
            translated = _translate(error, "Legacy v1 schema normalization failed")
            if translated is None:
                raise
            raise translated from error
    return True


def run_migrations(
    connection: sqlite3.Connection,
    migrations: Sequence[Migration] = MIGRATIONS,
    *,
    backup: bool = True,
) -> MigrationRunResult:
    """Apply pending forward migrations in order, one atomic transaction per version."""
    validate_migration_registry(migrations)
    latest = latest_schema_version(migrations)

    with _autocommit(connection):
        current = get_user_version(connection)
        _check_supported_version(current, latest)
        if current < BASELINE_SCHEMA_VERSION:
            raise SchemaVersionError(
                f"Database has no schema version (user_version={current}); migrations start at "
                f"version {BASELINE_SCHEMA_VERSION}."
            )

        pending = sorted(
            (migration for migration in migrations if current < migration.version <= latest),
            key=lambda migration: migration.version,
        )
        if not pending:
            return MigrationRunResult(applied_versions=(), backup_path=None)

        _require_no_transaction(connection)
        backup_path = create_pre_migration_backup(connection, from_version=current) if backup else None

        for migration in pending:
            _apply_migration(connection, migration)

        _validate_integrity(connection)
        return MigrationRunResult(
            applied_versions=tuple(migration.version for migration in pending),
            backup_path=backup_path,
        )


def initialize_schema(
    connection: sqlite3.Connection,
    *,
    baseline_path: str | Path,
    migrations: Sequence[Migration] = MIGRATIONS,
) -> SchemaInitializationResult:
    """The single schema initialization path for fresh and existing databases."""
    validate_migration_registry(migrations)
    latest = latest_schema_version(migrations)

    with _autocommit(connection):
        _require_no_transaction(connection)
        initial_version = get_user_version(connection)
        _check_supported_version(initial_version, latest)

        baseline_statements = load_baseline_statements(baseline_path)
        created = not _has_user_schema(connection)

        if created:
            _create_baseline(connection, baseline_statements)
            initial_version = BASELINE_SCHEMA_VERSION
        elif initial_version < BASELINE_SCHEMA_VERSION:
            raise SchemaVersionError(
                f"Database has user tables but no schema version (user_version={initial_version}); "
                f"TSN DSS cannot upgrade it automatically."
            )

        normalized = False
        if get_user_version(connection) == BASELINE_SCHEMA_VERSION:
            normalized = normalize_legacy_v1(connection, baseline_statements)

        if created or normalized:
            _validate_integrity(connection)

        run_result = run_migrations(connection, migrations, backup=not created)

        return SchemaInitializationResult(
            created=created,
            initial_version=initial_version,
            final_version=get_user_version(connection),
            normalized_legacy=normalized,
            applied_migrations=run_result.applied_versions,
            backup_path=run_result.backup_path,
        )


def create_pre_migration_backup(connection: sqlite3.Connection, *, from_version: int) -> Path | None:
    """Copy a file-backed database next to itself before an upgrade. None for in-memory databases."""
    database_file = _main_database_file(connection)
    if database_file is None:
        return None

    stamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
    backup_path = database_file.with_name(f"{database_file.name}.v{from_version}-{stamp}.bak")
    suffix = 2
    while backup_path.exists():
        backup_path = database_file.with_name(f"{database_file.name}.v{from_version}-{stamp}-{suffix}.bak")
        suffix += 1

    destination = sqlite3.connect(backup_path)
    try:
        connection.backup(destination)
        backed_up_version = get_user_version(destination)
    finally:
        destination.close()

    if backed_up_version != from_version:
        backup_path.unlink(missing_ok=True)
        raise MigrationError(
            f"Pre-migration backup verification failed: expected version {from_version}, "
            f"found {backed_up_version}."
        )
    return backup_path


def _apply_migration(connection: sqlite3.Connection, migration: Migration) -> None:
    label = f"Migration to schema version {migration.version} ({migration.description})"
    foreign_keys_before = _foreign_keys_enabled(connection)
    if migration.foreign_keys_off and foreign_keys_before:
        connection.execute("PRAGMA foreign_keys = OFF")

    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            if migration.precondition is not None:
                migration.precondition(connection)
                _require_open_transaction(connection, "precondition")
            for statement in split_sql_statements(migration.sql):
                connection.execute(statement)
                _require_open_transaction(connection, statement)
            if migration.apply is not None:
                migration.apply(connection)
                _require_open_transaction(connection, "apply step")

            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise MigrationError(f"foreign_key_check found {len(violations)} violation(s).")

            connection.execute(f"PRAGMA user_version = {int(migration.version)}")
            connection.execute("COMMIT")
        except BaseException as error:
            _rollback_quietly(connection)
            translated = _translate(error, f"{label} failed")
            if translated is None:
                raise
            raise translated from error
    finally:
        if migration.foreign_keys_off and foreign_keys_before:
            connection.execute("PRAGMA foreign_keys = ON")


def _create_baseline(connection: sqlite3.Connection, baseline_statements: Sequence[str]) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        for statement in baseline_statements:
            connection.execute(statement)
            _require_open_transaction(connection, statement)
        connection.execute(f"PRAGMA user_version = {BASELINE_SCHEMA_VERSION}")
        connection.execute("COMMIT")
    except BaseException as error:
        _rollback_quietly(connection)
        translated = _translate(error, "Creating the baseline schema failed")
        if translated is None:
            raise
        raise translated from error


def _check_supported_version(version: int, latest: int) -> None:
    if version > latest:
        raise SchemaVersionError(
            f"Database schema version {version} is newer than the newest version this TSN DSS "
            f"build supports ({latest}). Upgrade TSN DSS; schema downgrades are not supported."
        )


def _validate_integrity(connection: sqlite3.Connection) -> None:
    rows = [str(row[0]) for row in connection.execute("PRAGMA integrity_check").fetchall()]
    if rows != ["ok"]:
        raise MigrationError("integrity_check failed: " + "; ".join(rows[:5]))


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


def _object_names(connection: sqlite3.Connection, object_type: str) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = ?;", (object_type,))
    }


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table});").fetchall()}


def _foreign_keys_enabled(connection: sqlite3.Connection) -> bool:
    return bool(connection.execute("PRAGMA foreign_keys").fetchone()[0])


def _main_database_file(connection: sqlite3.Connection) -> Path | None:
    for row in connection.execute("PRAGMA database_list").fetchall():
        if row[1] == "main":
            return Path(row[2]) if row[2] else None
    return None


@contextmanager
def _autocommit(connection: sqlite3.Connection) -> Iterator[None]:
    """Take explicit control of transactions, then restore the caller's isolation mode."""
    previous = connection.isolation_level
    connection.isolation_level = None
    try:
        yield
    finally:
        connection.isolation_level = previous


def _require_no_transaction(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        raise MigrationError("Schema changes require a connection with no open transaction.")


def _require_open_transaction(connection: sqlite3.Connection, what: str) -> None:
    if not connection.in_transaction:
        raise MigrationError(f"{what} ended the migration transaction; migrations must not commit or roll back.")


def _rollback_quietly(connection: sqlite3.Connection) -> None:
    if connection.in_transaction:
        connection.execute("ROLLBACK")


def _translate(error: BaseException, message: str) -> MigrationError | None:
    """Wrap ordinary errors as MigrationError; None means re-raise unchanged (already a
    MigrationError, or KeyboardInterrupt and friends)."""
    if isinstance(error, MigrationError) or not isinstance(error, Exception):
        return None
    return MigrationError(f"{message}: {error}")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


_CREATE_OBJECT = re.compile(
    r"^CREATE\s+(?:TABLE|INDEX|VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)


def _baseline_definitions(baseline_statements: Sequence[str]) -> dict[str, str]:
    definitions: dict[str, str] = {}
    for statement in baseline_statements:
        match = _CREATE_OBJECT.match(_strip_leading_comments(statement))
        if match:
            definitions[match.group(1)] = statement
    return definitions


def _definition(definitions: dict[str, str], name: str) -> str:
    try:
        return definitions[name]
    except KeyError:
        raise MigrationError(f"Baseline schema has no definition for legacy v1 object '{name}'.") from None


def _strip_leading_comments(statement: str) -> str:
    text = statement.lstrip()
    while True:
        if text.startswith("--"):
            newline = text.find("\n")
            text = "" if newline == -1 else text[newline + 1 :].lstrip()
        elif text.startswith("/*"):
            end = text.find("*/")
            text = "" if end == -1 else text[end + 2 :].lstrip()
        else:
            return text


_TRANSACTION_CONTROL = re.compile(r"^(BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE)\b", re.IGNORECASE)

_COMMENTS = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)


def _has_sql_content(text: str) -> bool:
    return bool(_COMMENTS.sub("", text).replace(";", "").strip())
