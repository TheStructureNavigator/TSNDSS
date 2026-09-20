from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from tsn_dss.domain.models import AcquisitionPlan, AcquisitionSequence, Equipment, LocalHorizonPoint, Site, Target
from tsn_dss.engine.sqlite.db import initialize_database
from tsn_dss.engine.sqlite.planning import PlanningRepository, ValidationError


class PlanningRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "planning.db"
        self.connection = initialize_database(self.db_path)
        self.repository = PlanningRepository(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def test_target_crud_round_trip(self) -> None:
        created = self.repository.create_target(
            Target(
                id="target:ngc7000",
                catalog="NGC",
                catalog_id="7000",
                name="North America Nebula",
                object_type="emission_nebula",
                ra_deg=312.5,
                dec_deg=44.3,
                constellation="Cygnus",
            )
        )

        self.assertEqual(created.name, "North America Nebula")

        updated = self.repository.update_target(
            Target(
                id="target:ngc7000",
                catalog="NGC",
                catalog_id="7000",
                name="North America Nebula Revised",
                object_type="emission_nebula",
                ra_deg=312.5,
                dec_deg=44.3,
                constellation="Cygnus",
                notes="Widefield target",
            )
        )
        listed = self.repository.list_targets()

        self.assertEqual(updated.notes, "Widefield target")
        self.assertEqual([target.id for target in listed], ["target:ngc7000"])

        self.repository.delete_target("target:ngc7000")
        self.assertIsNone(self.repository.get_target("target:ngc7000"))

    def test_find_target_by_query_matches_name_and_catalog_id(self) -> None:
        self.repository.create_target(
            Target(
                id="target:m31",
                catalog="Messier",
                catalog_id="M31",
                name="Andromeda Galaxy",
                ra_deg=10.6847083,
                dec_deg=41.26875,
            )
        )

        by_name = self.repository.find_target_by_query("Andromeda Galaxy")
        by_catalog_id = self.repository.find_target_by_query("M31")

        assert by_name is not None
        assert by_catalog_id is not None
        self.assertEqual(by_name.id, "target:m31")
        self.assertEqual(by_catalog_id.id, "target:m31")

    def test_site_crud_round_trip(self) -> None:
        created = self.repository.create_site(
            Site(
                id="site:test-01",
                name="Test Site",
                latitude_deg=50.0,
                longitude_deg=19.0,
                bortle_class=4,
                south_horizon_open=True,
                lp_artificial_brightness_mcd_m2=0.1785,
                lp_natural_sky_ratio=1.04,
                lp_estimated_total_brightness_mcd_m2=0.3497,
                lp_estimated_sqm_mag_arcsec2=21.22,
                lp_estimated_bortle_class=4,
                lp_dataset_name="New World Atlas",
                lp_provider_name="local-raster",
                lp_source="Falchi et al. 2016",
                lp_source_unit="mcd/m²",
                lp_data_kind="modeled",
                lp_updated_at="2026-09-20T19:00:00Z",
            )
        )

        self.assertTrue(created.south_horizon_open)
        self.assertEqual(created.lp_dataset_name, "New World Atlas")
        self.assertEqual(created.lp_estimated_bortle_class, 4)
        self.assertEqual(created.lp_updated_at, "2026-09-20T19:00:00Z")

        updated = self.repository.update_site(
            Site(
                id="site:test-01",
                name="Test Site Updated",
                latitude_deg=50.5,
                longitude_deg=19.5,
                sqm_mag_arcsec2=21.1,
                bortle_class=3,
                south_horizon_open=False,
                lp_artificial_brightness_mcd_m2=0.25,
                lp_natural_sky_ratio=1.46,
                lp_estimated_total_brightness_mcd_m2=0.4212,
                lp_estimated_sqm_mag_arcsec2=21.02,
                lp_estimated_bortle_class=4,
                lp_dataset_name="New World Atlas",
                lp_provider_name="local-raster",
                lp_source="Falchi et al. 2016",
                lp_source_unit="mcd/m²",
                lp_data_kind="modeled",
                lp_updated_at="2026-09-20T20:00:00Z",
                notes="Backup location",
            )
        )

        self.assertEqual(updated.name, "Test Site Updated")
        self.assertFalse(updated.south_horizon_open)
        self.assertEqual(updated.lp_artificial_brightness_mcd_m2, 0.25)
        self.assertEqual(updated.lp_estimated_sqm_mag_arcsec2, 21.02)
        self.assertEqual(updated.lp_updated_at, "2026-09-20T20:00:00Z")

        self.repository.delete_site("site:test-01")
        self.assertIsNone(self.repository.get_site("site:test-01"))

    def test_list_sites_returns_name_sorted_sites(self) -> None:
        self.repository.create_site(Site(id="site:zeta", name="Zeta Site"))
        self.repository.create_site(Site(id="site:alpha", name="Alpha Site"))

        listed = self.repository.list_sites()

        self.assertEqual([site.id for site in listed], ["site:alpha", "site:zeta"])

    def test_site_without_horizon_profile_loads_empty_profile(self) -> None:
        created = self.repository.create_site(Site(id="site:no-profile", name="No Profile"))

        self.assertEqual(created.horizon_profile, [])
        self.assertEqual(self.repository.get_site("site:no-profile").horizon_profile, [])

    def test_site_horizon_profile_is_saved_loaded_sorted_updated_and_cleared(self) -> None:
        created = self.repository.create_site(
            Site(
                id="site:horizon",
                name="Horizon Site",
                horizon_profile=[
                    LocalHorizonPoint(azimuth_deg=180, min_altitude_deg=40),
                    LocalHorizonPoint(azimuth_deg=0, min_altitude_deg=12),
                    LocalHorizonPoint(azimuth_deg=90, min_altitude_deg=25),
                ],
            )
        )

        self.assertEqual([point.azimuth_deg for point in created.horizon_profile], [0, 90, 180])
        self.assertEqual([point.min_altitude_deg for point in created.horizon_profile], [12, 25, 40])

        updated = self.repository.update_site(
            Site(
                id="site:horizon",
                name="Horizon Site Updated",
                horizon_profile=[
                    LocalHorizonPoint(azimuth_deg=315, min_altitude_deg=12),
                    LocalHorizonPoint(azimuth_deg=45, min_altitude_deg=18),
                ],
            )
        )

        self.assertEqual([point.azimuth_deg for point in updated.horizon_profile], [45, 315])

        cleared = self.repository.update_site(
            Site(
                id="site:horizon",
                name="Horizon Site Updated",
                horizon_profile=[],
            )
        )

        self.assertEqual(cleared.horizon_profile, [])

    def test_invalid_horizon_profile_points_are_rejected(self) -> None:
        invalid_profiles = [
            [LocalHorizonPoint(azimuth_deg=-1, min_altitude_deg=10)],
            [LocalHorizonPoint(azimuth_deg=360, min_altitude_deg=10)],
            [LocalHorizonPoint(azimuth_deg=90, min_altitude_deg=-1)],
            [LocalHorizonPoint(azimuth_deg=90, min_altitude_deg=91)],
            [
                LocalHorizonPoint(azimuth_deg=90, min_altitude_deg=10),
                LocalHorizonPoint(azimuth_deg=90, min_altitude_deg=20),
            ],
        ]

        for profile in invalid_profiles:
            with self.subTest(profile=profile):
                with self.assertRaises(ValidationError):
                    self.repository.create_site(
                        Site(
                            id=f"site:bad-{len(profile)}-{profile[0].azimuth_deg}",
                            name="Bad Horizon Site",
                            horizon_profile=profile,
                        )
                    )

    def test_equipment_crud_round_trip_with_json_properties(self) -> None:
        created = self.repository.create_equipment(
            Equipment(
                id="filter:l-extreme-001",
                equipment_type="filter",
                manufacturer="Optolong",
                model="L-eXtreme",
                properties={"bandpass_nm": 7, "narrowband": True},
                notes="Dual narrowband filter",
            )
        )

        self.assertEqual(created.properties["bandpass_nm"], 7)

        updated = self.repository.update_equipment(
            Equipment(
                id="filter:l-extreme-001",
                equipment_type="filter",
                manufacturer="Optolong",
                model="L-eXtreme",
                serial_number="SN-001",
                properties={"bandpass_nm": 7, "narrowband": True, "mounted": False},
                active=False,
                notes="Stored in case",
            )
        )

        self.assertFalse(updated.active)
        self.assertEqual(updated.properties["mounted"], False)

        self.repository.delete_equipment("filter:l-extreme-001")
        self.assertIsNone(self.repository.get_equipment("filter:l-extreme-001"))

    def test_save_acquisition_plan_persists_sequences_in_order(self) -> None:
        self.repository.create_target(
            Target(
                id="target:m42",
                catalog="MESSIER",
                catalog_id="M42",
                name="Orion Nebula",
                ra_deg=83.8221,
                dec_deg=-5.3911,
            )
        )
        self.repository.create_equipment(
            Equipment(
                id="filter:l-pro-001",
                equipment_type="filter",
                model="L-Pro",
                properties={},
            )
        )

        saved = self.repository.save_acquisition_plan(
            AcquisitionPlan(
                id="plan:m42:narrowband",
                target_id="target:m42",
                name="M42 Narrowband Plan",
                status="ready",
                sequences=[
                    AcquisitionSequence(
                        sequence_order=20,
                        name="Core",
                        frame_type="light",
                        exposure_s=15.0,
                        frame_count=40,
                        iso=400,
                        filter_id="filter:l-pro-001",
                    ),
                    AcquisitionSequence(
                        sequence_order=10,
                        name="Nebula",
                        frame_type="light",
                        exposure_s=60.0,
                        frame_count=120,
                        iso=800,
                        filter_id="filter:l-pro-001",
                    ),
                ],
            )
        )

        self.assertEqual([sequence.sequence_order for sequence in saved.sequences], [10, 20])
        self.assertTrue(all(sequence.id is not None for sequence in saved.sequences))

    def test_update_acquisition_plan_replaces_sequence_set(self) -> None:
        self.repository.create_target(
            Target(
                id="target:m45",
                catalog="MESSIER",
                catalog_id="M45",
                name="Pleiades",
                ra_deg=56.75,
                dec_deg=24.1167,
            )
        )

        self.repository.save_acquisition_plan(
            AcquisitionPlan(
                id="plan:m45:first-light",
                target_id="target:m45",
                name="M45 First Light",
                status="draft",
                sequences=[
                    AcquisitionSequence(sequence_order=10, frame_type="light", exposure_s=60.0, frame_count=30),
                    AcquisitionSequence(sequence_order=20, frame_type="light", exposure_s=30.0, frame_count=30),
                ],
            )
        )

        updated = self.repository.save_acquisition_plan(
            AcquisitionPlan(
                id="plan:m45:first-light",
                target_id="target:m45",
                name="M45 Final Plan",
                status="ready",
                sequences=[
                    AcquisitionSequence(sequence_order=5, frame_type="light", exposure_s=90.0, frame_count=24),
                ],
            )
        )

        self.assertEqual(updated.name, "M45 Final Plan")
        self.assertEqual(updated.status, "ready")
        self.assertEqual(len(updated.sequences), 1)
        self.assertEqual(updated.sequences[0].sequence_order, 5)

    def test_list_acquisition_plans_can_filter_by_target(self) -> None:
        self.repository.create_target(
            Target(id="target:m31", catalog="MESSIER", catalog_id="M31", name="Andromeda", ra_deg=10.6847, dec_deg=41.2692)
        )
        self.repository.create_target(
            Target(id="target:m33", catalog="MESSIER", catalog_id="M33", name="Triangulum", ra_deg=23.4621, dec_deg=30.6602)
        )

        self.repository.save_acquisition_plan(
            AcquisitionPlan(
                id="plan:m31",
                target_id="target:m31",
                name="M31 Plan",
                sequences=[AcquisitionSequence(sequence_order=10, frame_type="light", exposure_s=120.0, frame_count=20)],
            )
        )
        self.repository.save_acquisition_plan(
            AcquisitionPlan(
                id="plan:m33",
                target_id="target:m33",
                name="M33 Plan",
                sequences=[AcquisitionSequence(sequence_order=10, frame_type="light", exposure_s=180.0, frame_count=15)],
            )
        )

        filtered = self.repository.list_acquisition_plans(target_id="target:m31")

        self.assertEqual([plan.id for plan in filtered], ["plan:m31"])

    def test_delete_acquisition_plan_cascades_sequences(self) -> None:
        self.repository.create_target(
            Target(id="target:ic1805", catalog="IC", catalog_id="1805", name="Heart Nebula", ra_deg=38.5, dec_deg=61.5)
        )
        self.repository.save_acquisition_plan(
            AcquisitionPlan(
                id="plan:heart",
                target_id="target:ic1805",
                name="Heart Plan",
                sequences=[AcquisitionSequence(sequence_order=10, frame_type="light", exposure_s=300.0, frame_count=12)],
            )
        )

        self.repository.delete_acquisition_plan("plan:heart")

        plan = self.repository.get_acquisition_plan("plan:heart")
        sequence_count = self.connection.execute(
            "SELECT COUNT(*) FROM acquisition_sequences WHERE plan_id = ?;",
            ("plan:heart",),
        ).fetchone()[0]

        self.assertIsNone(plan)
        self.assertEqual(sequence_count, 0)

    def test_invalid_target_is_rejected_before_sqlite_write(self) -> None:
        with self.assertRaises(ValidationError):
            self.repository.create_target(
                Target(
                    id="target:bad",
                    catalog="TEST",
                    catalog_id="BAD",
                    name="Bad Target",
                    ra_deg=360.0,
                    dec_deg=0.0,
                )
            )

    def test_invalid_sequence_is_rejected_before_sqlite_write(self) -> None:
        self.repository.create_target(
            Target(id="target:test", catalog="TEST", catalog_id="T1", name="Test", ra_deg=1.0, dec_deg=2.0)
        )

        with self.assertRaises(ValidationError):
            self.repository.save_acquisition_plan(
                AcquisitionPlan(
                    id="plan:bad-sequence",
                    target_id="target:test",
                    name="Broken Plan",
                    sequences=[
                        AcquisitionSequence(
                            sequence_order=10,
                            frame_type="light",
                            exposure_s=60.0,
                            frame_count=0,
                        )
                    ],
                )
            )

    def test_plan_target_foreign_key_is_enforced(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.save_acquisition_plan(
                AcquisitionPlan(
                    id="plan:missing-target",
                    target_id="target:missing",
                    name="Missing Target Plan",
                    sequences=[
                        AcquisitionSequence(
                            sequence_order=10,
                            frame_type="light",
                            exposure_s=60.0,
                            frame_count=1,
                        )
                    ],
                )
            )


if __name__ == "__main__":
    unittest.main()
