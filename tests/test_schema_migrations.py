from __future__ import annotations

"""Tests for the central SQLite schema infrastructure (tsn_dss.engine.sqlite.migrations)."""

import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from tsn_dss.domain.models import LocalHorizonPoint, MosaicPlan, Observation, Site, Target
from tsn_dss.engine.sqlite.datasets import DatasetRepository
from tsn_dss.engine.sqlite.db import (
    DEFAULT_SCHEMA_PATH,
    DEFAULT_SEED_PATH,
    EXPECTED_USER_VERSION,
    connect_database,
    foreign_key_violations,
    initialize_database,
    integrity_check,
)
from tsn_dss.engine.sqlite.frames import FrameRepository
from tsn_dss.engine.sqlite.migrations import (
    BASELINE_SCHEMA_VERSION,
    CURRENT_SCHEMA_VERSION,
    LEGACY_V1_COLUMNS,
    LEGACY_V1_INDEXES,
    LEGACY_V1_TABLES,
    MIGRATIONS,
    Migration,
    MigrationError,
    SchemaVersionError,
    create_pre_migration_backup,
    get_user_version,
    initialize_schema,
    latest_schema_version,
    load_baseline_statements,
    run_migrations,
    split_sql_statements,
    validate_migration_registry,
)
from tsn_dss.engine.sqlite.mosaics import MosaicRepository
from tsn_dss.engine.sqlite.observation import ObservationRepository
from tsn_dss.engine.sqlite.planning import PlanningRepository
from tsn_dss.engine.sqlite.processing import ProcessingRunRepository

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent

# The infrastructure tests below exercise the runner against the v1 baseline. "V1_ONLY" is an
# empty registry: the v1-only world, independent of whichever migrations production has added.
# Tests about production behaviour pass MIGRATIONS (the default) instead.
V1_ONLY: tuple = ()


# ---------------------------------------------------------------------------
# Semantic schema comparison helper
# ---------------------------------------------------------------------------


def describe_schema(connection: sqlite3.Connection) -> dict:
    """Structural description of a schema that ignores column order, DDL text and
    generated auto-index names. CHECK constraints are intentionally not compared."""
    tables: dict[str, dict] = {}
    for (table,) in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name;"
    ).fetchall():
        columns = {
            row[1]: {"type": str(row[2]).upper(), "notnull": bool(row[3]), "default": row[4], "pk": row[5]}
            for row in connection.execute(f'PRAGMA table_info("{table}");').fetchall()
        }
        foreign_keys = frozenset(
            (row[3], row[2], row[4], row[5], row[6])  # from, table, to, on_update, on_delete
            for row in connection.execute(f'PRAGMA foreign_key_list("{table}");').fetchall()
        )
        indexes = set()
        for row in connection.execute(f'PRAGMA index_list("{table}");').fetchall():
            name, unique, origin = row[1], bool(row[2]), row[3]
            index_columns = tuple(info[2] for info in connection.execute(f'PRAGMA index_info("{name}");').fetchall())
            if origin == "c":
                indexes.add(("index", name, unique, index_columns))
            else:  # UNIQUE / PRIMARY KEY constraint: the generated name is not meaningful
                indexes.add((origin, unique, index_columns))
        tables[table] = {"columns": columns, "foreign_keys": foreign_keys, "indexes": frozenset(indexes)}

    views = {
        name: " ".join(str(sql).split())
        for name, sql in connection.execute("SELECT name, sql FROM sqlite_master WHERE type = 'view';").fetchall()
    }
    triggers = {
        name: " ".join(str(sql).split())
        for name, sql in connection.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger';").fetchall()
    }
    return {"tables": tables, "views": views, "triggers": triggers}


def schema_diff(left: dict, right: dict) -> list[str]:
    """Human-readable differences, for assertion messages."""
    problems: list[str] = []
    for kind in ("tables", "views", "triggers"):
        for name in sorted(set(left[kind]) ^ set(right[kind])):
            problems.append(f"{kind[:-1]} {name} only on one side")
    for name in sorted(set(left["tables"]) & set(right["tables"])):
        a, b = left["tables"][name], right["tables"][name]
        for part in ("columns", "foreign_keys", "indexes"):
            if a[part] != b[part]:
                problems.append(f"table {name}: {part} differ")
    for kind in ("views", "triggers"):
        for name in sorted(set(left[kind]) & set(right[kind])):
            if left[kind][name] != right[kind][name]:
                problems.append(f"{kind[:-1]} {name} differs")
    return problems


# ---------------------------------------------------------------------------
# Legacy v1 fixtures, derived from the baseline so no schema is written twice
# ---------------------------------------------------------------------------

_CREATE_NAME = re.compile(r"^CREATE\s+(?:TABLE|INDEX|VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", re.IGNORECASE)
_HORIZON_OBJECTS = {"site_horizon_profile_points", "idx_site_horizon_profile_site"}
_MOSAIC_OBJECTS = {
    "mosaic_plans",
    "mosaic_panels",
    "idx_mosaic_plans_project",
    "idx_mosaic_plans_status",
    "idx_mosaic_panels_plan",
    "idx_mosaic_panels_status",
}

LEGACY_VARIANTS = ("2026-08-23", "2026-08-29", "2026-09-01")


def _strip_comments(statement: str) -> str:
    return re.sub(r"^(\s*--[^\n]*\n)+", "", statement)


def build_legacy_v1_database(path: Path, variant: str) -> None:
    """Create a database shaped like a historical v1 build, at user_version = 1.

    2026-08-23: original baseline (no lp_* site columns, no horizon table, no mosaic tables)
    2026-08-29: + mosaic tables that lack observation_type / filter
    2026-09-01: + lp_* site columns added by ALTER, mosaic with intent columns, still no horizon table
    """
    definitions: dict[str, str] = {}
    ordered: list[tuple[str, str]] = []
    for statement in load_baseline_statements(DEFAULT_SCHEMA_PATH):
        match = _CREATE_NAME.match(_strip_comments(statement))
        name = match.group(1) if match else ""
        definitions[name] = statement
        ordered.append((name, statement))

    old_sites = re.sub(
        r"    lp_artificial_brightness_mcd_m2.*?    lp_updated_at TEXT,\n", "", definitions["sites"], flags=re.DOTALL
    )
    assert old_sites != definitions["sites"], "fixture failed to strip the lp_* block"
    plans_without_intent = definitions["mosaic_plans"].replace("    observation_type TEXT,\n    filter TEXT,\n", "")
    assert plans_without_intent != definitions["mosaic_plans"], "fixture failed to strip the intent columns"

    connection = sqlite3.connect(path, isolation_level=None)
    try:
        for name, statement in ordered:
            if name in _HORIZON_OBJECTS:
                continue
            if name in _MOSAIC_OBJECTS and variant == "2026-08-23":
                continue
            if name == "sites":
                statement = old_sites
            elif name == "mosaic_plans" and variant == "2026-08-29":
                statement = plans_without_intent
            connection.execute(statement)
        if variant == "2026-09-01":
            for table, column, column_type in LEGACY_V1_COLUMNS:
                if table == "sites":
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
        connection.execute("PRAGMA user_version = 1")
    finally:
        connection.close()


def populate_legacy_data(path: Path, variant: str) -> None:
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute(
            """
            INSERT INTO sites (id, name, latitude_deg, longitude_deg, elevation_m, sqm_mag_arcsec2,
                               bortle_class, south_horizon_open, notes)
            VALUES ('site:legacy', 'Legacy Field', 49.5, 19.9, 410.0, 21.3, 3, 1, 'dark corner');
            """
        )
        if variant == "2026-09-01":
            connection.execute(
                """
                UPDATE sites SET
                    lp_artificial_brightness_mcd_m2 = 0.1785,
                    lp_natural_sky_ratio = 1.04,
                    lp_estimated_total_brightness_mcd_m2 = 0.3497,
                    lp_estimated_sqm_mag_arcsec2 = 21.22,
                    lp_estimated_bortle_class = 4,
                    lp_dataset_name = 'New World Atlas',
                    lp_provider_name = 'local-raster',
                    lp_source = 'Falchi et al. 2016',
                    lp_source_unit = 'mcd/m2',
                    lp_data_kind = 'modeled',
                    lp_updated_at = '2026-09-01T10:00:00+00:00'
                WHERE id = 'site:legacy';
                """
            )
        if variant in ("2026-08-29", "2026-09-01"):
            columns = "id, project_slug, name, target_name, imaging_profile_id, imaging_profile_label, " \
                      "fov_width_deg, fov_height_deg, center_ra_deg, center_dec_deg, region_width_deg, " \
                      "region_height_deg, rotation_deg, overlap_percent, status, selected_panel_id"
            connection.execute(
                f"""
                INSERT INTO mosaic_plans ({columns})
                VALUES ('mosaic:legacy', 'Cygnus Loop', 'Cygnus Loop Test', 'Cygnus Loop', 'simulator-default',
                        'Seestar S30 Pro tele profile', 2.59, 1.47, 313.5, 30.7, 5.0, 3.0, 12.5, 25.0,
                        'draft', 'panel:legacy-2');
                """
            )
            if variant == "2026-09-01":
                connection.execute(
                    "UPDATE mosaic_plans SET observation_type = 'dual-band imaging', filter = 'L-eXtreme' "
                    "WHERE id = 'mosaic:legacy';"
                )
            for index, label in enumerate(("P01", "P02")):
                connection.execute(
                    """
                    INSERT INTO mosaic_panels (id, mosaic_plan_id, panel_index, panel_label, center_ra_deg,
                                               center_dec_deg, fov_width_deg, fov_height_deg, rotation_deg,
                                               row_index, column_index, status, target_integration_seconds,
                                               acquired_integration_seconds)
                    VALUES (?, 'mosaic:legacy', ?, ?, ?, 30.7, 2.59, 1.47, 12.5, 0, ?, 'in_progress', 7200.0, 3600.5);
                    """,
                    (f"panel:legacy-{index + 1}", index, label, 312.0 + index * 1.9, index),
                )
    finally:
        connection.close()


def snapshot_rows(connection: sqlite3.Connection) -> dict[str, tuple[list[str], list[tuple]]]:
    """All rows of every user table, with the column names present at snapshot time."""
    snapshot: dict[str, tuple[list[str], list[tuple]]] = {}
    for (table,) in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name;"
    ).fetchall():
        columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}");').fetchall()]
        quoted = ", ".join(f'"{column}"' for column in columns)
        rows = [tuple(row) for row in connection.execute(f'SELECT {quoted} FROM "{table}" ORDER BY rowid;').fetchall()]
        snapshot[table] = (columns, rows)
    return snapshot


def rows_for_columns(connection: sqlite3.Connection, table: str, columns: list[str]) -> list[tuple]:
    quoted = ", ".join(f'"{column}"' for column in columns)
    return [tuple(row) for row in connection.execute(f'SELECT {quoted} FROM "{table}" ORDER BY rowid;').fetchall()]


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.dir = Path(self.temp_dir.name)
        self.db_path = self.dir / "tsn.db"

    def track(self, connection: sqlite3.Connection) -> sqlite3.Connection:
        self.addCleanup(self._close_quietly, connection)
        return connection

    @staticmethod
    def _close_quietly(connection: sqlite3.Connection) -> None:
        try:
            connection.close()
        except sqlite3.ProgrammingError:
            pass

    def open_initialized(self, path: Path | None = None) -> sqlite3.Connection:
        """A database at the current production schema."""
        return self.track(initialize_database(path or self.db_path))

    def open_v1(self, path: Path | None = None) -> sqlite3.Connection:
        """A database at the v1 baseline only (no production migrations applied)."""
        connection = self.track(connect_database(path or self.db_path))
        initialize_schema(connection, baseline_path=DEFAULT_SCHEMA_PATH, migrations=V1_ONLY)
        return connection

    def raw(self, path: Path | None = None) -> sqlite3.Connection:
        return self.track(sqlite3.connect(path or self.db_path))

    def backups(self) -> list[str]:
        return sorted(item.name for item in self.dir.glob("*.bak"))

    def initialize(self, migrations=MIGRATIONS, path: Path | None = None):
        connection = self.raw(path)
        result = initialize_schema(connection, baseline_path=DEFAULT_SCHEMA_PATH, migrations=migrations)
        return connection, result


# ---------------------------------------------------------------------------
# Comparison helper self-tests
# ---------------------------------------------------------------------------


class SchemaDescriptionTests(unittest.TestCase):
    def _connection(self, *statements: str) -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:")
        for statement in statements:
            connection.execute(statement)
        return connection

    def test_column_order_is_ignored(self) -> None:
        a = self._connection("CREATE TABLE t (a TEXT NOT NULL, b INTEGER DEFAULT 3)")
        b = self._connection("CREATE TABLE t (b INTEGER DEFAULT 3, a TEXT NOT NULL)")
        self.assertEqual(describe_schema(a), describe_schema(b))

    def test_type_nullability_default_and_indexes_are_compared(self) -> None:
        base = describe_schema(self._connection("CREATE TABLE t (a TEXT, b INTEGER)"))
        variants = {
            "type": "CREATE TABLE t (a TEXT, b REAL)",
            "nullability": "CREATE TABLE t (a TEXT NOT NULL, b INTEGER)",
            "default": "CREATE TABLE t (a TEXT DEFAULT 'x', b INTEGER)",
            "unique": "CREATE TABLE t (a TEXT UNIQUE, b INTEGER)",
            "primary key": "CREATE TABLE t (a TEXT PRIMARY KEY, b INTEGER)",
        }
        for label, ddl in variants.items():
            with self.subTest(label):
                self.assertNotEqual(base, describe_schema(self._connection(ddl)))

    def test_explicit_index_names_and_foreign_key_actions_are_compared(self) -> None:
        with_index = describe_schema(
            self._connection("CREATE TABLE t (a TEXT)", "CREATE INDEX idx_a ON t (a)")
        )
        renamed = describe_schema(
            self._connection("CREATE TABLE t (a TEXT)", "CREATE INDEX idx_other ON t (a)")
        )
        self.assertNotEqual(with_index, renamed)

        cascade = describe_schema(
            self._connection(
                "CREATE TABLE p (id INTEGER PRIMARY KEY)",
                "CREATE TABLE c (pid INTEGER REFERENCES p(id) ON DELETE CASCADE)",
            )
        )
        restrict = describe_schema(
            self._connection(
                "CREATE TABLE p (id INTEGER PRIMARY KEY)",
                "CREATE TABLE c (pid INTEGER REFERENCES p(id) ON DELETE RESTRICT)",
            )
        )
        self.assertNotEqual(cascade, restrict)


# ---------------------------------------------------------------------------
# Version behaviour
# ---------------------------------------------------------------------------


class VersionBehaviourTests(TempDirTestCase):
    def test_production_registry_is_valid_and_matches_current_version(self) -> None:
        validate_migration_registry(MIGRATIONS)
        self.assertEqual(CURRENT_SCHEMA_VERSION, 5)
        self.assertEqual([migration.version for migration in MIGRATIONS], [2, 3, 4, 5])
        self.assertEqual(latest_schema_version(MIGRATIONS), CURRENT_SCHEMA_VERSION)
        self.assertEqual(EXPECTED_USER_VERSION, CURRENT_SCHEMA_VERSION)

    def test_fresh_database_replays_production_migrations_without_a_backup(self) -> None:
        connection, result = self.initialize()
        self.assertTrue(result.created)
        self.assertEqual((result.initial_version, result.final_version), (1, CURRENT_SCHEMA_VERSION))
        self.assertEqual(result.applied_migrations, (2, 3, 4, 5))
        self.assertIsNone(result.backup_path)
        self.assertEqual(get_user_version(connection), CURRENT_SCHEMA_VERSION)
        self.assertEqual(self.backups(), [])

    def test_fresh_database_is_created_at_version_one_when_only_the_baseline_is_registered(self) -> None:
        connection, result = self.initialize(migrations=V1_ONLY)
        self.assertTrue(result.created)
        self.assertEqual((result.initial_version, result.final_version), (1, 1))
        self.assertFalse(result.normalized_legacy)
        self.assertEqual(result.applied_migrations, ())
        self.assertIsNone(result.backup_path)
        self.assertEqual(get_user_version(connection), 1)
        self.assertEqual(self.backups(), [])

    def test_fresh_database_contains_complete_v1_baseline(self) -> None:
        connection = self.open_initialized()
        schema = describe_schema(connection)
        for table in ("targets", "sites", "site_horizon_profile_points", "mosaic_plans", "mosaic_panels",
                      "frames", "datasets", "processing_runs", "catalog_objects", "catalog_object_aliases",
                      "sessions"):
            self.assertIn(table, schema["tables"])
        self.assertIn("lp_data_kind", schema["tables"]["sites"]["columns"])
        self.assertIn("filter", schema["tables"]["mosaic_plans"]["columns"])
        self.assertIn("v_observation_summary", schema["views"])

    def test_v5_session_migration_preserves_observations_without_session_links(self) -> None:
        connection, result = self.initialize(migrations=MIGRATIONS[:-1])
        connection.row_factory = sqlite3.Row
        self.assertEqual(result.final_version, 4)
        PlanningRepository(connection).create_target(
            Target(id="target:m42", catalog="M", catalog_id="42", name="Orion Nebula", ra_deg=83.8, dec_deg=-5.4)
        )
        ObservationRepository(connection).create_observation(
            Observation(id="obs:legacy", target_id="target:m42", status="planned")
        )
        connection.close()

        connection, result = self.initialize()
        connection.row_factory = sqlite3.Row

        self.assertEqual(result.applied_migrations, (5,))
        self.assertEqual(get_user_version(connection), CURRENT_SCHEMA_VERSION)
        self.assertIn("sessions", describe_schema(connection)["tables"])
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM sessions;").fetchone()[0], 0)
        self.assertIsNotNone(ObservationRepository(connection).get_observation("obs:legacy"))
        observation_columns = {row[1] for row in connection.execute("PRAGMA table_info(observations);").fetchall()}
        self.assertNotIn("session_id", observation_columns)

    def test_reopening_current_v1_is_a_no_op(self) -> None:
        self.open_initialized().close()
        connection = self.raw()
        before_schema = describe_schema(connection)
        before_cookie = connection.execute("PRAGMA schema_version").fetchone()[0]
        connection.close()

        connection, result = self.initialize()
        self.assertFalse(result.created)
        self.assertFalse(result.normalized_legacy)
        self.assertEqual(result.applied_migrations, ())
        self.assertIsNone(result.backup_path)
        self.assertEqual(describe_schema(connection), before_schema)
        # schema_version increments on any DDL, so equality proves nothing was altered.
        self.assertEqual(connection.execute("PRAGMA schema_version").fetchone()[0], before_cookie)
        self.assertEqual(self.backups(), [])

    def test_repeated_initialization_is_idempotent(self) -> None:
        first, _ = self.initialize()
        snapshot = describe_schema(first)
        first.close()
        for _ in range(3):
            connection, result = self.initialize()
            self.assertFalse(result.created)
            self.assertFalse(result.normalized_legacy)
            self.assertEqual(describe_schema(connection), snapshot)
            connection.close()

    def test_database_newer_than_supported_fails_clearly(self) -> None:
        self.open_initialized().close()
        raw = self.raw()
        raw.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 1}")
        raw.close()

        with self.assertRaises(SchemaVersionError) as caught:
            initialize_database(self.db_path)
        message = str(caught.exception)
        self.assertIn(str(CURRENT_SCHEMA_VERSION + 1), message)
        self.assertIn("newer", message)
        self.assertIn("downgrade", message)

    def test_database_newer_than_injected_registry_fails_and_is_untouched(self) -> None:
        self.open_initialized().close()
        raw = self.raw()
        raw.execute("PRAGMA user_version = 3")
        raw.close()

        two = Migration(2, "two", sql="CREATE TABLE two (x INTEGER);")
        with self.assertRaises(SchemaVersionError):
            self.initialize(migrations=[two])
        with self.assertRaises(SchemaVersionError):
            run_migrations(self.raw(), [two])

        check = self.raw()
        self.assertEqual(get_user_version(check), 3)
        self.assertEqual(self.backups(), [])

    def test_unversioned_database_with_tables_is_refused(self) -> None:
        raw = self.raw()
        raw.execute("CREATE TABLE something (x INTEGER)")
        raw.commit()
        with self.assertRaises(SchemaVersionError):
            self.initialize()
        self.assertEqual(self.backups(), [])

    def test_run_migrations_refuses_unversioned_database_directly(self) -> None:
        raw = self.raw()
        raw.execute("CREATE TABLE something (x INTEGER)")
        raw.commit()
        with self.assertRaises(SchemaVersionError):
            run_migrations(raw, [Migration(2, "two", sql="CREATE TABLE two (x INTEGER);")])

    def test_initialize_database_leaves_connection_usable_and_in_legacy_mode(self) -> None:
        connection = self.open_initialized()
        self.assertFalse(connection.in_transaction)
        self.assertEqual(connection.isolation_level, "")
        self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(integrity_check(connection), "ok")
        self.assertEqual(foreign_key_violations(connection), [])

    def test_seed_is_applied_only_to_a_newly_created_database(self) -> None:
        first = self.track(initialize_database(self.db_path, seed_path=DEFAULT_SEED_PATH))
        seeded = first.execute("SELECT COUNT(*) FROM targets").fetchone()[0]
        self.assertGreater(seeded, 0)
        first.close()

        second = self.track(initialize_database(self.db_path, seed_path=DEFAULT_SEED_PATH))
        self.assertEqual(second.execute("SELECT COUNT(*) FROM targets").fetchone()[0], seeded)
        self.assertEqual(get_user_version(second), CURRENT_SCHEMA_VERSION)

    def test_frozen_legacy_rule_set_matches_baseline(self) -> None:
        # Tripwire: the legacy normalizer is frozen. Extending it must be a deliberate decision.
        self.assertEqual(len(LEGACY_V1_TABLES), 3)
        self.assertEqual(len(LEGACY_V1_INDEXES), 5)
        self.assertEqual(len(LEGACY_V1_COLUMNS), 13)

        fresh = describe_schema(self.open_initialized())
        for table in LEGACY_V1_TABLES:
            self.assertIn(table, fresh["tables"])
        declared_indexes = {
            entry[1] for table in fresh["tables"].values() for entry in table["indexes"] if entry[0] == "index"
        }
        for index in LEGACY_V1_INDEXES:
            self.assertIn(index, declared_indexes)
        for table, column, column_type in LEGACY_V1_COLUMNS:
            self.assertEqual(fresh["tables"][table]["columns"][column]["type"], column_type)
            self.assertFalse(fresh["tables"][table]["columns"][column]["notnull"])


# ---------------------------------------------------------------------------
# Legacy v1 normalization: convergence and data preservation
# ---------------------------------------------------------------------------


class LegacyNormalizationTests(TempDirTestCase):
    def fresh_schema(self, migrations=V1_ONLY) -> dict:
        fresh_path = self.dir / "fresh-reference.db"
        connection = self.track(sqlite3.connect(fresh_path))
        initialize_schema(connection, baseline_path=DEFAULT_SCHEMA_PATH, migrations=migrations)
        schema = describe_schema(connection)
        connection.close()
        fresh_path.unlink()
        return schema

    def test_legacy_fixtures_really_differ_from_current_v1(self) -> None:
        fresh = self.fresh_schema()
        for variant in LEGACY_VARIANTS:
            with self.subTest(variant):
                path = self.dir / f"legacy-{variant}.db"
                build_legacy_v1_database(path, variant)
                connection = self.raw(path)
                self.assertEqual(get_user_version(connection), 1)
                self.assertNotEqual(describe_schema(connection), fresh)
                connection.close()

    def test_normalized_legacy_databases_converge_to_the_fresh_schema(self) -> None:
        fresh = self.fresh_schema()
        for variant in LEGACY_VARIANTS:
            with self.subTest(variant):
                path = self.dir / f"legacy-{variant}.db"
                build_legacy_v1_database(path, variant)

                connection, result = self.initialize(migrations=V1_ONLY, path=path)
                self.assertTrue(result.normalized_legacy)
                self.assertFalse(result.created)
                self.assertEqual(result.final_version, 1)
                self.assertEqual(schema_diff(describe_schema(connection), fresh), [])
                self.assertEqual(describe_schema(connection), fresh)
                self.assertEqual(get_user_version(connection), 1)
                self.assertEqual(integrity_check(connection), "ok")
                self.assertEqual(foreign_key_violations(connection), [])
                connection.close()

    def test_normalization_preserves_existing_rows_exactly(self) -> None:
        for variant in LEGACY_VARIANTS:
            with self.subTest(variant):
                path = self.dir / f"legacy-data-{variant}.db"
                build_legacy_v1_database(path, variant)
                populate_legacy_data(path, variant)

                before_connection = self.raw(path)
                before = snapshot_rows(before_connection)
                before_connection.close()

                connection, _ = self.initialize(path=path)
                for table, (columns, rows) in before.items():
                    self.assertEqual(rows_for_columns(connection, table, columns), rows, f"{table} changed")
                connection.close()

    def test_normalization_adds_new_columns_as_null_and_new_tables_empty(self) -> None:
        path = self.dir / "legacy-2026-08-23.db"
        build_legacy_v1_database(path, "2026-08-23")
        populate_legacy_data(path, "2026-08-23")

        connection, _ = self.initialize(path=path)
        site = connection.execute("SELECT * FROM sites WHERE id = 'site:legacy'").fetchone()
        columns = [row[1] for row in connection.execute("PRAGMA table_info(sites)").fetchall()]
        values = dict(zip(columns, site))
        for _, column, _ in (entry for entry in LEGACY_V1_COLUMNS if entry[0] == "sites"):
            self.assertIsNone(values[column], column)
        self.assertEqual(values["south_horizon_open"], 1)
        for table in ("site_horizon_profile_points", "mosaic_plans", "mosaic_panels"):
            self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_normalization_never_creates_backups_or_bumps_version(self) -> None:
        for variant in LEGACY_VARIANTS:
            with self.subTest(variant):
                path = self.dir / f"legacy-{variant}.db"
                build_legacy_v1_database(path, variant)
                connection, result = self.initialize(migrations=V1_ONLY, path=path)
                self.assertIsNone(result.backup_path)
                self.assertEqual(get_user_version(connection), 1)
                connection.close()
        self.assertEqual(self.backups(), [])

    def test_normalization_is_a_no_op_the_second_time(self) -> None:
        path = self.dir / "legacy-2026-08-29.db"
        build_legacy_v1_database(path, "2026-08-29")
        populate_legacy_data(path, "2026-08-29")
        first, first_result = self.initialize(path=path)
        self.assertTrue(first_result.normalized_legacy)
        cookie = first.execute("PRAGMA schema_version").fetchone()[0]
        first.close()

        second, second_result = self.initialize(path=path)
        self.assertFalse(second_result.normalized_legacy)
        self.assertEqual(second.execute("PRAGMA schema_version").fetchone()[0], cookie)

    def test_partial_legacy_fixture_from_the_original_test_still_normalizes(self) -> None:
        raw = self.raw()
        raw.executescript(
            """
            PRAGMA user_version = 1;
            CREATE TABLE sites (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, latitude_deg REAL, longitude_deg REAL,
                elevation_m REAL, sqm_mag_arcsec2 REAL, bortle_class INTEGER,
                south_horizon_open INTEGER NOT NULL DEFAULT 0, notes TEXT
            );
            INSERT INTO sites (id, name) VALUES ('site:partial', 'Partial');
            """
        )
        raw.close()

        connection = self.open_v1()
        columns = {row[1] for row in connection.execute("PRAGMA table_info(sites)").fetchall()}
        self.assertIn("lp_updated_at", columns)
        self.assertEqual(connection.execute("SELECT name FROM sites").fetchone()[0], "Partial")
        self.assertEqual(get_user_version(connection), 1)

    def test_partial_legacy_fixture_also_upgrades_to_the_current_schema(self) -> None:
        # Even this artificial database (only a sites table) must normalize and then migrate.
        raw = self.raw()
        raw.executescript(
            "PRAGMA user_version = 1;"
            "CREATE TABLE sites (id TEXT PRIMARY KEY, name TEXT NOT NULL, latitude_deg REAL, longitude_deg REAL,"
            " elevation_m REAL, sqm_mag_arcsec2 REAL, bortle_class INTEGER,"
            " south_horizon_open INTEGER NOT NULL DEFAULT 0, notes TEXT);"
            "INSERT INTO sites (id, name) VALUES ('site:partial', 'Partial');"
        )
        raw.close()

        connection = self.open_initialized()
        self.assertEqual(get_user_version(connection), CURRENT_SCHEMA_VERSION)
        self.assertEqual(connection.execute("SELECT name FROM sites").fetchone()[0], "Partial")
        self.assertIn("projects", describe_schema(connection)["tables"])

    def test_complete_site_horizon_and_mosaic_data_survives_reopen_through_repositories(self) -> None:
        connection = self.open_initialized()
        planning = PlanningRepository(connection)
        mosaics = MosaicRepository(connection)

        site = planning.create_site(
            Site(
                id="site:full",
                name="Full Site",
                latitude_deg=49.5,
                longitude_deg=19.9,
                elevation_m=410.0,
                sqm_mag_arcsec2=21.3,
                bortle_class=3,
                lp_artificial_brightness_mcd_m2=0.1785,
                lp_natural_sky_ratio=1.04,
                lp_estimated_total_brightness_mcd_m2=0.3497,
                lp_estimated_sqm_mag_arcsec2=21.22,
                lp_estimated_bortle_class=4,
                lp_dataset_name="New World Atlas",
                lp_provider_name="local-raster",
                lp_source="Falchi et al. 2016",
                lp_source_unit="mcd/m2",
                lp_data_kind="modeled",
                lp_updated_at="2026-09-20T12:00:00+00:00",
                south_horizon_open=True,
                notes="modeled, not measured",
                horizon_profile=[
                    LocalHorizonPoint(azimuth_deg=0.0, min_altitude_deg=5.0),
                    LocalHorizonPoint(azimuth_deg=90.0, min_altitude_deg=22.5),
                    LocalHorizonPoint(azimuth_deg=200.0, min_altitude_deg=12.0),
                ],
            )
        )
        plan = mosaics.save_mosaic_plan(
            MosaicPlan(
                id="mosaic:full",
                project_slug="M27 — Dumbbell Nebula",
                name="Dumbbell",
                target_name="M27",
                observation_type="dual-band imaging",
                filter="L-eXtreme",
                imaging_profile_id="simulator-default",
                imaging_profile_label="Seestar S30 Pro tele profile",
                fov_width_deg=2.59,
                fov_height_deg=1.47,
                center_ra_deg=299.9,
                center_dec_deg=22.7,
                region_width_deg=5.0,
                region_height_deg=3.0,
                overlap_percent=25.0,
            )
        )
        mosaics.generate_panels(plan.id)
        expected_plan = mosaics.get_mosaic_plan(plan.id)
        self.assertGreater(len(expected_plan.panels), 1)
        connection.close()

        reopened = self.open_initialized()
        self.assertEqual(PlanningRepository(reopened).get_site("site:full"), site)
        restored = MosaicRepository(reopened).get_mosaic_plan("mosaic:full")
        self.assertEqual(restored, expected_plan)
        self.assertEqual(restored.observation_type, "dual-band imaging")
        self.assertEqual(restored.filter, "L-eXtreme")

    def test_legacy_data_is_readable_through_repositories_after_normalization(self) -> None:
        path = self.dir / "legacy-2026-09-01.db"
        build_legacy_v1_database(path, "2026-09-01")
        populate_legacy_data(path, "2026-09-01")

        connection = self.open_initialized(path)
        site = PlanningRepository(connection).get_site("site:legacy")
        self.assertEqual(site.lp_artificial_brightness_mcd_m2, 0.1785)
        self.assertEqual(site.lp_estimated_bortle_class, 4)
        self.assertEqual(site.lp_data_kind, "modeled")
        self.assertEqual(site.lp_updated_at, "2026-09-01T10:00:00+00:00")
        self.assertTrue(site.south_horizon_open)
        self.assertEqual(site.horizon_profile, [])

        plan = MosaicRepository(connection).get_mosaic_plan("mosaic:legacy")
        self.assertEqual((plan.observation_type, plan.filter), ("dual-band imaging", "L-eXtreme"))
        self.assertEqual([panel.panel_label for panel in plan.panels], ["P01", "P02"])
        self.assertEqual(plan.panels[0].acquired_integration_seconds, 3600.5)

        # The newly created horizon table is immediately usable.
        site.horizon_profile = [LocalHorizonPoint(azimuth_deg=45.0, min_altitude_deg=10.0)]
        updated = PlanningRepository(connection).update_site(site)
        self.assertEqual(updated.horizon_profile, site.horizon_profile)

    def test_legacy_mosaic_without_intent_columns_gets_null_intent(self) -> None:
        path = self.dir / "legacy-2026-08-29.db"
        build_legacy_v1_database(path, "2026-08-29")
        populate_legacy_data(path, "2026-08-29")

        connection = self.open_initialized(path)
        plan = MosaicRepository(connection).get_mosaic_plan("mosaic:legacy")
        self.assertIsNone(plan.observation_type)
        self.assertIsNone(plan.filter)
        self.assertEqual(plan.name, "Cygnus Loop Test")
        self.assertEqual(plan.rotation_deg, 12.5)
        self.assertEqual(len(plan.panels), 2)


# ---------------------------------------------------------------------------
# Repositories must not evolve the schema
# ---------------------------------------------------------------------------


class RepositorySchemaIsolationTests(TempDirTestCase):
    def test_constructing_repositories_never_changes_the_schema(self) -> None:
        build_legacy_v1_database(self.db_path, "2026-08-23")  # no mosaic tables, no horizon table
        connection = self.track(connect_database(self.db_path))
        before = describe_schema(connection)
        cookie = connection.execute("PRAGMA schema_version").fetchone()[0]

        PlanningRepository(connection)
        MosaicRepository(connection)
        ObservationRepository(connection)
        FrameRepository(connection)
        DatasetRepository(connection)
        ProcessingRunRepository(connection)

        self.assertEqual(describe_schema(connection), before)
        self.assertEqual(connection.execute("PRAGMA schema_version").fetchone()[0], cookie)
        self.assertNotIn("mosaic_plans", describe_schema(connection)["tables"])

    def test_mosaic_repository_does_not_create_missing_tables_when_used(self) -> None:
        build_legacy_v1_database(self.db_path, "2026-08-23")
        connection = self.track(connect_database(self.db_path))
        repository = MosaicRepository(connection)
        with self.assertRaises(sqlite3.OperationalError):
            repository.list_mosaic_plans()
        self.assertNotIn("mosaic_plans", describe_schema(connection)["tables"])

    def test_only_the_schema_layer_contains_ddl(self) -> None:
        pattern = re.compile(r"\b(CREATE|ALTER|DROP)\s+(TABLE|INDEX|VIEW|TRIGGER)\b|executescript", re.IGNORECASE)
        allowed = {"migrations.py", "db.py"}  # db.py only executes the optional seed script
        offenders = []
        for source in sorted((REPOSITORY_ROOT / "tsn_dss").rglob("*.py")):
            if source.name in allowed and source.parent.name == "sqlite":
                continue
            if pattern.search(source.read_text(encoding="utf-8")):
                offenders.append(str(source.relative_to(REPOSITORY_ROOT)))
        self.assertEqual(offenders, [])


# ---------------------------------------------------------------------------
# SQL splitting and registry validation
# ---------------------------------------------------------------------------


class SqlSplittingTests(unittest.TestCase):
    def test_semicolons_inside_strings_and_comments_do_not_split(self) -> None:
        script = """
            -- leading comment; with a semicolon
            INSERT INTO t VALUES ('a;b');  -- trailing; comment
            /* block; comment */ INSERT INTO t VALUES ("c;d");
        """
        statements = split_sql_statements(script)
        self.assertEqual(len(statements), 2)
        self.assertIn("'a;b'", statements[0])
        self.assertIn('"c;d"', statements[1])

    def test_multiple_statements_on_one_line(self) -> None:
        self.assertEqual(
            split_sql_statements("CREATE TABLE a (x); CREATE TABLE b (y); DROP TABLE a;"),
            ["CREATE TABLE a (x);", "CREATE TABLE b (y);", "DROP TABLE a;"],
        )

    def test_trigger_bodies_are_kept_intact(self) -> None:
        script = """
            CREATE TABLE t (id INTEGER);
            CREATE TABLE log (msg TEXT);
            CREATE TRIGGER trg AFTER INSERT ON t
            BEGIN
                INSERT INTO log VALUES ('one;');
                INSERT INTO log VALUES ('two');
            END;
            SELECT 1;
        """
        statements = split_sql_statements(script)
        self.assertEqual(len(statements), 4)
        self.assertTrue(statements[2].startswith("CREATE TRIGGER"))
        self.assertTrue(statements[2].rstrip().endswith("END;"))
        self.assertIn("'two'", statements[2])

    def test_trailing_comment_and_blank_statements_are_ignored(self) -> None:
        self.assertEqual(split_sql_statements("SELECT 1;\n;\n-- done\n"), ["SELECT 1;"])

    def test_incomplete_final_statement_is_an_error(self) -> None:
        with self.assertRaises(MigrationError):
            split_sql_statements("SELECT 1; SELECT 2")

    def test_baseline_loads_without_pragmas(self) -> None:
        statements = load_baseline_statements(DEFAULT_SCHEMA_PATH)
        self.assertGreater(len(statements), 30)
        self.assertFalse(any(re.match(r"(?is)\s*(--[^\n]*\n\s*)*PRAGMA", statement) for statement in statements))
        self.assertTrue(all(sqlite3.complete_statement(statement) for statement in statements))


class RegistryValidationTests(unittest.TestCase):
    def test_valid_registry(self) -> None:
        validate_migration_registry(
            [Migration(2, "a", sql="SELECT 1;"), Migration(3, "b", apply=lambda connection: None)]
        )
        self.assertEqual(latest_schema_version([Migration(2, "a", sql="SELECT 1;")]), 2)

    def test_invalid_registries_are_rejected(self) -> None:
        cases = {
            "must start after the baseline": [Migration(3, "x", sql="SELECT 1;")],
            "gap": [Migration(2, "a", sql="SELECT 1;"), Migration(4, "b", sql="SELECT 1;")],
            "duplicate": [Migration(2, "a", sql="SELECT 1;"), Migration(2, "b", sql="SELECT 1;")],
            "out of order": [Migration(3, "b", sql="SELECT 1;"), Migration(2, "a", sql="SELECT 1;")],
            "empty migration": [Migration(2, "nothing")],
            "transaction control": [Migration(2, "x", sql="CREATE TABLE a (x); COMMIT;")],
            "savepoint": [Migration(2, "x", sql="SAVEPOINT s;")],
            "incomplete sql": [Migration(2, "x", sql="CREATE TABLE a (x)")],
        }
        for label, registry in cases.items():
            with self.subTest(label), self.assertRaises(MigrationError):
                validate_migration_registry(registry)

    def test_word_starting_with_end_is_not_transaction_control(self) -> None:
        validate_migration_registry([Migration(2, "x", sql="CREATE TABLE ending (x);")])


# ---------------------------------------------------------------------------
# Migration runner: atomicity, ordering, foreign keys
# ---------------------------------------------------------------------------


def _boom(connection: sqlite3.Connection) -> None:
    raise RuntimeError("boom from apply step")


class MigrationRunnerTests(TempDirTestCase):
    def prepare_v1(self) -> None:
        connection = self.open_v1()
        connection.execute(
            "INSERT INTO targets (id, catalog, catalog_id, name, ra_deg, dec_deg) "
            "VALUES ('t:1', 'M', '31', 'Andromeda', 10.68, 41.27)"
        )
        connection.commit()
        connection.close()

    def observe(self) -> tuple[int, set[str], list[tuple]]:
        """State as seen by a brand-new connection, so only committed state counts."""
        view = self.raw()
        try:
            tables = {row[0] for row in view.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            targets = [tuple(row) for row in view.execute("SELECT id, name FROM targets ORDER BY id")]
            return get_user_version(view), tables, targets
        finally:
            view.close()

    def test_successful_migration_changes_schema_and_version_together(self) -> None:
        self.prepare_v1()
        migration = Migration(
            2,
            "add tag column and probe table",
            sql="""
                -- adds a column; and a table
                ALTER TABLE targets ADD COLUMN test_tag TEXT;
                CREATE TABLE migration_probe (id INTEGER PRIMARY KEY, note TEXT);
                INSERT INTO migration_probe (note) VALUES ('semi;colon in string');
            """,
        )
        connection, result = self.initialize(migrations=[migration])

        self.assertEqual(result.applied_migrations, (2,))
        self.assertEqual((result.initial_version, result.final_version), (1, 2))
        self.assertFalse(result.created)
        version, tables, targets = self.observe()
        self.assertEqual(version, 2)
        self.assertIn("migration_probe", tables)
        self.assertEqual(targets, [("t:1", "Andromeda")])
        view = self.raw()
        self.assertIn("test_tag", {row[1] for row in view.execute("PRAGMA table_info(targets)")})
        self.assertEqual(view.execute("SELECT note FROM migration_probe").fetchone()[0], "semi;colon in string")
        self.assertFalse(connection.in_transaction)

    def test_failing_sql_migration_leaves_schema_data_and_version_unchanged(self) -> None:
        self.prepare_v1()
        before = self.observe()
        failing = Migration(
            2,
            "half applied",
            sql="""
                ALTER TABLE targets ADD COLUMN test_tag TEXT;
                CREATE TABLE half_done (id INTEGER);
                INSERT INTO half_done VALUES (1);
                INSERT INTO table_that_does_not_exist VALUES (1);
            """,
        )
        connection = self.raw()
        with self.assertRaises(MigrationError) as caught:
            initialize_schema(connection, baseline_path=DEFAULT_SCHEMA_PATH, migrations=[failing])
        self.assertIn("version 2", str(caught.exception))

        self.assertEqual(self.observe(), before)
        self.assertNotIn("half_done", self.observe()[1])
        view = self.raw()
        self.assertNotIn("test_tag", {row[1] for row in view.execute("PRAGMA table_info(targets)")})
        self.assertFalse(connection.in_transaction)

    def test_failing_apply_step_rolls_back_earlier_sql_of_the_same_migration(self) -> None:
        self.prepare_v1()
        before = self.observe()
        failing = Migration(
            2,
            "sql then python failure",
            sql="CREATE TABLE before_boom (id INTEGER); INSERT INTO before_boom VALUES (1);",
            apply=_boom,
        )
        with self.assertRaises(MigrationError) as caught:
            self.initialize(migrations=[failing])
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        self.assertEqual(self.observe(), before)

    def test_apply_step_changes_commit_atomically_with_the_version(self) -> None:
        self.prepare_v1()

        def rename_targets(connection: sqlite3.Connection) -> None:
            connection.execute("UPDATE targets SET name = name || '!'")

        self.initialize(migrations=[Migration(2, "python step", apply=rename_targets)])
        version, _, targets = self.observe()
        self.assertEqual(version, 2)
        self.assertEqual(targets, [("t:1", "Andromeda!")])

    def test_apply_step_that_commits_is_reported_and_version_is_not_bumped(self) -> None:
        self.prepare_v1()
        with self.assertRaises(MigrationError):
            self.initialize(migrations=[Migration(2, "commits", apply=lambda c: c.execute("COMMIT"))])
        self.assertEqual(self.observe()[0], 1)

    def test_migrations_apply_in_order_and_each_version_is_atomic(self) -> None:
        self.prepare_v1()
        ok = Migration(2, "ok", sql="CREATE TABLE v2_table (x INTEGER);")
        bad = Migration(3, "fails", sql="CREATE TABLE v3_table (x INTEGER); INSERT INTO nope VALUES (1);")
        with self.assertRaises(MigrationError) as caught:
            self.initialize(migrations=[ok, bad])
        self.assertIn("version 3", str(caught.exception))

        version, tables, _ = self.observe()
        self.assertEqual(version, 2)  # the first transition committed on its own
        self.assertIn("v2_table", tables)
        self.assertNotIn("v3_table", tables)

    def test_multi_step_upgrade_runs_in_version_order(self) -> None:
        self.prepare_v1()
        order: list[int] = []
        steps = [
            Migration(2, "two", apply=lambda c: order.append(2)),
            Migration(3, "three", apply=lambda c: order.append(3)),
            Migration(4, "four", apply=lambda c: order.append(4)),
        ]
        connection, result = self.initialize(migrations=steps)
        self.assertEqual(order, [2, 3, 4])
        self.assertEqual(result.applied_migrations, (2, 3, 4))
        self.assertEqual(get_user_version(connection), 4)

    def test_only_pending_migrations_run_when_partly_upgraded(self) -> None:
        self.prepare_v1()
        calls: list[int] = []
        steps = [
            Migration(2, "two", apply=lambda c: calls.append(2)),
            Migration(3, "three", apply=lambda c: calls.append(3)),
        ]
        self.initialize(migrations=steps[:1])
        self.assertEqual(calls, [2])
        connection, result = self.initialize(migrations=steps)
        self.assertEqual(calls, [2, 3])
        self.assertEqual(result.applied_migrations, (3,))
        _, again = self.initialize(migrations=steps)
        self.assertEqual(again.applied_migrations, ())
        self.assertEqual(calls, [2, 3])

    def test_trigger_created_by_a_migration_works(self) -> None:
        self.prepare_v1()
        migration = Migration(
            2,
            "audit trigger",
            sql="""
                CREATE TABLE audit (msg TEXT);
                CREATE TRIGGER trg_targets_insert AFTER INSERT ON targets
                BEGIN
                    INSERT INTO audit (msg) VALUES ('inserted;' || NEW.id);
                    INSERT INTO audit (msg) VALUES ('second');
                END;
            """,
        )
        connection, _ = self.initialize(migrations=[migration])
        connection.execute(
            "INSERT INTO targets (id, catalog, catalog_id, name, ra_deg, dec_deg) "
            "VALUES ('t:2', 'M', '42', 'Orion', 83.8, -5.4)"
        )
        connection.commit()
        self.assertEqual(
            [row[0] for row in connection.execute("SELECT msg FROM audit ORDER BY rowid")],
            ["inserted;t:2", "second"],
        )

    def test_integrity_and_foreign_key_checks_are_clean_after_migration(self) -> None:
        self.prepare_v1()
        connection, _ = self.initialize(migrations=[Migration(2, "add", sql="CREATE TABLE extra (x INTEGER);")])
        self.assertEqual(integrity_check(connection), "ok")
        self.assertEqual(foreign_key_violations(connection), [])

    # -- foreign key handling -------------------------------------------------

    def seed_parent_child(self) -> None:
        connection = self.open_v1()
        connection.executescript(
            """
            CREATE TABLE parent (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE child (
                id INTEGER PRIMARY KEY,
                parent_id INTEGER REFERENCES parent(id) ON DELETE CASCADE
            );
            INSERT INTO parent (id, name) VALUES (1, 'a'), (2, 'b');
            INSERT INTO child (id, parent_id) VALUES (10, 1), (11, 1), (12, 2);
            """
        )
        connection.commit()
        connection.close()

    REBUILD_PARENT = """
        CREATE TABLE parent_new (id INTEGER PRIMARY KEY, name TEXT, extra TEXT DEFAULT 'x');
        INSERT INTO parent_new (id, name) SELECT id, name FROM parent;
        DROP TABLE parent;
        ALTER TABLE parent_new RENAME TO parent;
    """

    def child_count(self) -> int:
        view = self.raw()
        try:
            return view.execute("SELECT COUNT(*) FROM child").fetchone()[0]
        finally:
            view.close()

    def test_table_rebuild_with_foreign_keys_off_preserves_child_rows(self) -> None:
        self.seed_parent_child()
        connection = self.track(connect_database(self.db_path))
        self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)

        result = initialize_schema(
            connection,
            baseline_path=DEFAULT_SCHEMA_PATH,
            migrations=[Migration(2, "rebuild parent", sql=self.REBUILD_PARENT, foreign_keys_off=True)],
        )

        self.assertEqual(result.applied_migrations, (2,))
        self.assertEqual(self.child_count(), 3)
        self.assertIn("extra", {row[1] for row in connection.execute("PRAGMA table_info(parent)")})
        self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)  # restored
        self.assertEqual(foreign_key_violations(connection), [])

    def test_table_rebuild_without_the_flag_would_cascade_delete_child_rows(self) -> None:
        # Documents why foreign_keys_off exists and proves the previous test is meaningful.
        self.seed_parent_child()
        connection = self.track(connect_database(self.db_path))
        initialize_schema(
            connection,
            baseline_path=DEFAULT_SCHEMA_PATH,
            migrations=[Migration(2, "rebuild parent", sql=self.REBUILD_PARENT, foreign_keys_off=False)],
        )
        self.assertEqual(self.child_count(), 0)

    def test_foreign_key_check_failure_rolls_the_migration_back(self) -> None:
        self.seed_parent_child()
        before_child_count = self.child_count()
        connection = self.track(connect_database(self.db_path))
        violating = Migration(
            2,
            "introduces an orphan",
            sql="INSERT INTO child (id, parent_id) VALUES (99, 12345);",
            foreign_keys_off=True,  # enforcement is off, so only foreign_key_check can catch it
        )
        with self.assertRaises(MigrationError) as caught:
            initialize_schema(connection, baseline_path=DEFAULT_SCHEMA_PATH, migrations=[violating])
        self.assertIn("foreign_key_check", str(caught.exception))
        self.assertEqual(self.child_count(), before_child_count)
        self.assertEqual(self.observe()[0], 1)
        self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)  # restored after failure

    def test_foreign_key_state_is_left_alone_when_a_migration_does_not_ask_for_it(self) -> None:
        self.prepare_v1()
        raw = self.raw()  # a plain connection: foreign keys default to OFF
        initialize_schema(
            raw,
            baseline_path=DEFAULT_SCHEMA_PATH,
            migrations=[Migration(2, "x", sql="CREATE TABLE x (a INTEGER);")],
        )
        self.assertEqual(raw.execute("PRAGMA foreign_keys").fetchone()[0], 0)


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------


class BackupTests(TempDirTestCase):
    UPGRADE = Migration(2, "upgrade", sql="ALTER TABLE targets ADD COLUMN backup_probe TEXT;")

    def make_v1_with_data(self) -> None:
        connection = self.open_v1()
        connection.execute(
            "INSERT INTO targets (id, catalog, catalog_id, name, ra_deg, dec_deg) "
            "VALUES ('t:1', 'M', '31', 'Andromeda', 10.68, 41.27)"
        )
        connection.commit()
        connection.close()

    def test_backup_is_created_for_a_real_upgrade_with_a_deterministic_name(self) -> None:
        self.make_v1_with_data()
        moment = datetime(2026, 9, 21, 16, 45, 0, tzinfo=timezone.utc)
        with patch("tsn_dss.engine.sqlite.migrations._utc_now", return_value=moment):
            _, result = self.initialize(migrations=[self.UPGRADE])

        self.assertEqual(result.backup_path, self.dir / "tsn.db.v1-20260921T164500Z.bak")
        self.assertEqual(self.backups(), ["tsn.db.v1-20260921T164500Z.bak"])

        backup = self.raw(result.backup_path)
        self.assertEqual(get_user_version(backup), 1)
        self.assertNotIn("backup_probe", {row[1] for row in backup.execute("PRAGMA table_info(targets)")})
        self.assertEqual(backup.execute("SELECT name FROM targets").fetchone()[0], "Andromeda")

        upgraded = self.raw()
        self.assertEqual(get_user_version(upgraded), 2)
        self.assertIn("backup_probe", {row[1] for row in upgraded.execute("PRAGMA table_info(targets)")})

    def test_backup_name_collision_gets_a_numeric_suffix(self) -> None:
        self.make_v1_with_data()
        moment = datetime(2026, 9, 21, 16, 45, 0, tzinfo=timezone.utc)
        connection = self.raw()
        with patch("tsn_dss.engine.sqlite.migrations._utc_now", return_value=moment):
            first = create_pre_migration_backup(connection, from_version=1)
            second = create_pre_migration_backup(connection, from_version=1)
        self.assertEqual(first.name, "tsn.db.v1-20260921T164500Z.bak")
        self.assertEqual(second.name, "tsn.db.v1-20260921T164500Z-2.bak")

    def test_one_backup_covers_a_multi_version_upgrade(self) -> None:
        self.make_v1_with_data()
        steps = [
            self.UPGRADE,
            Migration(3, "second", sql="CREATE TABLE second_step (x INTEGER);"),
        ]
        _, result = self.initialize(migrations=steps)
        self.assertEqual(result.applied_migrations, (2, 3))
        self.assertEqual(len(self.backups()), 1)
        self.assertIn(".v1-", self.backups()[0])

    def test_backup_is_kept_when_the_migration_fails(self) -> None:
        self.make_v1_with_data()
        failing = Migration(2, "fails", sql="INSERT INTO missing_table VALUES (1);")
        with self.assertRaises(MigrationError):
            self.initialize(migrations=[failing])
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(get_user_version(self.raw()), 1)

    def test_no_backup_for_fresh_noop_or_normalization(self) -> None:
        self.initialize(migrations=V1_ONLY)  # fresh
        self.initialize(migrations=V1_ONLY)  # no-op
        legacy = self.dir / "legacy.db"
        build_legacy_v1_database(legacy, "2026-08-23")
        self.initialize(migrations=V1_ONLY, path=legacy)  # legacy normalization (no version change)
        self.assertEqual(self.backups(), [])

    def test_no_backup_for_a_fresh_database_even_when_migrations_replay(self) -> None:
        connection, result = self.initialize(migrations=[self.UPGRADE])
        self.assertTrue(result.created)
        self.assertEqual(result.applied_migrations, (2,))
        self.assertIsNone(result.backup_path)
        self.assertEqual(get_user_version(connection), 2)
        self.assertEqual(self.backups(), [])

    def test_in_memory_database_upgrades_without_a_backup(self) -> None:
        connection = self.track(sqlite3.connect(":memory:"))
        initialize_schema(connection, baseline_path=DEFAULT_SCHEMA_PATH, migrations=V1_ONLY)
        result = run_migrations(connection, [self.UPGRADE])
        self.assertEqual(result.applied_versions, (2,))
        self.assertIsNone(result.backup_path)
        self.assertEqual(self.backups(), [])

    def test_backup_can_be_disabled_explicitly(self) -> None:
        self.make_v1_with_data()
        result = run_migrations(self.raw(), [self.UPGRADE], backup=False)
        self.assertEqual(result.applied_versions, (2,))
        self.assertEqual(self.backups(), [])


if __name__ == "__main__":
    unittest.main()
