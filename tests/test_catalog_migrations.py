from __future__ import annotations

import unittest

from tsn_dss.engine.sqlite.db import DEFAULT_SCHEMA_PATH, foreign_key_violations, integrity_check
from tsn_dss.engine.sqlite.migrations import CURRENT_SCHEMA_VERSION, MIGRATIONS, Migration, get_user_version, initialize_schema

try:
    from test_schema_migrations import TempDirTestCase, describe_schema, schema_diff
except ImportError:  # pragma: no cover
    from tests.test_schema_migrations import TempDirTestCase, describe_schema, schema_diff


V3_ONLY = MIGRATIONS[:2]


class CatalogMigrationTests(TempDirTestCase):
    def test_fresh_database_reaches_current_schema_with_catalog_tables(self) -> None:
        connection, result = self.initialize()

        self.assertEqual(result.final_version, CURRENT_SCHEMA_VERSION)
        self.assertEqual(result.applied_migrations, (2, 3, 4, 5, 6, 7, 8, 9, 10))
        self.assertEqual(get_user_version(connection), CURRENT_SCHEMA_VERSION)
        tables = describe_schema(connection)["tables"]
        self.assertIn("catalog_objects", tables)
        self.assertIn("catalog_object_aliases", tables)

    def test_v3_to_current_schema_preserves_existing_data(self) -> None:
        before, _ = self.initialize(migrations=V3_ONLY)
        before.execute(
            """
            INSERT INTO targets (id, catalog, catalog_id, name, ra_deg, dec_deg)
            VALUES ('target:m42', 'MESSIER', 'M42', 'Orion Nebula', 83.8, -5.4);
            """
        )
        before.commit()
        before.close()

        connection, result = self.initialize()

        self.assertEqual(result.applied_migrations, (4, 5, 6, 7, 8, 9, 10))
        self.assertEqual(get_user_version(connection), CURRENT_SCHEMA_VERSION)
        self.assertEqual(
            tuple(connection.execute("SELECT id, catalog_id, name FROM targets").fetchone()),
            ("target:m42", "M42", "Orion Nebula"),
        )
        self.assertEqual(integrity_check(connection), "ok")
        self.assertEqual(foreign_key_violations(connection), [])

    def test_catalog_aliases_cascade_when_catalog_object_is_deleted(self) -> None:
        connection, _ = self.initialize()
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO catalog_objects (
                id, canonical_designation, display_name, ra_deg, dec_deg, object_type,
                coordinate_frame, source_provider, source_version
            ) VALUES ('catalog-object:test:1', 'TEST 1', 'Test Object', 1.0, 2.0, 'nebula',
                      'ICRS', 'test', '1');
            """
        )
        connection.execute(
            """
            INSERT INTO catalog_object_aliases (catalog_object_id, alias, normalized_alias, alias_kind)
            VALUES ('catalog-object:test:1', 'Test Object', 'test object', 'name');
            """
        )
        connection.commit()

        connection.execute("DELETE FROM catalog_objects WHERE id = 'catalog-object:test:1';")
        connection.commit()

        self.assertEqual(connection.execute("SELECT COUNT(*) FROM catalog_object_aliases").fetchone()[0], 0)

    def test_migrated_database_matches_fresh_schema_semantically(self) -> None:
        migrated, _ = self.initialize(migrations=V3_ONLY)
        migrated.close()
        migrated, _ = self.initialize()
        migrated_schema = describe_schema(migrated)

        fresh = self.raw(self.dir / "fresh.db")
        initialize_schema(fresh, baseline_path=DEFAULT_SCHEMA_PATH)

        self.assertEqual(schema_diff(migrated_schema, describe_schema(fresh)), [])

    def test_failed_v4_migration_leaves_v3_untouched(self) -> None:
        before, _ = self.initialize(migrations=V3_ONLY)
        before.close()

        failing = list(MIGRATIONS[:2]) + [
            Migration(
                4,
                "failing catalog",
                sql="""
                CREATE TABLE catalog_objects_probe (id TEXT);
                INSERT INTO missing_table VALUES (1);
                """,
            )
        ]

        with self.assertRaises(Exception):
            self.initialize(migrations=failing)

        view = self.raw()
        self.assertEqual(get_user_version(view), 3)
        self.assertNotIn("catalog_objects_probe", describe_schema(view)["tables"])


if __name__ == "__main__":
    unittest.main()
