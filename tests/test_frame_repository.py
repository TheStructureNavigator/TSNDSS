from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

try:
    from domain_seed import seed_project_and_capture
except ModuleNotFoundError:
    from tests.domain_seed import seed_project_and_capture
from tsn_dss.domain.models import (
    AcquisitionPlan,
    AcquisitionSequence,
    Equipment,
    Frame,
    Observation,
    Site,
    Target,
)
from tsn_dss.engine.sqlite.db import initialize_database
from tsn_dss.engine.sqlite.frames import FrameRepository
from tsn_dss.engine.sqlite.observation import ObservationRepository
from tsn_dss.engine.sqlite.planning import PlanningRepository, ValidationError


TEST_SESSION_ID = "session:test-observation"

class FrameRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "frames.db"
        self.connection = initialize_database(self.db_path)
        self.connection.execute(
            """
            INSERT INTO sessions (id, title, state, started_at, notes)
            VALUES (?, 'Test Observation Session', 'planned', '2026-08-21T20:00:00+00:00', 'Test fixture Session.')
            ON CONFLICT(id) DO NOTHING;
            """,
            (TEST_SESSION_ID,),
        )
        self.connection.commit()
        self.planning = PlanningRepository(self.connection)
        self.observations = ObservationRepository(self.connection)
        self.frames = FrameRepository(self.connection)
        self._seed_domain_graph()

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def test_create_frame_with_sequence_and_filter(self) -> None:
        created = self.frames.create_frame(
            Frame(
                project_id=self.project_id,
                capture_id=self.capture_id,
                observation_id="obs:0001",
                sequence_id=self.sequence_light_id,
                frame_type="light",
                rel_path="captures/Night1/data/m42/light_0001.fit",
                captured_at="2026-08-21 22:10:00",
                exposure_s=30.0,
                iso=800,
                filter_id="filter:l-pro-001",
                metadata={"format": "fits"},
            )
        )

        self.assertIsNotNone(created.id)
        self.assertEqual(created.filter_id, "filter:l-pro-001")
        self.assertEqual(created.accepted, None)

    def test_list_frames_can_filter_by_type_and_review_state(self) -> None:
        first = self.frames.create_frame(
            Frame(
                project_id=self.project_id,
                capture_id=self.capture_id,
                observation_id="obs:0001",
                sequence_id=self.sequence_light_id,
                frame_type="light",
                rel_path="captures/Night1/data/m42/light_0001.fit",
                exposure_s=30.0,
            )
        )
        second = self.frames.create_frame(
            Frame(
                project_id=self.project_id,
                capture_id=self.capture_id,
                observation_id="obs:0001",
                sequence_id=self.sequence_dark_id,
                frame_type="dark",
                rel_path="captures/Night1/data/m42/dark_0001.fit",
                exposure_s=30.0,
            )
        )
        self.frames.review_frame(first.id, accepted=True)

        accepted_lights = self.frames.list_frames(
            observation_id="obs:0001",
            frame_type="light",
            accepted=True,
        )

        self.assertEqual([item.id for item in accepted_lights], [first.id])
        self.assertNotEqual(second.id, first.id)

    def test_review_frame_accepts_and_rejects(self) -> None:
        created = self.frames.create_frame(
            Frame(
                project_id=self.project_id,
                capture_id=self.capture_id,
                observation_id="obs:0001",
                sequence_id=self.sequence_light_id,
                frame_type="light",
                rel_path="captures/Night1/data/m42/light_review.fit",
                exposure_s=30.0,
            )
        )

        accepted = self.frames.review_frame(created.id, accepted=True)
        rejected = self.frames.review_frame(
            created.id,
            accepted=False,
            rejection_reason="Trailing stars",
        )

        self.assertTrue(accepted.accepted)
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.rejection_reason, "Trailing stars")

    def test_update_frame_quality_metrics_round_trip(self) -> None:
        created = self.frames.create_frame(
            Frame(
                project_id=self.project_id,
                capture_id=self.capture_id,
                observation_id="obs:0001",
                sequence_id=self.sequence_light_id,
                frame_type="light",
                rel_path="captures/Night1/data/m42/light_metrics.fit",
                exposure_s=30.0,
            )
        )

        created.fwhm_arcsec = 2.1
        created.eccentricity = 0.42
        created.star_count = 154
        created.guiding_rms_arcsec = 0.78
        updated = self.frames.update_frame(created)

        self.assertEqual(updated.fwhm_arcsec, 2.1)
        self.assertEqual(updated.star_count, 154)

    def test_sequence_must_belong_to_observation_plan(self) -> None:
        with self.assertRaises(ValidationError):
            self.frames.create_frame(
                Frame(
                    project_id=self.project_id,
                    capture_id=self.capture_id,
                    observation_id="obs:0001",
                    sequence_id=self.sequence_other_plan_id,
                    frame_type="light",
                    rel_path="captures/Night1/data/m42/wrong_sequence.fit",
                    exposure_s=30.0,
                )
            )

    def test_frame_type_must_match_sequence_type(self) -> None:
        with self.assertRaises(ValidationError):
            self.frames.create_frame(
                Frame(
                    project_id=self.project_id,
                    capture_id=self.capture_id,
                    observation_id="obs:0001",
                    sequence_id=self.sequence_dark_id,
                    frame_type="light",
                    rel_path="captures/Night1/data/m42/type_mismatch.fit",
                    exposure_s=30.0,
                )
            )

    def test_filter_id_must_reference_filter_equipment(self) -> None:
        with self.assertRaises(ValidationError):
            self.frames.create_frame(
                Frame(
                    project_id=self.project_id,
                    capture_id=self.capture_id,
                    observation_id="obs:0001",
                    sequence_id=self.sequence_light_id,
                    frame_type="light",
                    rel_path="captures/Night1/data/m42/bad_filter.fit",
                    exposure_s=30.0,
                    filter_id="camera:canon600d-001",
                )
            )

    def test_filter_id_must_match_sequence_filter_when_defined(self) -> None:
        self.planning.create_equipment(
            Equipment(
                id="filter:ha-001",
                equipment_type="filter",
                manufacturer="Optolong",
                model="H-alpha",
                properties={"bandpass_nm": 7},
            )
        )

        with self.assertRaises(ValidationError):
            self.frames.create_frame(
                Frame(
                    project_id=self.project_id,
                    capture_id=self.capture_id,
                    observation_id="obs:0001",
                    sequence_id=self.sequence_light_id,
                    frame_type="light",
                    rel_path="captures/Night1/data/m42/filter_mismatch.fit",
                    exposure_s=30.0,
                    filter_id="filter:ha-001",
                )
            )

    def test_sequence_requires_observation_plan(self) -> None:
        self.observations.create_observation(
            Observation(session_id=TEST_SESSION_ID,
                id="obs:no-plan",
                observation_number=2,
                target_id="target:m42",
                status="planned",
            )
        )

        with self.assertRaises(ValidationError):
            self.frames.create_frame(
                Frame(
                    project_id=self.project_id,
                    capture_id=self.capture_id,
                    observation_id="obs:no-plan",
                    sequence_id=self.sequence_light_id,
                    frame_type="light",
                    rel_path="captures/Night1/data/m42/no_plan.fit",
                    exposure_s=30.0,
                )
            )

    def test_delete_frame_removes_it(self) -> None:
        created = self.frames.create_frame(
            Frame(
                project_id=self.project_id,
                capture_id=self.capture_id,
                observation_id="obs:0001",
                sequence_id=self.sequence_light_id,
                frame_type="light",
                rel_path="captures/Night1/data/m42/delete_me.fit",
                exposure_s=30.0,
            )
        )

        self.frames.delete_frame(created.id)

        self.assertIsNone(self.frames.get_frame(created.id))

    def _seed_domain_graph(self) -> None:
        self.project_id, self.capture_id = seed_project_and_capture(self.connection)
        self.planning.create_target(
            Target(
                id="target:m42",
                catalog="MESSIER",
                catalog_id="M42",
                name="Orion Nebula",
                ra_deg=83.8221,
                dec_deg=-5.3911,
            )
        )
        self.planning.create_target(
            Target(
                id="target:m31",
                catalog="MESSIER",
                catalog_id="M31",
                name="Andromeda Galaxy",
                ra_deg=10.6847,
                dec_deg=41.2692,
            )
        )
        self.planning.create_site(
            Site(
                id="site:field-01",
                name="Field Site",
                latitude_deg=50.0,
                longitude_deg=19.0,
                bortle_class=4,
            )
        )
        self.planning.create_equipment(
            Equipment(
                id="camera:canon600d-001",
                equipment_type="camera",
                manufacturer="Canon",
                model="EOS 600D",
                properties={"sensor_type": "APS-C CMOS"},
            )
        )
        self.planning.create_equipment(
            Equipment(
                id="filter:l-pro-001",
                equipment_type="filter",
                manufacturer="Optolong",
                model="L-Pro",
                properties={"broadband": True},
            )
        )
        self.planning.save_acquisition_plan(
            AcquisitionPlan(
                id="plan:m42:first-light",
                target_id="target:m42",
                name="M42 First Light",
                status="ready",
                sequences=[
                    AcquisitionSequence(
                        sequence_order=10,
                        frame_type="light",
                        exposure_s=30.0,
                        frame_count=120,
                        filter_id="filter:l-pro-001",
                    ),
                    AcquisitionSequence(
                        sequence_order=20,
                        frame_type="dark",
                        exposure_s=30.0,
                        frame_count=30,
                    ),
                ],
            )
        )
        other_plan = self.planning.save_acquisition_plan(
            AcquisitionPlan(
                id="plan:m31:first-light",
                target_id="target:m31",
                name="M31 First Light",
                status="ready",
                sequences=[
                    AcquisitionSequence(
                        sequence_order=10,
                        frame_type="light",
                        exposure_s=60.0,
                        frame_count=20,
                    )
                ],
            )
        )
        m42_plan = self.planning.get_acquisition_plan("plan:m42:first-light")
        self.sequence_light_id = m42_plan.sequences[0].id
        self.sequence_dark_id = m42_plan.sequences[1].id
        self.sequence_other_plan_id = other_plan.sequences[0].id

        self.observations.create_observation(
            Observation(session_id=TEST_SESSION_ID,
                id="obs:0001",
                observation_number=1,
                target_id="target:m42",
                site_id="site:field-01",
                acquisition_plan_id="plan:m42:first-light",
                status="running",
            )
        )


if __name__ == "__main__":
    unittest.main()
