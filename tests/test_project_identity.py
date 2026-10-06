from __future__ import annotations

"""Stage S2: canonical Project identity, the v1 -> v2 migration, dual-write and mosaic linkage."""

import json
import re
import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

try:
    from test_schema_migrations import (
        LEGACY_VARIANTS,
        V1_ONLY,
        TempDirTestCase,
        build_legacy_v1_database,
        describe_schema,
        populate_legacy_data,
        rows_for_columns,
        schema_diff,
        snapshot_rows,
    )
except ModuleNotFoundError:
    from tests.test_schema_migrations import (
        LEGACY_VARIANTS,
        V1_ONLY,
        TempDirTestCase,
        build_legacy_v1_database,
        describe_schema,
        populate_legacy_data,
        rows_for_columns,
        schema_diff,
        snapshot_rows,
    )
from tsn_dss.domain.models import LocalHorizonPoint, MosaicPlan, Project, Target
from tsn_dss.engine.project_registry import (
    MetadataConflict,
    ProjectInUseError,
    ProjectRegistry,
    scan_filesystem_projects,
    validate_dir_key,
)
from tsn_dss.engine.projects import ProjectStorage
from tsn_dss.engine.sqlite.db import DEFAULT_SCHEMA_PATH, connect_database, initialize_database
from tsn_dss.engine.sqlite.migrations import (
    _MIGRATION_2_PROJECTS_SQL,
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    Migration,
    MigrationError,
    get_user_version,
    initialize_schema,
)
from tsn_dss.engine.sqlite.mosaics import MosaicRepository, ValidationError  # mosaics has its own class
from tsn_dss.engine.sqlite.planning import PlanningRepository
from tsn_dss.engine.sqlite.planning import ValidationError as RepositoryValidationError
from tsn_dss.engine.sqlite.project_repository import ProjectRepository, new_project_id
from tsn_dss.gui.http_api import create_http_server

# The v2 world: production registry restricted to the v1 -> v2 migration (v3 exists now).
V2_ONLY = MIGRATIONS[:1]


# ---------------------------------------------------------------------------
# Migration v1 -> v2
# ---------------------------------------------------------------------------


class ProjectSchemaMigrationTests(TempDirTestCase):
    def build_v1_with_real_shaped_data(self) -> None:
        self.open_v1().close()
        populate_legacy_data(self.db_path, "2026-09-01")  # site + LP values, mosaic plan + panels + intent
        raw = sqlite3.connect(self.db_path)
        raw.executemany(
            "INSERT INTO site_horizon_profile_points (site_id, azimuth_deg, min_altitude_deg) VALUES (?, ?, ?)",
            [("site:legacy", 0.0, 5.0), ("site:legacy", 90.0, 22.5), ("site:legacy", 200.0, 12.0)],
        )
        raw.commit()
        raw.close()

    def test_upgrade_sets_version_two_and_preserves_sites_horizon_and_mosaics(self) -> None:
        self.build_v1_with_real_shaped_data()
        before_connection = self.raw()
        self.assertEqual(get_user_version(before_connection), 1)
        before = snapshot_rows(before_connection)
        before_connection.close()

        connection, result = self.initialize(migrations=V2_ONLY)  # the v2 world: only the v1 -> v2 migration
        self.assertEqual((result.initial_version, result.final_version), (1, 2))
        self.assertEqual(result.applied_migrations, (2,))
        self.assertIsNotNone(result.backup_path)
        self.assertEqual(get_user_version(connection), 2)

        for table, (columns, rows) in before.items():
            self.assertEqual(rows_for_columns(connection, table, columns), rows, f"{table} changed")
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0)
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM mosaic_plans WHERE project_id IS NOT NULL").fetchone()[0], 0
        )
        connection.close()

        upgraded = self.open_initialized()
        site = PlanningRepository(upgraded).get_site("site:legacy")
        self.assertEqual([(p.azimuth_deg, p.min_altitude_deg) for p in site.horizon_profile],
                         [(0.0, 5.0), (90.0, 22.5), (200.0, 12.0)])
        self.assertEqual(site.lp_estimated_bortle_class, 4)
        plan = MosaicRepository(upgraded).get_mosaic_plan("mosaic:legacy")
        self.assertEqual((plan.observation_type, plan.filter), ("dual-band imaging", "L-eXtreme"))
        self.assertEqual(len(plan.panels), 2)
        self.assertIsNone(plan.project_id)
        self.assertEqual(plan.project_slug, "Cygnus Loop")

    def test_upgrade_backs_up_the_v1_database_once(self) -> None:
        self.build_v1_with_real_shaped_data()
        _, result = self.initialize()
        self.assertEqual(len(self.backups()), 1)
        backup = self.raw(result.backup_path)
        self.assertEqual(get_user_version(backup), 1)
        self.assertNotIn("projects", describe_schema(backup)["tables"])

    def test_failing_v2_migration_rolls_back_completely(self) -> None:
        self.build_v1_with_real_shaped_data()
        before_connection = self.raw()
        before_rows = snapshot_rows(before_connection)
        before_schema = describe_schema(before_connection)
        before_connection.close()

        failing = Migration(2, "v2 then fail", sql=_MIGRATION_2_PROJECTS_SQL + "INSERT INTO no_such_table VALUES (1);")
        with self.assertRaises(MigrationError):
            self.initialize(migrations=[failing])

        after = self.raw()
        self.assertEqual(get_user_version(after), 1)
        self.assertEqual(describe_schema(after), before_schema)
        self.assertNotIn("projects", describe_schema(after)["tables"])
        self.assertEqual(snapshot_rows(after), before_rows)

    def test_migrated_database_equals_a_fresh_database_semantically(self) -> None:
        self.build_v1_with_real_shaped_data()
        migrated, _ = self.initialize()
        migrated_schema = describe_schema(migrated)
        migrated.close()

        fresh = self.track(sqlite3.connect(self.dir / "fresh.db"))
        initialize_schema(fresh, baseline_path=DEFAULT_SCHEMA_PATH)
        self.assertEqual(schema_diff(migrated_schema, describe_schema(fresh)), [])
        self.assertEqual(migrated_schema, describe_schema(fresh))

    def test_every_legacy_v1_shape_reaches_the_same_v2_schema_as_a_fresh_database(self) -> None:
        fresh = self.track(sqlite3.connect(self.dir / "fresh.db"))
        initialize_schema(fresh, baseline_path=DEFAULT_SCHEMA_PATH)
        expected = describe_schema(fresh)

        for variant in LEGACY_VARIANTS:
            with self.subTest(variant):
                path = self.dir / f"legacy-{variant}.db"
                build_legacy_v1_database(path, variant)
                populate_legacy_data(path, variant)
                connection, result = self.initialize(path=path)
                self.assertTrue(result.normalized_legacy)
                self.assertEqual(result.applied_migrations, (2, 3, 4, 5, 6, 7, 8))
                self.assertEqual(describe_schema(connection), expected)
                connection.close()

    def test_migration_does_not_link_or_create_anything(self) -> None:
        # The migration is schema only. Linking existing plans is the registrar's job.
        self.build_v1_with_real_shaped_data()
        connection, _ = self.initialize()
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 0)
        self.assertEqual(
            connection.execute("SELECT project_slug, project_id FROM mosaic_plans").fetchall()[0][1], None
        )

    def test_delete_actions_and_uniqueness_are_conservative(self) -> None:
        schema = describe_schema(self.open_initialized())
        plan_keys = schema["tables"]["mosaic_plans"]["foreign_keys"]
        self.assertIn(("project_id", "projects", "id", "CASCADE", "RESTRICT"), plan_keys)
        self.assertIn(("target_id", "targets", "id", "CASCADE", "RESTRICT"), schema["tables"]["projects"]["foreign_keys"])
        self.assertIn(("u", True, ("dir_key",)), schema["tables"]["projects"]["indexes"])
        self.assertIn("project_id", schema["tables"]["mosaic_plans"]["columns"])
        self.assertTrue(schema["tables"]["mosaic_plans"]["columns"]["project_slug"]["notnull"])  # legacy column kept

    def test_reopening_a_current_database_is_a_no_op(self) -> None:
        self.open_initialized().close()
        first = self.raw()
        cookie = first.execute("PRAGMA schema_version").fetchone()[0]
        first.close()
        connection, result = self.initialize()
        self.assertEqual(result.applied_migrations, ())
        self.assertFalse(result.normalized_legacy)
        self.assertIsNone(result.backup_path)
        self.assertEqual(connection.execute("PRAGMA schema_version").fetchone()[0], cookie)


# ---------------------------------------------------------------------------
# Project repository
# ---------------------------------------------------------------------------


class ProjectRepositoryTests(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.connection = self.open_initialized()
        self.repo = ProjectRepository(self.connection)

    def test_id_is_opaque_prefixed_and_independent_of_dir_key(self) -> None:
        first = self.repo.register_project(dir_key="M31")
        second = self.repo.register_project(dir_key="M27 \u2014 Dumbbell Nebula")
        for project in (first, second):
            self.assertRegex(project.id, r"^project:[0-9a-f]{12}$")
            self.assertNotIn(project.dir_key.lower(), project.id.lower())
        self.assertNotEqual(first.id, second.id)
        self.assertRegex(new_project_id(), r"^project:[0-9a-f]{12}$")
        self.assertNotEqual(new_project_id(), new_project_id())

    def test_display_name_defaults_to_dir_key_and_status_is_active(self) -> None:
        project = self.repo.register_project(dir_key="Cygnus Loop", target_label="NGC6960")
        self.assertEqual(project.display_name, "Cygnus Loop")
        self.assertEqual(project.dir_key, "Cygnus Loop")
        self.assertEqual(project.target_label, "NGC6960")
        self.assertIsNone(project.target_id)
        self.assertEqual(project.status, "active")
        self.assertIsNone(project.notes)

    def test_dir_key_is_unique(self) -> None:
        self.repo.register_project(dir_key="M31")
        with self.assertRaises(RepositoryValidationError):
            self.repo.register_project(dir_key="M31")
        self.assertEqual(len(self.repo.list_projects()), 1)

    def test_lookup_and_listing(self) -> None:
        a = self.repo.register_project(dir_key="b-project")
        b = self.repo.register_project(dir_key="a-project")
        self.assertEqual(self.repo.get_project(a.id), a)
        self.assertEqual(self.repo.get_project_by_dir_key("a-project"), b)
        self.assertIsNone(self.repo.get_project("project:missing"))
        self.assertIsNone(self.repo.get_project_by_dir_key("nope"))
        self.assertEqual([p.dir_key for p in self.repo.list_projects()], ["a-project", "b-project"])
        self.assertEqual(self.repo.dir_key_to_id(), {"b-project": a.id, "a-project": b.id})

    def test_validation(self) -> None:
        for project in (
            Project(id="", display_name="x", dir_key="x"),
            Project(id="project:1", display_name="", dir_key="x"),
            Project(id="project:1", display_name="x", dir_key=""),
            Project(id="project:1", display_name="x", dir_key="x", status="deleted"),
        ):
            with self.subTest(project), self.assertRaises(RepositoryValidationError):
                self.repo.create_project(project)

    def test_set_target_label_touches_only_the_label(self) -> None:
        project = self.repo.register_project(dir_key="M31", target_label="M31")
        updated = self.repo.set_target_label(project.id, "Andromeda")
        self.assertEqual(updated.target_label, "Andromeda")
        self.assertEqual((updated.id, updated.dir_key, updated.display_name), (project.id, "M31", "M31"))
        self.assertIsNone(self.repo.set_target_label(project.id, None).target_label)
        with self.assertRaises(KeyError):
            self.repo.set_target_label("project:missing", "x")

    def test_unknown_target_stays_null_even_when_a_matching_target_exists(self) -> None:
        PlanningRepository(self.connection).create_target(
            Target(id="target:m31", catalog="M", catalog_id="31", name="M31", ra_deg=10.68, dec_deg=41.27)
        )
        project = self.repo.register_project(dir_key="M31", target_label="M31")
        self.assertIsNone(project.target_id)

    def test_a_linked_target_cannot_be_deleted(self) -> None:
        PlanningRepository(self.connection).create_target(
            Target(id="target:m31", catalog="M", catalog_id="31", name="M31", ra_deg=10.68, dec_deg=41.27)
        )
        self.connection.execute("INSERT INTO projects (id, display_name, dir_key, target_id) "
                                "VALUES ('project:x', 'M31', 'M31', 'target:m31')")
        self.connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM targets WHERE id = 'target:m31'")
        self.connection.rollback()

    def test_constructing_the_repository_never_changes_the_schema(self) -> None:
        cookie = self.connection.execute("PRAGMA schema_version").fetchone()[0]
        ProjectRepository(self.connection)
        ProjectRegistry(ProjectStorage(self.dir / "projects"), self.connection)
        self.assertEqual(self.connection.execute("PRAGMA schema_version").fetchone()[0], cookie)


# ---------------------------------------------------------------------------
# Registrar and dual-write
# ---------------------------------------------------------------------------


def tree_snapshot(root: Path) -> dict[str, tuple]:
    snapshot: dict[str, tuple] = {}
    for path in [root, *sorted(root.rglob("*"))]:
        stat = path.stat()
        snapshot[str(path.relative_to(root))] = (
            path.is_dir(),
            0 if path.is_dir() else stat.st_size,
            stat.st_mtime_ns,
        )
    return snapshot


class RegistryTestCase(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.dir / "projects"
        self.root.mkdir()
        self.storage = ProjectStorage(self.root)
        self.connection = self.open_initialized(self.dir / "registry.db")
        self.registry = ProjectRegistry(self.storage, self.connection)
        self.repo = ProjectRepository(self.connection)

    def make_project(self, name: str, project_json: str | None = None, *, layout: bool = False) -> Path:
        path = self.root / name
        path.mkdir(parents=True)
        if layout:
            (path / "captures").mkdir()
            (path / "runs").mkdir()
        if project_json is not None:
            (path / "project.json").write_text(project_json, encoding="utf-8")
        return path

    def plan(self, plan_id: str, slug: str, **extra) -> MosaicPlan:
        return MosaicPlan(
            id=plan_id,
            project_slug=slug,
            name=f"plan {plan_id}",
            imaging_profile_id="simulator-default",
            imaging_profile_label="Seestar",
            fov_width_deg=2.59,
            fov_height_deg=1.47,
            center_ra_deg=10.0,
            center_dec_deg=41.0,
            region_width_deg=5.0,
            region_height_deg=3.0,
            **extra,
        )


class RegistrarTests(RegistryTestCase):
    def test_registers_each_directory_with_exact_dir_key_and_verbatim_label(self) -> None:
        self.make_project("M31", '{"sky_target": "M31"}')
        self.make_project("Cygnus Loop", '{"sky_target": "NGC6960"}')
        self.make_project("M27 \u2014 Dumbbell Nebula", '{"sky_target": " M27 "}')
        self.make_project("Algol")
        (self.root / "tsn_dss.db").write_text("not a project")
        (self.root / "notes.txt").write_text("not a project either")

        report = self.registry.register_existing()

        self.assertEqual(sorted(report.created), sorted(["M31", "Cygnus Loop", "M27 \u2014 Dumbbell Nebula", "Algol"]))
        self.assertEqual(report.unchanged, [])
        self.assertEqual(report.conflicts, [])
        self.assertEqual(sorted(report.filesystem_only), sorted(report.created))
        by_key = {p.dir_key: p for p in self.repo.list_projects()}
        self.assertEqual(set(by_key), {"M31", "Cygnus Loop", "M27 \u2014 Dumbbell Nebula", "Algol"})
        self.assertEqual(by_key["Cygnus Loop"].target_label, "NGC6960")
        self.assertEqual(by_key["M27 \u2014 Dumbbell Nebula"].target_label, " M27 ")  # verbatim, not stripped
        self.assertIsNone(by_key["Algol"].target_label)
        for project in by_key.values():
            self.assertEqual(project.display_name, project.dir_key)
            self.assertIsNone(project.target_id)
            self.assertEqual(project.status, "active")
        self.assertEqual(len({p.id for p in by_key.values()}), 4)

    def test_registration_is_idempotent(self) -> None:
        self.make_project("M31", '{"sky_target": "M31"}')
        self.make_project("Algol")
        first = self.registry.register_existing()
        ids = self.repo.dir_key_to_id()

        second = self.registry.register_existing()
        third = self.registry.register_existing()
        for report in (second, third):
            self.assertEqual(report.created, [])
            self.assertEqual(sorted(report.unchanged), ["Algol", "M31"])
            self.assertEqual(report.conflicts, [])
            self.assertEqual(report.filesystem_only, [])
        self.assertEqual(self.repo.dir_key_to_id(), ids)
        self.assertEqual(len(first.created), 2)

    def test_missing_and_malformed_project_json_import_no_target(self) -> None:
        self.make_project("absent")
        self.make_project("malformed", "{not json")
        self.make_project("array", "[1, 2, 3]")
        self.make_project("blank", '{"sky_target": "   "}')
        self.make_project("number", '{"sky_target": 123}')
        self.make_project("null", '{"sky_target": null}')

        report = self.registry.register_existing()

        self.assertEqual(len(report.created), 6)
        for project in self.repo.list_projects():
            self.assertIsNone(project.target_label, project.dir_key)
        warned = " ".join(report.warnings)
        self.assertIn("malformed", warned)
        self.assertIn("array", warned)
        self.assertEqual(len(report.warnings), 2)  # only genuinely bad files are warnings

    def test_unreadable_project_json_is_reported_not_fatal(self) -> None:
        self.make_project("bad-bytes")
        (self.root / "bad-bytes" / "project.json").write_bytes(b"\xff\xfe\x00garbage\xff")
        report = self.registry.register_existing()
        self.assertEqual(report.created, ["bad-bytes"])
        self.assertTrue(any("bad-bytes" in warning for warning in report.warnings))

    def test_scanning_and_registering_write_nothing_to_the_filesystem(self) -> None:
        self.make_project("bare")  # no captures/runs: list_projects would create them
        self.make_project("with-json", '{"sky_target": "M31"}', layout=True)
        (self.root / "with-json" / "captures" / "OrionNebula").mkdir()
        (self.root / "with-json" / "captures" / "OrionNebula" / "frame.CR2").write_bytes(b"raw")
        before = tree_snapshot(self.root)

        scan_filesystem_projects(self.root)
        self.registry.inspect()
        self.registry.register_existing()
        self.registry.register_existing()

        self.assertEqual(tree_snapshot(self.root), before)
        self.assertFalse((self.root / "bare" / "captures").exists())
        self.assertFalse((self.root / "bare" / "runs").exists())

    def test_inspect_is_a_dry_run_that_writes_nothing_to_the_database(self) -> None:
        self.make_project("M31", '{"sky_target": "M31"}')
        self.connection.execute("INSERT INTO mosaic_plans (id, project_slug, name, imaging_profile_id, "
                                "imaging_profile_label, fov_width_deg, fov_height_deg, center_ra_deg, "
                                "center_dec_deg, region_width_deg, region_height_deg) "
                                "VALUES ('p1', 'M31', 'n', 'i', 'l', 1, 1, 1, 1, 1, 1)")
        self.connection.commit()

        report = self.registry.inspect()

        self.assertTrue(report.dry_run)
        self.assertEqual(report.filesystem_only, ["M31"])
        self.assertEqual(report.created, [])
        self.assertEqual(report.mosaic_linked, ["p1"])
        self.assertEqual(self.repo.list_projects(), [])
        self.assertIsNone(self.connection.execute("SELECT project_id FROM mosaic_plans").fetchone()[0])

    def test_database_only_projects_are_reported_and_kept(self) -> None:
        self.make_project("kept")
        self.registry.register_existing()
        self.repo.register_project(dir_key="ghost")

        report = self.registry.register_existing()

        self.assertEqual(report.db_only, ["ghost"])
        self.assertIsNotNone(self.repo.get_project_by_dir_key("ghost"))
        self.assertTrue(report.needs_attention)

    def test_registration_adds_only_new_directories(self) -> None:
        self.make_project("old")
        self.registry.register_existing()
        old_id = self.repo.get_project_by_dir_key("old").id
        self.make_project("new", '{"sky_target": "M42"}')

        report = self.registry.register_existing()

        self.assertEqual(report.created, ["new"])
        self.assertEqual(report.unchanged, ["old"])
        self.assertEqual(self.repo.get_project_by_dir_key("old").id, old_id)

    # -- conflict behaviour ----------------------------------------------------------------

    def test_changed_project_json_never_overwrites_the_database(self) -> None:
        self.make_project("M31", '{"sky_target": "M31"}')
        self.registry.register_existing()
        metadata = self.root / "M31" / "project.json"
        metadata.write_text('{"sky_target": "Andromeda"}', encoding="utf-8")
        before = metadata.read_bytes()

        report = self.registry.register_existing()

        self.assertEqual(
            report.conflicts,
            [MetadataConflict(dir_key="M31", field="target_label", database_value="M31", project_json_value="Andromeda")],
        )
        self.assertEqual(report.unchanged, [])
        self.assertEqual(self.repo.get_project_by_dir_key("M31").target_label, "M31")  # database wins
        self.assertEqual(metadata.read_bytes(), before)  # and the file is not rewritten either

    def test_database_change_is_reported_against_an_unchanged_project_json(self) -> None:
        self.make_project("M31", '{"sky_target": "M31"}')
        self.registry.register_existing()
        project = self.repo.get_project_by_dir_key("M31")
        self.repo.set_target_label(project.id, "NGC 224")

        report = self.registry.register_existing()

        self.assertEqual([(c.database_value, c.project_json_value) for c in report.conflicts], [("NGC 224", "M31")])
        self.assertEqual(self.repo.get_project_by_dir_key("M31").target_label, "NGC 224")

    def test_missing_project_json_is_a_conflict_when_the_database_has_a_target(self) -> None:
        self.make_project("M31", '{"sky_target": "M31"}')
        self.registry.register_existing()
        (self.root / "M31" / "project.json").unlink()

        report = self.registry.register_existing()

        self.assertEqual([(c.database_value, c.project_json_value) for c in report.conflicts], [("M31", None)])
        self.assertEqual(self.repo.get_project_by_dir_key("M31").target_label, "M31")

    def test_display_name_and_other_canonical_fields_are_never_touched(self) -> None:
        self.make_project("M31", '{"sky_target": "M31"}')
        self.registry.register_existing()
        self.connection.execute("UPDATE projects SET display_name = 'Andromeda campaign', notes = 'mine', "
                                "status = 'archived' WHERE dir_key = 'M31'")
        self.connection.commit()

        report = self.registry.register_existing()

        project = self.repo.get_project_by_dir_key("M31")
        self.assertEqual((project.display_name, project.notes, project.status), ("Andromeda campaign", "mine", "archived"))
        self.assertEqual(report.unchanged, ["M31"])

    # -- mosaic backfill -------------------------------------------------------------------

    def test_existing_mosaic_plans_are_linked_by_exact_dir_key_only(self) -> None:
        self.make_project("M31")
        self.make_project("M27 \u2014 Dumbbell Nebula")
        for plan_id, slug in [("p-m31", "M31"), ("p-m27", "M27 \u2014 Dumbbell Nebula"), ("p-case", "m31"),
                              ("p-space", "M31 "), ("p-none", "Ghost")]:
            self.connection.execute(
                "INSERT INTO mosaic_plans (id, project_slug, name, imaging_profile_id, imaging_profile_label, "
                "fov_width_deg, fov_height_deg, center_ra_deg, center_dec_deg, region_width_deg, region_height_deg) "
                "VALUES (?, ?, 'n', 'i', 'l', 1, 1, 1, 1, 1, 1)", (plan_id, slug))
        self.connection.commit()

        report = self.registry.register_existing()

        ids = self.repo.dir_key_to_id()
        rows = dict(self.connection.execute("SELECT id, project_id FROM mosaic_plans").fetchall())
        self.assertEqual(rows["p-m31"], ids["M31"])
        self.assertEqual(rows["p-m27"], ids["M27 \u2014 Dumbbell Nebula"])
        for unmatched in ("p-case", "p-space", "p-none"):
            self.assertIsNone(rows[unmatched], unmatched)
        self.assertEqual(sorted(report.mosaic_linked), ["p-m27", "p-m31"])
        self.assertEqual(sorted(slug for _, slug in report.mosaic_unmatched), ["Ghost", "M31 ", "m31"])

        again = self.registry.register_existing()
        self.assertEqual(again.mosaic_linked, [])
        self.assertEqual(len(again.mosaic_unmatched), 3)

    def test_backfill_leaves_already_linked_plans_and_updated_at_alone(self) -> None:
        self.make_project("M31")
        self.registry.register_existing()
        MosaicRepository(self.connection).save_mosaic_plan(self.plan("linked", "M31"))
        self.connection.execute("UPDATE mosaic_plans SET updated_at = '2001-01-01 00:00:00'")
        self.connection.commit()

        self.registry.register_existing()

        self.assertEqual(
            self.connection.execute("SELECT updated_at FROM mosaic_plans").fetchone()[0], "2001-01-01 00:00:00"
        )


class DualWriteTests(RegistryTestCase):
    # -- create ----------------------------------------------------------------------------

    def test_create_makes_the_directory_layout_and_a_canonical_record(self) -> None:
        summary, record = self.registry.create_project("M42")

        self.assertEqual(summary.slug, "M42")
        self.assertTrue((self.root / "M42" / "captures").is_dir())
        self.assertTrue((self.root / "M42" / "runs").is_dir())
        stored = self.repo.get_project_by_dir_key("M42")
        self.assertEqual(stored, record)
        self.assertRegex(record.id, r"^project:[0-9a-f]{12}$")
        self.assertEqual((record.display_name, record.target_label, record.target_id), ("M42", None, None))
        self.assertFalse((self.root / "M42" / "project.json").exists())  # as before: written on first target

    def test_create_is_idempotent_and_keeps_the_identity(self) -> None:
        _, first = self.registry.create_project("M42")
        _, second = self.registry.create_project("M42")
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.repo.list_projects()), 1)

    def test_create_registers_an_existing_unregistered_directory_with_its_project_json(self) -> None:
        self.make_project("M31", '{"sky_target": "M31"}', layout=True)
        _, record = self.registry.create_project("M31")
        self.assertEqual(record.target_label, "M31")

    def test_names_with_spaces_and_unicode_are_valid(self) -> None:
        _, record = self.registry.create_project("M27 \u2014 Dumbbell Nebula")
        self.assertEqual(record.dir_key, "M27 \u2014 Dumbbell Nebula")
        self.assertTrue((self.root / "M27 \u2014 Dumbbell Nebula").is_dir())

    def test_invalid_names_are_rejected_before_anything_is_created(self) -> None:
        before = tree_snapshot(self.dir)
        for name in ("", "   ", ".", "..", "../escape", "a/b", "a\\b", "/abs", "C:\\temp", "C:", "nul\x00byte"):
            with self.subTest(name), self.assertRaises(ValueError):
                self.registry.create_project(name)
        self.assertEqual(tree_snapshot(self.dir), before)
        self.assertEqual(self.repo.list_projects(), [])
        self.assertEqual(validate_dir_key("M31"), "M31")

    def test_record_failure_removes_the_directory_created_by_this_call(self) -> None:
        with patch.object(ProjectRepository, "register_project", side_effect=RuntimeError("database down")):
            with self.assertRaises(RuntimeError):
                self.registry.create_project("M42")

        self.assertFalse((self.root / "M42").exists())  # no filesystem project without identity
        self.assertEqual(self.repo.list_projects(), [])

    def test_record_failure_never_removes_a_directory_that_already_existed(self) -> None:
        self.make_project("Existing", layout=True)
        with patch.object(ProjectRepository, "register_project", side_effect=RuntimeError("database down")):
            with self.assertRaises(RuntimeError):
                self.registry.create_project("Existing")
        self.assertTrue((self.root / "Existing").is_dir())

    def test_compensation_never_removes_a_directory_that_contains_files(self) -> None:
        def register_then_fail(*args, **kwargs):
            (self.root / "Busy" / "captures" / "important.fit").write_bytes(b"data")
            raise RuntimeError("database down")

        with patch.object(ProjectRepository, "register_project", side_effect=register_then_fail):
            with self.assertRaises(RuntimeError):
                self.registry.create_project("Busy")
        self.assertTrue((self.root / "Busy" / "captures" / "important.fit").exists())

    def test_filesystem_failure_leaves_no_record(self) -> None:
        with patch.object(ProjectStorage, "create_project", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.registry.create_project("M42")
        self.assertEqual(self.repo.list_projects(), [])
        self.assertFalse((self.root / "M42").exists())

    def test_a_directory_left_without_a_record_is_healed_by_the_next_registration(self) -> None:
        self.make_project("Orphan", layout=True)
        self.assertEqual(self.repo.list_projects(), [])
        report = self.registry.register_existing()
        self.assertEqual(report.created, ["Orphan"])

    # -- target label / project.json mirror -----------------------------------------------------

    def test_set_target_label_updates_the_record_and_mirrors_project_json(self) -> None:
        self.registry.create_project("M31")
        summary, record = self.registry.set_target_label("M31", "  M31  ")

        self.assertEqual(record.target_label, "M31")  # normalised exactly like the legacy behaviour
        self.assertEqual(summary.sky_target, "M31")
        self.assertEqual(json.loads((self.root / "M31" / "project.json").read_text(encoding="utf-8")), {"sky_target": "M31"})
        self.assertEqual(self.repo.get_project_by_dir_key("M31").target_label, "M31")

    def test_blank_and_none_clear_the_label_in_both_places(self) -> None:
        self.registry.create_project("M31")
        self.registry.set_target_label("M31", "M31")
        for value in ("   ", None):
            self.registry.set_target_label("M31", "M31")
            summary, record = self.registry.set_target_label("M31", value)
            self.assertIsNone(record.target_label)
            self.assertIsNone(summary.sky_target)
            self.assertEqual(json.loads((self.root / "M31" / "project.json").read_text(encoding="utf-8")),
                             {"sky_target": None})

    def test_set_target_label_keeps_other_project_json_keys_and_never_touches_target_id(self) -> None:
        PlanningRepository(self.connection).create_target(
            Target(id="target:m31", catalog="M", catalog_id="31", name="M31", ra_deg=10.68, dec_deg=41.27)
        )
        self.make_project("M31", '{"sky_target": "old", "extra": {"keep": true}}', layout=True)
        self.registry.register_existing()
        self.connection.execute("UPDATE projects SET target_id = 'target:m31' WHERE dir_key = 'M31'")
        self.connection.commit()

        _, record = self.registry.set_target_label("M31", "Andromeda")

        self.assertEqual(record.target_id, "target:m31")
        self.assertEqual(json.loads((self.root / "M31" / "project.json").read_text(encoding="utf-8")),
                         {"sky_target": "Andromeda", "extra": {"keep": True}})

    def test_set_target_label_registers_an_unregistered_existing_project_first(self) -> None:
        self.make_project("M31", '{"sky_target": "old"}', layout=True)
        _, record = self.registry.set_target_label("M31", "new")
        self.assertEqual(record.target_label, "new")
        self.assertEqual(len(self.repo.list_projects()), 1)

    def test_set_target_label_on_a_new_name_creates_the_project_like_before(self) -> None:
        summary, record = self.registry.set_target_label("Brand New", "M13")
        self.assertTrue((self.root / "Brand New" / "captures").is_dir())
        self.assertEqual((summary.sky_target, record.target_label, record.dir_key), ("M13", "M13", "Brand New"))

    def test_mirror_failure_restores_the_canonical_value_and_raises(self) -> None:
        self.registry.create_project("M31")
        self.registry.set_target_label("M31", "M31")

        with patch.object(ProjectStorage, "set_project_sky_target", side_effect=OSError("read-only file system")):
            with self.assertRaises(OSError):
                self.registry.set_target_label("M31", "Andromeda")

        self.assertEqual(self.repo.get_project_by_dir_key("M31").target_label, "M31")
        self.assertEqual(json.loads((self.root / "M31" / "project.json").read_text(encoding="utf-8")),
                         {"sky_target": "M31"})

    def test_invalid_name_is_rejected_for_target_updates(self) -> None:
        with self.assertRaises(ValueError):
            self.registry.set_target_label("../escape", "x")
        self.assertFalse((self.dir / "escape").exists())


class DeleteSemanticsTests(RegistryTestCase):
    def linked_plan(self, slug: str = "M31") -> None:
        MosaicRepository(self.connection).save_mosaic_plan(self.plan("plan-1", slug))

    def test_delete_without_dependents_removes_directory_and_record(self) -> None:
        self.registry.create_project("M42")
        (self.root / "M42" / "captures" / "Orion").mkdir()
        self.registry.delete_project("M42")
        self.assertFalse((self.root / "M42").exists())
        self.assertIsNone(self.repo.get_project_by_dir_key("M42"))

    def test_delete_is_refused_while_mosaic_plans_reference_the_project(self) -> None:
        self.registry.create_project("M31")
        self.linked_plan()
        before = tree_snapshot(self.root)

        with self.assertRaises(ProjectInUseError) as caught:
            self.registry.delete_project("M31")

        self.assertEqual(caught.exception.mosaic_plan_count, 1)
        self.assertEqual(tree_snapshot(self.root), before)  # nothing on disk changed
        self.assertIsNotNone(self.repo.get_project_by_dir_key("M31"))
        self.assertEqual(MosaicRepository(self.connection).get_mosaic_plan("plan-1").project_slug, "M31")

    def test_delete_succeeds_after_the_plans_are_deleted(self) -> None:
        self.registry.create_project("M31")
        self.linked_plan()
        MosaicRepository(self.connection).delete_mosaic_plan("plan-1")
        self.registry.delete_project("M31")
        self.assertFalse((self.root / "M31").exists())
        self.assertIsNone(self.repo.get_project_by_dir_key("M31"))

    def test_the_database_itself_refuses_to_delete_a_referenced_project(self) -> None:
        self.registry.create_project("M31")
        self.linked_plan()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM projects WHERE dir_key = 'M31'")
        self.connection.rollback()
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM mosaic_plans").fetchone()[0], 1)

    def test_unlinked_plans_for_other_projects_do_not_block_deletion(self) -> None:
        self.registry.create_project("M42")
        self.registry.create_project("M31")
        self.linked_plan("M31")
        self.registry.delete_project("M42")
        self.assertIsNone(self.repo.get_project_by_dir_key("M42"))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM mosaic_plans").fetchone()[0], 1)

    def test_unknown_project_is_not_found(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.registry.delete_project("nothing-here")

    def test_filesystem_only_project_is_deleted_as_before(self) -> None:
        self.make_project("Legacy", layout=True)
        self.registry.delete_project("Legacy")
        self.assertFalse((self.root / "Legacy").exists())

    def test_database_only_project_can_be_cleaned_up(self) -> None:
        self.registry.create_project("Gone")
        shutil.rmtree(self.root / "Gone")
        self.assertEqual(self.registry.inspect().db_only, ["Gone"])
        self.registry.delete_project("Gone")
        self.assertIsNone(self.repo.get_project_by_dir_key("Gone"))

    def test_directory_removal_failure_keeps_the_record(self) -> None:
        self.registry.create_project("M42")
        with patch.object(ProjectStorage, "delete_project", side_effect=OSError("file is in use")):
            with self.assertRaises(OSError):
                self.registry.delete_project("M42")
        self.assertIsNotNone(self.repo.get_project_by_dir_key("M42"))
        self.assertTrue((self.root / "M42").is_dir())

    def test_record_removal_failure_leaves_a_reported_database_only_project_that_a_retry_finishes(self) -> None:
        self.registry.create_project("M42")
        with patch.object(ProjectRepository, "delete_project", side_effect=RuntimeError("database down")):
            with self.assertRaises(RuntimeError):
                self.registry.delete_project("M42")

        self.assertFalse((self.root / "M42").exists())
        self.assertIsNotNone(self.repo.get_project_by_dir_key("M42"))  # identity is not silently lost
        self.assertEqual(self.registry.inspect().db_only, ["M42"])

        self.registry.delete_project("M42")  # retry completes the deletion
        self.assertIsNone(self.repo.get_project_by_dir_key("M42"))


# ---------------------------------------------------------------------------
# Mosaic linkage
# ---------------------------------------------------------------------------


class MosaicProjectLinkTests(RegistryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.mosaics = MosaicRepository(self.connection)
        _, self.m31 = self.registry.create_project("M31")

    def test_new_plan_gets_the_project_id_when_the_slug_resolves(self) -> None:
        saved = self.mosaics.save_mosaic_plan(self.plan("a", "M31"))
        self.assertEqual(saved.project_id, self.m31.id)
        self.assertEqual(saved.project_slug, "M31")  # legacy field is untouched
        self.assertEqual(self.mosaics.get_mosaic_plan("a").project_id, self.m31.id)

    def test_new_plan_stays_unlinked_when_the_project_is_unknown(self) -> None:
        saved = self.mosaics.save_mosaic_plan(self.plan("b", "Unknown Project"))
        self.assertIsNone(saved.project_id)
        self.assertEqual(saved.project_slug, "Unknown Project")

    def test_slug_resolution_is_exact(self) -> None:
        for slug in ("m31", "M31 ", " M31", "M3"):
            with self.subTest(slug):
                self.assertIsNone(self.mosaics.save_mosaic_plan(self.plan(f"x-{len(slug)}-{slug!r}", slug)).project_id)

    def test_updating_a_plan_relinks_by_slug(self) -> None:
        self.mosaics.save_mosaic_plan(self.plan("c", "Unknown"))
        _, other = self.registry.create_project("Other")
        moved = self.mosaics.save_mosaic_plan(self.plan("c", "Other"))
        self.assertEqual(moved.project_id, other.id)
        back = self.mosaics.save_mosaic_plan(self.plan("c", "Unknown"))
        self.assertIsNone(back.project_id)

    def test_resaving_a_linked_plan_without_a_project_id_keeps_the_link(self) -> None:
        first = self.mosaics.save_mosaic_plan(self.plan("d", "M31"))
        again = self.mosaics.save_mosaic_plan(self.plan("d", "M31", status="ready"))
        self.assertEqual(again.project_id, first.project_id)

    def test_explicit_project_id_must_exist_and_match_the_slug(self) -> None:
        _, other = self.registry.create_project("Other")
        with self.assertRaises(ValidationError):
            self.mosaics.save_mosaic_plan(self.plan("e", "M31", project_id=other.id))
        with self.assertRaises(ValidationError):
            self.mosaics.save_mosaic_plan(self.plan("f", "M31", project_id="project:doesnotexist"))
        ok = self.mosaics.save_mosaic_plan(self.plan("g", "M31", project_id=self.m31.id))
        self.assertEqual(ok.project_id, self.m31.id)

    def test_listing_by_slug_still_works_for_linked_and_unlinked_plans(self) -> None:
        self.mosaics.save_mosaic_plan(self.plan("h", "M31"))
        self.mosaics.save_mosaic_plan(self.plan("i", "Unknown"))
        self.assertEqual([p.id for p in self.mosaics.list_mosaic_plans(project_slug="M31")], ["h"])
        self.assertEqual([p.id for p in self.mosaics.list_mosaic_plans(project_slug="Unknown")], ["i"])
        self.assertEqual(len(self.mosaics.list_mosaic_plans()), 2)

    def test_generating_panels_keeps_the_link(self) -> None:
        self.mosaics.save_mosaic_plan(self.plan("j", "M31"))
        self.mosaics.generate_panels("j")
        plan = self.mosaics.get_mosaic_plan("j")
        self.assertGreater(len(plan.panels), 0)
        self.assertEqual(plan.project_id, self.m31.id)

    def test_deleting_a_plan_never_deletes_the_project(self) -> None:
        self.mosaics.save_mosaic_plan(self.plan("k", "M31"))
        self.mosaics.delete_mosaic_plan("k")
        self.assertIsNotNone(self.repo.get_project_by_dir_key("M31"))

    def test_project_and_mosaic_data_round_trip_through_reopen(self) -> None:
        self.mosaics.save_mosaic_plan(self.plan("l", "M31"))
        self.connection.close()
        reopened = self.open_initialized(self.dir / "registry.db")
        self.assertEqual(MosaicRepository(reopened).get_mosaic_plan("l").project_id, self.m31.id)
        self.assertEqual(ProjectRepository(reopened).get_project(self.m31.id), self.m31)


# ---------------------------------------------------------------------------
# HTTP API compatibility
# ---------------------------------------------------------------------------


class ProjectApiTests(unittest.TestCase):
    LEGACY_PROJECT_KEYS = {
        "slug", "project_root", "captures_dir", "runs_dir", "capture_count", "run_count",
        "capture_names", "run_names", "sky_target",
    }

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        # Registered first, so (cleanups run last-in-first-out) it runs after every connection is closed.
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name) / "projects"
        for name, payload in (("M31", '{"sky_target": "M31"}'), ("Cygnus Loop", None)):
            (self.root / name / "captures").mkdir(parents=True)
            (self.root / name / "runs").mkdir(parents=True)
            if payload:
                (self.root / name / "project.json").write_text(payload, encoding="utf-8")
        self.start_server()

    def start_server(self) -> None:
        self.server = create_http_server(host="127.0.0.1", port=0, projects_root=self.root)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def tearDown(self) -> None:
        self.stop_server()

    def call(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(f"{self.base}{path}", data=data, method=method,
                          headers={"Content-Type": "application/json"} if data else {})
        try:
            with urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def db(self) -> sqlite3.Connection:
        connection = connect_database(self.root / "tsn_dss.db")
        self.addCleanup(connection.close)
        return connection

    def plan_payload(self, slug: str) -> dict:
        return {"project_slug": slug, "name": "Plan", "center_ra_deg": 10.0, "center_dec_deg": 41.0,
                "region_width_deg": 5.0, "region_height_deg": 3.0}

    def test_startup_registers_existing_projects_without_renaming_anything(self) -> None:
        report = self.server.project_registration_report
        self.assertEqual(sorted(report.created), ["Cygnus Loop", "M31"])
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.is_dir()), ["Cygnus Loop", "M31"])
        self.assertEqual({p.dir_key: p.target_label for p in ProjectRepository(self.db()).list_projects()},
                         {"M31": "M31", "Cygnus Loop": None})

    def test_restarting_the_server_registers_nothing_new_and_keeps_ids(self) -> None:
        ids = ProjectRepository(self.db()).dir_key_to_id()
        self.stop_server()
        self.start_server()
        report = self.server.project_registration_report
        self.assertEqual(report.created, [])
        self.assertEqual(sorted(report.unchanged), ["Cygnus Loop", "M31"])
        self.assertEqual(ProjectRepository(self.db()).dir_key_to_id(), ids)

    def test_project_payloads_keep_every_legacy_key_and_add_project_id(self) -> None:
        ids = ProjectRepository(self.db()).dir_key_to_id()
        _, listing = self.call("GET", "/api/projects")
        by_slug = {project["slug"]: project for project in listing["projects"]}
        self.assertEqual(set(by_slug), {"M31", "Cygnus Loop"})
        for slug, project in by_slug.items():
            self.assertTrue(self.LEGACY_PROJECT_KEYS <= set(project))
            self.assertEqual(project["project_id"], ids[slug])
        self.assertEqual(by_slug["M31"]["sky_target"], "M31")

        _, detail = self.call("GET", "/api/projects/M31")
        self.assertEqual(detail["project"]["project_id"], ids["M31"])
        self.assertEqual(detail["project"]["slug"], "M31")

    def test_creating_a_project_creates_the_identity_and_keeps_the_response_shape(self) -> None:
        status, body = self.call("POST", "/api/projects", {"slug": "M42"})
        self.assertEqual(status, 201)
        self.assertTrue(self.LEGACY_PROJECT_KEYS <= set(body["project"]))
        self.assertEqual(body["project"]["slug"], "M42")
        self.assertRegex(body["project"]["project_id"], r"^project:[0-9a-f]{12}$")
        self.assertTrue((self.root / "M42" / "captures").is_dir())
        self.assertEqual(ProjectRepository(self.db()).get_project_by_dir_key("M42").id, body["project"]["project_id"])

        again_status, again = self.call("POST", "/api/projects", {"slug": "M42"})  # idempotent like before
        self.assertEqual((again_status, again["project"]["project_id"]), (201, body["project"]["project_id"]))

    def test_invalid_project_names_are_a_400_and_create_nothing(self) -> None:
        before = sorted(p.name for p in self.root.parent.iterdir())
        for slug in ("../escape", "a/b", ".."):
            status, body = self.call("POST", "/api/projects", {"slug": slug})
            self.assertEqual(status, 400, slug)
            self.assertEqual(body["error"], "project_create_failed")
        self.assertEqual(sorted(p.name for p in self.root.parent.iterdir()), before)

    def test_sky_target_update_writes_the_record_and_the_mirror(self) -> None:
        status, body = self.call("POST", "/api/projects/Cygnus%20Loop/sky-target", {"sky_target": " NGC6960 "})
        self.assertEqual(status, 200)
        self.assertEqual(body["project"]["sky_target"], "NGC6960")
        self.assertTrue(body["project"]["project_id"])
        self.assertEqual(ProjectRepository(self.db()).get_project_by_dir_key("Cygnus Loop").target_label, "NGC6960")
        self.assertEqual(json.loads((self.root / "Cygnus Loop" / "project.json").read_text(encoding="utf-8")),
                         {"sky_target": "NGC6960"})
        _, detail = self.call("GET", "/api/projects/Cygnus%20Loop")
        self.assertEqual(detail["project"]["sky_target"], "NGC6960")

    def test_importing_a_capture_into_a_new_project_name_registers_the_project(self) -> None:
        source = Path(self.temp_dir.name) / "source"
        for folder in ("biases", "darks", "flats", "lights"):
            (source / folder).mkdir(parents=True)
            (source / folder / f"{folder}_001.CR2").write_bytes(b"raw")

        status, body = self.call("POST", "/api/import-capture",
                                 {"project_slug": "Fresh", "capture_name": "Night1", "source_dir": str(source)})

        self.assertEqual(status, 201)
        self.assertRegex(body["project"]["project_id"], r"^project:[0-9a-f]{12}$")
        self.assertIsNotNone(ProjectRepository(self.db()).get_project_by_dir_key("Fresh"))
        self.assertEqual(body["project"]["capture_names"], ["Night1"])

    def test_import_into_an_invalid_project_name_is_refused_before_copying(self) -> None:
        source = Path(self.temp_dir.name) / "source2"
        for folder in ("biases", "darks", "flats", "lights"):
            (source / folder).mkdir(parents=True)
        status, body = self.call("POST", "/api/import-capture",
                                 {"project_slug": "../escape", "capture_name": "x", "source_dir": str(source)})
        self.assertEqual((status, body["error"]), (400, "capture_import_failed"))
        self.assertFalse((self.root.parent / "escape").exists())

    def test_new_mosaic_plans_carry_the_project_id_and_keep_the_slug(self) -> None:
        ids = ProjectRepository(self.db()).dir_key_to_id()
        status, body = self.call("POST", "/api/mosaics", self.plan_payload("M31"))
        self.assertEqual(status, 201)
        self.assertEqual(body["mosaic"]["project_id"], ids["M31"])
        self.assertEqual(body["mosaic"]["project_slug"], "M31")

        _, unknown = self.call("POST", "/api/mosaics", self.plan_payload("Nobody"))
        self.assertIsNone(unknown["mosaic"]["project_id"])

        _, listing = self.call("GET", "/api/mosaics?project_slug=M31")
        self.assertEqual([m["project_id"] for m in listing["mosaics"]], [ids["M31"]])

    def test_deleting_a_project_with_mosaic_plans_is_refused_with_a_clear_conflict(self) -> None:
        _, created = self.call("POST", "/api/mosaics", self.plan_payload("M31"))
        mosaic_id = created["mosaic"]["id"]

        status, body = self.call("DELETE", "/api/projects/M31")

        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "project_has_dependents")
        self.assertEqual(body["mosaic_plan_count"], 1)
        self.assertIn("mosaic plan", body["message"])  # the frontend shows this text
        self.assertTrue((self.root / "M31").is_dir())
        self.assertIsNotNone(ProjectRepository(self.db()).get_project_by_dir_key("M31"))
        _, plans = self.call("GET", "/api/mosaics?project_slug=M31")
        self.assertEqual(len(plans["mosaics"]), 1)

        self.assertEqual(self.call("DELETE", f"/api/mosaics/{mosaic_id}")[0], 200)
        status, body = self.call("DELETE", "/api/projects/M31")
        self.assertEqual((status, body["deleted"]), (200, True))
        self.assertFalse((self.root / "M31").exists())
        self.assertIsNone(ProjectRepository(self.db()).get_project_by_dir_key("M31"))

    def test_deleting_a_project_without_dependents_works_as_before(self) -> None:
        status, body = self.call("DELETE", "/api/projects/Cygnus%20Loop")
        self.assertEqual((status, body["deleted"], body["project_slug"]), (200, True, "Cygnus Loop"))
        self.assertFalse((self.root / "Cygnus Loop").exists())

    def test_deleting_an_unknown_project_is_still_a_404(self) -> None:
        status, body = self.call("DELETE", "/api/projects/Nothing")
        self.assertEqual((status, body["error"]), (404, "project_not_found"))


if __name__ == "__main__":
    unittest.main()
