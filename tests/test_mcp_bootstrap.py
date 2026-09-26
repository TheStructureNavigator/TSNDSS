from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from tsn_dss.engine.sqlite.db import DEFAULT_SCHEMA_PATH, connect_database, initialize_database
from tsn_dss.engine.sqlite.migrations import CURRENT_SCHEMA_VERSION, MIGRATIONS, get_user_version, initialize_schema
from tsn_dss.mcp.bootstrap import McpBootstrapError, open_readonly_database, resolve_database_path
from tsn_dss.mcp.tools.sites import get_sites


class McpBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.projects_root = self.root / "projects"
        self.projects_root.mkdir()
        self.db_path = self.projects_root / "tsn_dss.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_resolves_default_database_under_projects_root(self) -> None:
        self.assertEqual(
            resolve_database_path(projects_root=self.projects_root),
            self.projects_root / "tsn_dss.db",
        )

    def test_opens_current_schema_database_read_only(self) -> None:
        created = initialize_database(self.db_path)
        created.close()

        connection = open_readonly_database(projects_root=self.projects_root)
        try:
            self.assertEqual(get_user_version(connection), CURRENT_SCHEMA_VERSION)
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("CREATE TABLE should_not_write (id INTEGER);")
        finally:
            connection.close()

    def test_missing_database_is_refused_and_not_created(self) -> None:
        with self.assertRaisesRegex(McpBootstrapError, "does not exist"):
            open_readonly_database(projects_root=self.projects_root)

        self.assertFalse(self.db_path.exists())

    def test_old_schema_is_refused_without_migration_or_backup(self) -> None:
        connection = connect_database(self.db_path)
        try:
            initialize_schema(
                connection,
                baseline_path=DEFAULT_SCHEMA_PATH,
                migrations=MIGRATIONS[:2],
            )
            self.assertEqual(get_user_version(connection), CURRENT_SCHEMA_VERSION - 1)
        finally:
            connection.close()

        before_files = sorted(path.name for path in self.projects_root.iterdir())
        with self.assertRaisesRegex(McpBootstrapError, "older than this build"):
            open_readonly_database(projects_root=self.projects_root)
        after_files = sorted(path.name for path in self.projects_root.iterdir())

        self.assertEqual(before_files, ["tsn_dss.db"])
        self.assertEqual(after_files, before_files)
        verifier = connect_database(self.db_path)
        try:
            self.assertEqual(get_user_version(verifier), CURRENT_SCHEMA_VERSION - 1)
        finally:
            verifier.close()

    def test_newer_schema_is_refused(self) -> None:
        connection = initialize_database(self.db_path)
        try:
            connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 1};")
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(McpBootstrapError, "newer than this build"):
            open_readonly_database(projects_root=self.projects_root)

    def test_opening_does_not_register_filesystem_projects(self) -> None:
        (self.projects_root / "M31").mkdir()
        (self.projects_root / "M31" / "captures").mkdir()
        created = initialize_database(self.db_path)
        created.close()
        before_tree = sorted(path.relative_to(self.projects_root).as_posix() for path in self.projects_root.rglob("*"))

        connection = open_readonly_database(projects_root=self.projects_root)
        try:
            self.assertEqual(get_sites(connection), {"sites": []})
            project_count = connection.execute("SELECT COUNT(*) FROM projects;").fetchone()[0]
        finally:
            connection.close()
        after_tree = sorted(path.relative_to(self.projects_root).as_posix() for path in self.projects_root.rglob("*"))

        self.assertEqual(project_count, 0)
        self.assertEqual(after_tree, before_tree)


if __name__ == "__main__":
    unittest.main()
