from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from tsn_dss.domain.models import CatalogObject, CatalogObjectAlias, Target
from tsn_dss.engine.catalog import CatalogConflictError, CatalogRegistrationItem, normalize_catalog_alias
from tsn_dss.engine.sqlite.catalog import CatalogRepository
from tsn_dss.engine.sqlite.db import initialize_database
from tsn_dss.engine.sqlite.planning import PlanningRepository
from tsn_dss.engine.sqlite.project_repository import ProjectRepository


class CatalogRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "tsn.db"
        self.connection = initialize_database(self.db_path)
        self.addCleanup(self.connection.close)
        self.repository = CatalogRepository(self.connection)

    def m42_item(self) -> CatalogRegistrationItem:
        catalog_object = CatalogObject(
            id="catalog-object:messier:42",
            canonical_designation="M42",
            display_name="Orion Nebula",
            ra_deg=83.82208,
            dec_deg=-5.39111,
            object_type="emission_nebula",
            coordinate_frame="ICRS",
            coordinate_epoch="J2000",
            source_provider="test-fixture",
            source_version="2026-09-26",
            source_external_id="messier-42",
            imported_at="2026-09-26T00:00:00Z",
        )
        aliases = [
            CatalogObjectAlias(catalog_object.id, "M42", normalize_catalog_alias("M42"), "designation"),
            CatalogObjectAlias(catalog_object.id, "NGC 1976", normalize_catalog_alias("NGC 1976"), "designation"),
            CatalogObjectAlias(catalog_object.id, "Orion Nebula", normalize_catalog_alias("Orion Nebula"), "name"),
        ]
        return CatalogRegistrationItem(catalog_object, aliases)

    def test_alias_normalization_is_exact_and_deterministic(self) -> None:
        self.assertEqual(normalize_catalog_alias("M42"), "messier:42")
        self.assertEqual(normalize_catalog_alias("M 42"), "messier:42")
        self.assertEqual(normalize_catalog_alias("Messier 042"), "messier:42")
        self.assertEqual(normalize_catalog_alias("NGC 1976"), "ngc:1976")
        self.assertEqual(normalize_catalog_alias("  Orion   Nebula "), "orion nebula")

    def test_register_and_resolve_m42_aliases_to_same_catalog_object(self) -> None:
        self.repository.register_catalog_objects([self.m42_item()])

        object_ids = set()
        for query in ("M42", "M 42", "Messier 42", "NGC 1976", "Orion Nebula", "orion nebula"):
            with self.subTest(query=query):
                result = self.repository.resolve_alias(query)
                self.assertEqual(result.status, "resolved")
                assert result.catalog_object is not None
                object_ids.add(result.catalog_object.id)
                self.assertEqual(result.catalog_object.canonical_designation, "M42")
                self.assertEqual(result.catalog_object.display_name, "Orion Nebula")
                self.assertEqual(result.catalog_object.source_provider, "test-fixture")

        self.assertEqual(object_ids, {"catalog-object:messier:42"})

    def test_registration_is_idempotent(self) -> None:
        first = self.repository.register_catalog_objects([self.m42_item()])
        second = self.repository.register_catalog_objects([self.m42_item()])

        self.assertEqual(first.objects_created, ["catalog-object:messier:42"])
        self.assertEqual(second.objects_created, [])
        self.assertEqual(second.objects_unchanged, ["catalog-object:messier:42"])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM catalog_objects").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM catalog_object_aliases").fetchone()[0], 3)

    def test_alias_conflict_is_reported_and_rolled_back(self) -> None:
        self.repository.register_catalog_objects([self.m42_item()])
        conflicting = CatalogObject(
            id="catalog-object:test:other",
            canonical_designation="TEST 1",
            display_name="Other",
            ra_deg=1.0,
            dec_deg=2.0,
            object_type="nebula",
            coordinate_frame="ICRS",
            source_provider="test-fixture",
            source_version="2026-09-26",
            source_external_id="test-1",
        )
        item = CatalogRegistrationItem(
            conflicting,
            [CatalogObjectAlias(conflicting.id, "M42", normalize_catalog_alias("M42"), "designation")],
        )

        with self.assertRaises(CatalogConflictError) as caught:
            self.repository.register_catalog_objects([item])

        self.assertIn("already belongs", str(caught.exception))
        self.assertIsNone(self.repository.get_catalog_object("catalog-object:test:other"))

    def test_read_paths_do_not_mutate_catalog_state(self) -> None:
        self.repository.register_catalog_objects([self.m42_item()])
        before = (
            self.connection.execute("SELECT COUNT(*) FROM catalog_objects").fetchone()[0],
            self.connection.execute("SELECT COUNT(*) FROM catalog_object_aliases").fetchone()[0],
        )

        self.repository.get_catalog_object("catalog-object:messier:42")
        self.repository.list_catalog_objects(query="M42")
        self.repository.resolve_alias("M42")

        after = (
            self.connection.execute("SELECT COUNT(*) FROM catalog_objects").fetchone()[0],
            self.connection.execute("SELECT COUNT(*) FROM catalog_object_aliases").fetchone()[0],
        )
        self.assertEqual(after, before)

    def test_catalog_registration_does_not_populate_targets_or_change_projects(self) -> None:
        planning = PlanningRepository(self.connection)
        projects = ProjectRepository(self.connection)
        projects.register_project(dir_key="M42", target_label="M42")
        planning.create_target(
            Target(
                id="target:m31",
                catalog="MESSIER",
                catalog_id="M31",
                name="Andromeda Galaxy",
                ra_deg=10.6847,
                dec_deg=41.2692,
            )
        )
        before_targets = [target.id for target in planning.list_targets()]
        before_project = projects.get_project_by_dir_key("M42")

        self.repository.register_catalog_objects([self.m42_item()])

        self.assertEqual([target.id for target in planning.list_targets()], before_targets)
        after_project = projects.get_project_by_dir_key("M42")
        assert before_project is not None and after_project is not None
        self.assertEqual((before_project.target_id, before_project.target_label), (None, "M42"))
        self.assertEqual((after_project.target_id, after_project.target_label), (None, "M42"))

    def test_database_uniqueness_prevents_alias_ambiguity(self) -> None:
        self.repository.register_catalog_objects([self.m42_item()])
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO catalog_object_aliases (catalog_object_id, alias, normalized_alias, alias_kind)
                VALUES (?, ?, ?, ?);
                """,
                ("catalog-object:messier:42", "Messier 42", "messier:42", "designation"),
            )


if __name__ == "__main__":
    unittest.main()
