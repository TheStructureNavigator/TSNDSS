from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsn_dss.domain.models import MosaicPlan
from tsn_dss.engine.sqlite.db import initialize_database
from tsn_dss.engine.sqlite.mosaics import MosaicRepository, ValidationError


class MosaicRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "mosaics.db"
        self.connection = initialize_database(self.db_path)
        self.repository = MosaicRepository(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

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
        self.assertEqual(reloaded.panels, [])

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
