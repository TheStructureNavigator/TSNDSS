from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsn_dss.domain.models import MosaicPlan, PlannedPointing, Target
from tsn_dss.engine.sqlite.db import initialize_database
from tsn_dss.engine.sqlite.mosaics import MosaicRepository
from tsn_dss.engine.sqlite.planning import PlanningRepository
from tsn_dss.engine.target_resolution import TargetResolutionRequest, resolve_astronomical_target_context


class TargetResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "targets.db"
        self.connection = initialize_database(self.db_path)
        self.planning = PlanningRepository(self.connection)
        self.mosaics = MosaicRepository(self.connection)
        self.planning.create_target(
            Target(
                id="target:m31",
                catalog="Messier",
                catalog_id="M31",
                name="Andromeda Galaxy",
                ra_deg=10.6847,
                dec_deg=41.2692,
            )
        )
        self.mosaics.save_mosaic_plan(
            MosaicPlan(
                id="mosaic:m31",
                project_slug="m31",
                name="M31 Mosaic",
                imaging_profile_id="wide",
                imaging_profile_label="Wide",
                fov_width_deg=2.0,
                fov_height_deg=1.0,
                center_ra_deg=12.0,
                center_dec_deg=42.0,
                region_width_deg=2.0,
                region_height_deg=1.0,
            )
        )
        self.panel = self.mosaics.generate_panels("mosaic:m31")[0]

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def test_explicit_coordinates_take_precedence_over_catalog_query(self) -> None:
        resolved = resolve_astronomical_target_context(
            TargetResolutionRequest(
                target_ra_deg=123.0,
                target_dec_deg=-4.0,
                target_name="M31",
                source_kind="manual-test",
                source_id="manual:1",
            ),
            planning_repository=self.planning,
            mosaic_repository=self.mosaics,
        )

        assert resolved is not None
        self.assertEqual(resolved.ra_deg, 123.0)
        self.assertEqual(resolved.dec_deg, -4.0)
        self.assertEqual(resolved.source_kind, "manual-test")

    def test_catalog_name_resolves_before_mosaic_panel(self) -> None:
        resolved = resolve_astronomical_target_context(
            TargetResolutionRequest(target_name="M31", mosaic_panel_id=self.panel.id),
            planning_repository=self.planning,
            mosaic_repository=self.mosaics,
        )

        assert resolved is not None
        self.assertEqual(resolved.source_kind, "target")
        self.assertEqual(resolved.source_id, "target:m31")

    def test_mosaic_panel_resolves_before_target_id(self) -> None:
        resolved = resolve_astronomical_target_context(
            TargetResolutionRequest(mosaic_panel_id=self.panel.id, target_id="target:m31"),
            planning_repository=self.planning,
            mosaic_repository=self.mosaics,
        )

        assert resolved is not None
        self.assertEqual(resolved.source_kind, "mosaic_panel")
        self.assertEqual(resolved.source_id, self.panel.id)

    def test_target_id_resolves_before_planned_pointing(self) -> None:
        resolved = resolve_astronomical_target_context(
            TargetResolutionRequest(
                target_id="target:m31",
                use_planned_pointing=True,
                planned_pointing=PlannedPointing(target_name="Manual", ra_hours=8.0, dec_deg=9.0),
            ),
            planning_repository=self.planning,
            mosaic_repository=self.mosaics,
        )

        assert resolved is not None
        self.assertEqual(resolved.source_kind, "target")
        self.assertEqual(resolved.source_id, "target:m31")

    def test_planned_pointing_is_used_last(self) -> None:
        resolved = resolve_astronomical_target_context(
            TargetResolutionRequest(
                use_planned_pointing=True,
                planned_pointing=PlannedPointing(target_name="Manual", ra_hours=8.0, dec_deg=9.0),
            ),
            planning_repository=self.planning,
            mosaic_repository=self.mosaics,
        )

        assert resolved is not None
        self.assertEqual(resolved.ra_deg, 120.0)
        self.assertEqual(resolved.dec_deg, 9.0)


if __name__ == "__main__":
    unittest.main()
