from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from tsn_dss.engine.sqlite.db import (
    DEFAULT_SCHEMA_PATH,
    EXPECTED_USER_VERSION,
    SchemaVersionError,
    connect_database,
    foreign_key_violations,
    get_user_version,
    initialize_database,
    integrity_check,
    transaction,
)
from tsn_dss.engine.sqlite.migrations import CURRENT_SCHEMA_VERSION


class DatabaseFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_initialize_fresh_database_creates_schema(self) -> None:
        connection = initialize_database(self.db_path)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table'
                      AND name NOT LIKE 'sqlite_%';
                    """
                )
            }
        finally:
            connection.close()

        self.assertIn("targets", tables)
        self.assertIn("dataset_frames", tables)

    def test_reopen_existing_database_keeps_expected_version(self) -> None:
        first = initialize_database(self.db_path)
        first.close()

        reopened = initialize_database(self.db_path)
        try:
            self.assertEqual(get_user_version(reopened), EXPECTED_USER_VERSION)
        finally:
            reopened.close()

    def test_reopen_existing_database_adds_light_pollution_site_columns(self) -> None:
        legacy = connect_database(self.db_path)
        try:
            legacy.executescript(
                """
                PRAGMA user_version = 1;
                CREATE TABLE sites (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    latitude_deg REAL,
                    longitude_deg REAL,
                    elevation_m REAL,
                    sqm_mag_arcsec2 REAL,
                    bortle_class INTEGER,
                    south_horizon_open INTEGER NOT NULL DEFAULT 0,
                    notes TEXT
                );
                """
            )
        finally:
            legacy.close()

        reopened = initialize_database(self.db_path)
        try:
            columns = {
                row["name"]
                for row in reopened.execute("PRAGMA table_info(sites);").fetchall()
            }
        finally:
            reopened.close()

        self.assertIn("lp_artificial_brightness_mcd_m2", columns)
        self.assertIn("lp_estimated_sqm_mag_arcsec2", columns)
        self.assertIn("lp_data_kind", columns)
        self.assertIn("lp_updated_at", columns)

    def test_connection_enforces_foreign_keys(self) -> None:
        connection = initialize_database(self.db_path)
        try:
            self.assertEqual(connection.execute("PRAGMA foreign_keys;").fetchone()[0], 1)
        finally:
            connection.close()

    def test_schema_user_version_is_current(self) -> None:
        connection = initialize_database(self.db_path)
        try:
            self.assertEqual(get_user_version(connection), CURRENT_SCHEMA_VERSION)
        finally:
            connection.close()

    def test_integrity_check_returns_ok(self) -> None:
        connection = initialize_database(self.db_path)
        try:
            self.assertEqual(integrity_check(connection), "ok")
        finally:
            connection.close()

    def test_foreign_key_check_is_empty_for_fresh_database(self) -> None:
        connection = initialize_database(self.db_path)
        try:
            self.assertEqual(foreign_key_violations(connection), [])
        finally:
            connection.close()

    def test_invalid_foreign_key_is_rejected(self) -> None:
        connection = initialize_database(self.db_path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                with transaction(connection):
                    connection.execute(
                        """
                        INSERT INTO acquisition_plans (id, target_id, name, status)
                        VALUES (?, ?, ?, ?);
                        """,
                        ("plan:bad", "target:missing", "Broken Plan", "draft"),
                    )
        finally:
            connection.close()

    def test_accepted_null_is_not_counted_as_integration(self) -> None:
        connection = initialize_database(self.db_path)
        try:
            self._insert_minimal_observation_graph(connection)

            with transaction(connection):
                connection.executemany(
                    """
                    INSERT INTO frames (
                        observation_id,
                        sequence_id,
                        frame_type,
                        file_path,
                        exposure_s,
                        accepted
                    ) VALUES (?, ?, ?, ?, ?, ?);
                    """,
                    [
                        ("obs:test", 1, "light", "frames/accepted.fit", 30.0, 1),
                        ("obs:test", 1, "light", "frames/unreviewed.fit", 15.0, None),
                        ("obs:test", 1, "light", "frames/rejected.fit", 10.0, 0),
                    ],
                )

            result = connection.execute(
                """
                SELECT accepted_light_integration_s
                FROM v_observation_summary
                WHERE observation_id = ?;
                """,
                ("obs:test",),
            ).fetchone()
        finally:
            connection.close()

        self.assertEqual(result[0], 30.0)

    def test_dataset_frames_links_dataset_to_exact_frames(self) -> None:
        connection = initialize_database(self.db_path)
        try:
            self._insert_minimal_observation_graph(connection)

            with transaction(connection):
                connection.executemany(
                    """
                    INSERT INTO frames (
                        observation_id,
                        sequence_id,
                        frame_type,
                        file_path,
                        exposure_s,
                        accepted
                    ) VALUES (?, ?, ?, ?, ?, ?);
                    """,
                    [
                        ("obs:test", 1, "light", "frames/frame_001.fit", 30.0, 1),
                        ("obs:test", 1, "light", "frames/frame_002.fit", 30.0, 1),
                    ],
                )
                frame_ids = [
                    row[0]
                    for row in connection.execute(
                        "SELECT id FROM frames WHERE observation_id = ? ORDER BY id;",
                        ("obs:test",),
                    )
                ]
                connection.execute(
                    """
                    INSERT INTO datasets (
                        id,
                        target_id,
                        name,
                        status,
                        total_light_integration_s
                    ) VALUES (?, ?, ?, ?, ?);
                    """,
                    ("dataset:test", "target:test", "Test Dataset", "open", 60.0),
                )
                connection.execute(
                    """
                    INSERT INTO dataset_observations (dataset_id, observation_id)
                    VALUES (?, ?);
                    """,
                    ("dataset:test", "obs:test"),
                )
                connection.executemany(
                    """
                    INSERT INTO dataset_frames (dataset_id, frame_id)
                    VALUES (?, ?);
                    """,
                    [("dataset:test", frame_id) for frame_id in frame_ids],
                )

            rows = connection.execute(
                """
                SELECT df.dataset_id, f.file_path
                FROM dataset_frames df
                JOIN frames f ON f.id = df.frame_id
                WHERE df.dataset_id = ?
                ORDER BY f.id;
                """,
                ("dataset:test",),
            ).fetchall()
        finally:
            connection.close()

        self.assertEqual(
            [tuple(row) for row in rows],
            [
                ("dataset:test", "frames/frame_001.fit"),
                ("dataset:test", "frames/frame_002.fit"),
            ],
        )

    def test_transaction_rolls_back_on_error(self) -> None:
        connection = initialize_database(self.db_path)
        try:
            self._insert_minimal_observation_graph(connection)

            with self.assertRaises(sqlite3.IntegrityError):
                with transaction(connection):
                    connection.execute(
                        """
                        INSERT INTO datasets (
                            id,
                            target_id,
                            name,
                            status,
                            total_light_integration_s
                        ) VALUES (?, ?, ?, ?, ?);
                        """,
                        ("dataset:rollback", "target:test", "Rollback Dataset", "open", 0.0),
                    )
                    connection.execute(
                        """
                        INSERT INTO dataset_observations (dataset_id, observation_id)
                        VALUES (?, ?);
                        """,
                        ("dataset:rollback", "obs:missing"),
                    )

            count = connection.execute(
                "SELECT COUNT(*) FROM datasets WHERE id = ?;",
                ("dataset:rollback",),
            ).fetchone()[0]
        finally:
            connection.close()

        self.assertEqual(count, 0)

    def test_existing_schema_version_newer_than_supported_is_rejected(self) -> None:
        connection = connect_database(self.db_path)
        try:
            connection.executescript(
                Path(DEFAULT_SCHEMA_PATH).read_text(encoding="utf-8").replace(
                    "PRAGMA user_version = 1;",
                    f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 1};",
                )
            )
            connection.close()

            with self.assertRaises(SchemaVersionError):
                initialize_database(self.db_path)
        finally:
            if connection:
                try:
                    connection.close()
                except sqlite3.ProgrammingError:
                    pass

    def _insert_minimal_observation_graph(self, connection: sqlite3.Connection) -> None:
        with transaction(connection):
            connection.execute(
                """
                INSERT INTO targets (
                    id,
                    catalog,
                    catalog_id,
                    name,
                    ra_deg,
                    dec_deg
                ) VALUES (?, ?, ?, ?, ?, ?);
                """,
                ("target:test", "TEST", "T-1", "Test Target", 10.0, 20.0),
            )
            connection.execute(
                """
                INSERT INTO acquisition_plans (id, target_id, name, status)
                VALUES (?, ?, ?, ?);
                """,
                ("plan:test", "target:test", "Test Plan", "ready"),
            )
            connection.execute(
                """
                INSERT INTO acquisition_sequences (
                    id,
                    plan_id,
                    sequence_order,
                    frame_type,
                    exposure_s,
                    frame_count
                ) VALUES (?, ?, ?, ?, ?, ?);
                """,
                (1, "plan:test", 10, "light", 30.0, 3),
            )
            connection.execute(
                """
                INSERT INTO observations (
                    id,
                    observation_number,
                    target_id,
                    acquisition_plan_id,
                    status
                ) VALUES (?, ?, ?, ?, ?);
                """,
                ("obs:test", 1, "target:test", "plan:test", "planned"),
            )


if __name__ == "__main__":
    unittest.main()
