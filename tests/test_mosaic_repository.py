from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsn_dss.domain.models import MosaicPlan
from tsn_dss.engine.sqlite.db import DEFAULT_SCHEMA_PATH, connect_database, initialize_database
from tsn_dss.engine.sqlite.migrations import initialize_schema
from tsn_dss.engine.sqlite.mosaics import MosaicRepository, ValidationError
from tsn_dss.engine.sqlite.project_repository import ProjectRepository


class MosaicRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "mosaics.db"
        self.connection = initialize_database(self.db_path)
        self.repository = MosaicRepository(self.connection)
        self.projects = ProjectRepository(self.connection)
        for slug in ("veil_project", "rosette_project", "legacy_project", "wrap_project", "orion_project"):
            self.projects.register_project(dir_key=slug)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def plan(self, plan_id: str = "mosaic:test", project_slug: str = "veil_project", **overrides) -> MosaicPlan:
        name = overrides.pop("name", "Test Mosaic")
        return MosaicPlan(
            id=plan_id,
            project_slug=project_slug,
            name=name,
            imaging_profile_id="widefield",
            imaging_profile_label="Widefield",
            fov_width_deg=3.0,
            fov_height_deg=2.0,
            center_ra_deg=15.0,
            center_dec_deg=41.0,
            region_width_deg=4.0,
            region_height_deg=3.0,
            **overrides,
        )

    def test_save_and_reload_mosaic_plan_round_trip(self) -> None:
        saved = self.repository.save_mosaic_plan(
            MosaicPlan(
                id="mosaic:veil",
                project_slug="veil_project",
                name="Veil Core Mosaic",
                target_name="Cygnus Loop",
                imaging_profile_id="seestar_s30_pro",
                imaging_profile_label="Seestar S30 Pro",
                fov_width_deg=2.59,
                fov_height_deg=1.47,
                center_ra_deg=312.5,
                center_dec_deg=31.0,
                region_width_deg=3.0,
                region_height_deg=3.0,
                overlap_percent=25.0,
            )
        )

        reloaded = self.repository.get_mosaic_plan(saved.id)

        assert reloaded is not None
        self.assertEqual(reloaded.project_slug, "veil_project")
        self.assertEqual(reloaded.imaging_profile_label, "Seestar S30 Pro")
        self.assertEqual(reloaded.overlap_percent, 25.0)
        self.assertIsNone(reloaded.observation_type)
        self.assertIsNone(reloaded.filter)
        self.assertEqual(reloaded.panels, [])

    def test_save_and_reload_mosaic_plan_with_observation_intent_fields(self) -> None:
        saved = self.repository.save_mosaic_plan(
            MosaicPlan(
                id="mosaic:rosette",
                project_slug="rosette_project",
                name="Rosette Core",
                target_name="Rosette Nebula",
                observation_type="dual-band imaging",
                filter="L-eXtreme",
                imaging_profile_id="seestar_s30_pro",
                imaging_profile_label="Seestar S30 Pro",
                fov_width_deg=2.59,
                fov_height_deg=1.47,
                center_ra_deg=97.0,
                center_dec_deg=4.9,
                region_width_deg=3.0,
                region_height_deg=3.0,
                overlap_percent=20.0,
            )
        )

        reloaded = self.repository.get_mosaic_plan(saved.id)

        assert reloaded is not None
        self.assertEqual(reloaded.observation_type, "dual-band imaging")
        self.assertEqual(reloaded.filter, "L-eXtreme")

    def test_initialize_database_adds_optional_columns_to_legacy_mosaic_schema(self) -> None:
        # Schema evolution belongs to initialize_database (tsn_dss.engine.sqlite.migrations);
        # the repository itself never alters the schema.
        self.connection.close()
        self.db_path.unlink(missing_ok=True)
        legacy = connect_database(self.db_path)
        # A genuine v1 database: the frozen legacy normalizer only applies to user_version = 1.
        initialize_schema(legacy, baseline_path=DEFAULT_SCHEMA_PATH, migrations=())
        try:
            legacy.execute("DROP TABLE IF EXISTS mosaic_panels;")
            legacy.execute("DROP TABLE IF EXISTS mosaic_plans;")
            legacy.executescript(
                """
                CREATE TABLE mosaic_plans (
                    id TEXT PRIMARY KEY,
                    project_slug TEXT NOT NULL,
                    name TEXT NOT NULL,
                    target_name TEXT,
                    imaging_profile_id TEXT NOT NULL,
                    imaging_profile_label TEXT NOT NULL,
                    fov_width_deg REAL NOT NULL,
                    fov_height_deg REAL NOT NULL,
                    center_ra_deg REAL NOT NULL,
                    center_dec_deg REAL NOT NULL,
                    region_width_deg REAL NOT NULL,
                    region_height_deg REAL NOT NULL,
                    rotation_deg REAL NOT NULL DEFAULT 0,
                    overlap_percent REAL NOT NULL DEFAULT 10,
                    status TEXT NOT NULL DEFAULT 'draft',
                    selected_panel_id TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE mosaic_panels (
                    id TEXT PRIMARY KEY,
                    mosaic_plan_id TEXT NOT NULL,
                    panel_index INTEGER NOT NULL,
                    panel_label TEXT NOT NULL,
                    center_ra_deg REAL NOT NULL,
                    center_dec_deg REAL NOT NULL,
                    fov_width_deg REAL NOT NULL,
                    fov_height_deg REAL NOT NULL,
                    rotation_deg REAL NOT NULL DEFAULT 0,
                    row_index INTEGER,
                    column_index INTEGER,
                    status TEXT NOT NULL DEFAULT 'not_started',
                    target_integration_seconds REAL,
                    acquired_integration_seconds REAL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            legacy.commit()
            legacy.close()

            reopened = initialize_database(self.db_path)
            try:
                ProjectRepository(reopened).register_project(dir_key="legacy_project")
                repository = MosaicRepository(reopened)
                created = repository.save_mosaic_plan(
                    MosaicPlan(
                        id="mosaic:legacy",
                        project_slug="legacy_project",
                        name="Legacy Mosaic",
                        target_name="M31",
                        observation_type="broadband imaging",
                        filter="UV/IR Cut",
                        imaging_profile_id="widefield",
                        imaging_profile_label="Widefield",
                        fov_width_deg=3.0,
                        fov_height_deg=2.0,
                        center_ra_deg=15.0,
                        center_dec_deg=41.0,
                        region_width_deg=4.0,
                        region_height_deg=3.0,
                    )
                )
                restored = repository.get_mosaic_plan(created.id)
            finally:
                reopened.close()
        finally:
            self.connection = initialize_database(self.db_path)
            self.repository = MosaicRepository(self.connection)

        assert restored is not None
        self.assertEqual(restored.observation_type, "broadband imaging")
        self.assertEqual(restored.filter, "UV/IR Cut")

    def test_new_mosaic_plan_requires_existing_project_ownership(self) -> None:
        with self.assertRaisesRegex(ValidationError, "does not resolve"):
            self.repository.save_mosaic_plan(self.plan("mosaic:unknown", "unknown_project"))
        self.assertIsNone(self.repository.get_mosaic_plan("mosaic:unknown"))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM mosaic_panels;").fetchone()[0], 0)

    def test_explicit_project_id_must_exist_and_match_slug(self) -> None:
        project = self.projects.get_project_by_dir_key("veil_project")
        assert project is not None
        saved = self.repository.save_mosaic_plan(
            self.plan("mosaic:explicit", "veil_project", project_id=project.id)
        )
        self.assertEqual(saved.project_id, project.id)

        with self.assertRaisesRegex(ValidationError, "refer to different projects"):
            self.repository.save_mosaic_plan(
                self.plan("mosaic:mismatch", "rosette_project", project_id=project.id)
            )
        with self.assertRaisesRegex(ValidationError, "Unknown project_id"):
            self.repository.save_mosaic_plan(
                self.plan("mosaic:missing-project", "veil_project", project_id="project:missing")
            )

    def test_linked_mosaic_plan_cannot_be_orphaned_or_reassigned(self) -> None:
        veil = self.projects.get_project_by_dir_key("veil_project")
        rosette = self.projects.get_project_by_dir_key("rosette_project")
        assert veil is not None and rosette is not None
        saved = self.repository.save_mosaic_plan(self.plan("mosaic:stable", "veil_project"))
        panels = self.repository.generate_panels(saved.id)
        before = self.repository.get_mosaic_plan(saved.id)
        assert before is not None

        allowed = self.repository.save_mosaic_plan(self.plan("mosaic:stable", "veil_project", name="Renamed"))
        self.assertEqual((allowed.project_id, allowed.name), (veil.id, "Renamed"))

        for label, candidate in (
            ("unknown slug", self.plan("mosaic:stable", "unknown_project", name="Bad")),
            ("different project slug", self.plan("mosaic:stable", "rosette_project", name="Bad")),
            ("different explicit project", self.plan("mosaic:stable", "rosette_project", project_id=rosette.id, name="Bad")),
        ):
            with self.subTest(label), self.assertRaises(ValidationError):
                self.repository.save_mosaic_plan(candidate)

        after = self.repository.get_mosaic_plan(saved.id)
        assert after is not None
        self.assertEqual((after.project_id, after.project_slug, after.name), (veil.id, "veil_project", "Renamed"))
        self.assertEqual([panel.id for panel in after.panels], [panel.id for panel in panels])

    def test_unresolved_legacy_plan_rejects_alternate_write_paths_without_changes(self) -> None:
        self.connection.executescript(
            """
            INSERT INTO mosaic_plans (
                id, project_slug, name, imaging_profile_id, imaging_profile_label,
                fov_width_deg, fov_height_deg, center_ra_deg, center_dec_deg, region_width_deg, region_height_deg
            ) VALUES ('legacy:ghost', 'Ghost', 'Legacy Ghost', 'profile', 'Profile', 3, 2, 15, 41, 4, 3);
            INSERT INTO mosaic_panels (
                id, mosaic_plan_id, panel_index, panel_label, center_ra_deg, center_dec_deg, fov_width_deg, fov_height_deg
            ) VALUES ('panel:ghost', 'legacy:ghost', 0, 'P01', 15, 41, 3, 2);
            """
        )
        self.connection.commit()

        before_plan = self.repository.get_mosaic_plan("legacy:ghost")
        before_panel = self.repository.get_mosaic_panel("panel:ghost")
        assert before_plan is not None and before_panel is not None

        with self.assertRaises(ValidationError):
            self.repository.generate_panels("legacy:ghost")
        with self.assertRaises(ValidationError):
            self.repository.select_active_panel("legacy:ghost", "panel:ghost")

        edited_panel = self.repository.get_mosaic_panel("panel:ghost")
        assert edited_panel is not None
        edited_panel.panel_label = "Edited"
        edited_panel.status = "complete"
        with self.assertRaises(ValidationError):
            self.repository.update_mosaic_panel(edited_panel)

        with self.assertRaises(ValidationError):
            self.repository.delete_mosaic_plan("legacy:ghost")

        after_plan = self.repository.get_mosaic_plan("legacy:ghost")
        after_panel = self.repository.get_mosaic_panel("panel:ghost")
        assert after_plan is not None and after_panel is not None
        self.assertIsNone(after_plan.project_id)
        self.assertIsNone(after_plan.selected_panel_id)
        self.assertEqual((after_plan.project_slug, after_plan.name), (before_plan.project_slug, before_plan.name))
        self.assertEqual((after_panel.panel_label, after_panel.status), (before_panel.panel_label, before_panel.status))
        self.assertEqual([panel.id for panel in after_plan.panels], ["panel:ghost"])

    def test_linked_plan_allows_alternate_write_paths(self) -> None:
        saved = self.repository.save_mosaic_plan(self.plan("mosaic:linked", "veil_project"))
        panels = self.repository.generate_panels(saved.id)
        first = panels[0]

        selected = self.repository.select_active_panel(saved.id, first.id)
        self.assertEqual(selected.selected_panel_id, first.id)

        first.panel_label = "Updated"
        first.status = "complete"
        updated = self.repository.update_mosaic_panel(first)
        self.assertEqual((updated.panel_label, updated.status), ("Updated", "complete"))

        self.repository.delete_mosaic_plan(saved.id)
        self.assertIsNone(self.repository.get_mosaic_plan(saved.id))
        self.assertIsNotNone(self.projects.get_project_by_dir_key("veil_project"))

    def test_generate_panels_persists_selected_panel_and_geometry(self) -> None:
        self.repository.save_mosaic_plan(
            MosaicPlan(
                id="mosaic:veil",
                project_slug="veil_project",
                name="Veil Core Mosaic",
                target_name="Cygnus Loop",
                imaging_profile_id="seestar_s30_pro",
                imaging_profile_label="Seestar S30 Pro",
                fov_width_deg=2.59,
                fov_height_deg=1.47,
                center_ra_deg=312.5,
                center_dec_deg=31.0,
                region_width_deg=3.0,
                region_height_deg=3.0,
                overlap_percent=25.0,
                rotation_deg=12.5,
            )
        )

        panels = self.repository.generate_panels("mosaic:veil")
        reloaded = self.repository.get_mosaic_plan("mosaic:veil")

        self.assertEqual(len(panels), 6)
        assert reloaded is not None
        self.assertEqual(reloaded.selected_panel_id, panels[0].id)
        self.assertEqual(len(reloaded.panels), 6)
        self.assertTrue(all(panel.rotation_deg == 12.5 for panel in reloaded.panels))
        self.assertEqual([panel.panel_label for panel in reloaded.panels], ["P01", "P02", "P03", "P04", "P05", "P06"])

    def test_overlap_changes_generated_panel_count(self) -> None:
        low_overlap = self.repository.save_mosaic_plan(
            MosaicPlan(
                id="mosaic:low-overlap",
                project_slug="veil_project",
                name="Low overlap",
                imaging_profile_id="seestar_s30_pro",
                imaging_profile_label="Seestar S30 Pro",
                fov_width_deg=2.59,
                fov_height_deg=1.47,
                center_ra_deg=312.5,
                center_dec_deg=31.0,
                region_width_deg=5.0,
                region_height_deg=3.0,
                overlap_percent=0.0,
            )
        )
        high_overlap = self.repository.save_mosaic_plan(
            MosaicPlan(
                id="mosaic:high-overlap",
                project_slug="veil_project",
                name="High overlap",
                imaging_profile_id="seestar_s30_pro",
                imaging_profile_label="Seestar S30 Pro",
                fov_width_deg=2.59,
                fov_height_deg=1.47,
                center_ra_deg=312.5,
                center_dec_deg=31.0,
                region_width_deg=5.0,
                region_height_deg=3.0,
                overlap_percent=50.0,
            )
        )

        low_panels = self.repository.generate_panels(low_overlap.id)
        high_panels = self.repository.generate_panels(high_overlap.id)

        self.assertLess(len(low_panels), len(high_panels))

    def test_generation_wraps_ra_around_zero_hours_boundary(self) -> None:
        self.repository.save_mosaic_plan(
            MosaicPlan(
                id="mosaic:wrap",
                project_slug="wrap_project",
                name="Wrap test",
                imaging_profile_id="widefield",
                imaging_profile_label="Widefield",
                fov_width_deg=4.0,
                fov_height_deg=2.0,
                center_ra_deg=359.0,
                center_dec_deg=5.0,
                region_width_deg=6.0,
                region_height_deg=2.0,
                overlap_percent=10.0,
            )
        )

        panels = self.repository.generate_panels("mosaic:wrap")

        self.assertEqual(len(panels), 2)
        self.assertTrue(any(panel.center_ra_deg < 10.0 for panel in panels))
        self.assertTrue(all(0.0 <= panel.center_ra_deg < 360.0 for panel in panels))

    def test_generated_panels_persist_after_database_reopen(self) -> None:
        self.repository.save_mosaic_plan(
            MosaicPlan(
                id="mosaic:orion",
                project_slug="orion_project",
                name="Orion test mosaic",
                imaging_profile_id="widefield",
                imaging_profile_label="Widefield",
                fov_width_deg=3.0,
                fov_height_deg=1.5,
                center_ra_deg=85.0,
                center_dec_deg=-4.0,
                region_width_deg=6.5,
                region_height_deg=3.0,
                overlap_percent=25.0,
            )
        )
        generated = self.repository.generate_panels("mosaic:orion")
        self.connection.close()

        reopened = initialize_database(self.db_path)
        try:
            repository = MosaicRepository(reopened)
            restored = repository.get_mosaic_plan("mosaic:orion")
        finally:
            reopened.close()
            self.connection = initialize_database(self.db_path)
            self.repository = MosaicRepository(self.connection)

        assert restored is not None
        self.assertEqual(len(restored.panels), len(generated))
        self.assertEqual(restored.panels[0].panel_label, generated[0].panel_label)

    def test_invalid_overlap_is_rejected_before_sqlite_write(self) -> None:
        with self.assertRaises(ValidationError):
            self.repository.save_mosaic_plan(
                MosaicPlan(
                    id="mosaic:bad-overlap",
                    project_slug="bad_project",
                    name="Bad overlap",
                    imaging_profile_id="widefield",
                    imaging_profile_label="Widefield",
                    fov_width_deg=3.0,
                    fov_height_deg=2.0,
                    center_ra_deg=20.0,
                    center_dec_deg=0.0,
                    region_width_deg=4.0,
                    region_height_deg=4.0,
                    overlap_percent=100.0,
                )
            )


if __name__ == "__main__":
    unittest.main()
