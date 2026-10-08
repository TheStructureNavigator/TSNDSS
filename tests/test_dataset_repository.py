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
    Dataset,
    Equipment,
    Frame,
    Observation,
    Site,
    Target,
)
from tsn_dss.engine.sqlite.datasets import DatasetRepository
from tsn_dss.engine.sqlite.db import initialize_database
from tsn_dss.engine.sqlite.frames import FrameRepository
from tsn_dss.engine.sqlite.observation import ObservationRepository
from tsn_dss.engine.sqlite.planning import PlanningRepository, ValidationError


TEST_SESSION_ID = "session:test-observation"

class DatasetRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "datasets.db"
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
        self.datasets = DatasetRepository(self.connection)
        self._seed_domain_graph()

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def test_create_dataset_persists_observation_and_frame_membership(self) -> None:
        created = self.datasets.create_dataset(
            Dataset(
                id="dataset:m42:first-light",
                target_id="target:m42",
                name="M42 First Light Dataset",
                status="open",
                total_light_integration_s=35.0,
                observation_ids=["obs:0001"],
                frame_ids=[self.accepted_frame_30_id, self.accepted_frame_5_id],
            )
        )

        self.assertEqual(created.observation_ids, ["obs:0001"])
        self.assertEqual(
            created.frame_ids,
            sorted([self.accepted_frame_30_id, self.accepted_frame_5_id]),
        )

    def test_list_datasets_can_filter_by_target_and_status(self) -> None:
        self.datasets.create_dataset(
            Dataset(
                id="dataset:m42",
                target_id="target:m42",
                name="M42 Dataset",
                status="open",
                total_light_integration_s=35.0,
                observation_ids=["obs:0001"],
                frame_ids=[self.accepted_frame_30_id, self.accepted_frame_5_id],
            )
        )
        self.datasets.create_dataset(
            Dataset(
                id="dataset:m31",
                target_id="target:m31",
                name="M31 Dataset",
                status="complete",
                total_light_integration_s=60.0,
                observation_ids=["obs:1001"],
                frame_ids=[self.m31_frame_id],
            )
        )

        filtered = self.datasets.list_datasets(target_id="target:m42", status="open")

        self.assertEqual([item.id for item in filtered], ["dataset:m42"])

    def test_update_dataset_replaces_exact_frame_membership(self) -> None:
        self.datasets.create_dataset(
            Dataset(
                id="dataset:update",
                target_id="target:m42",
                name="Dataset Update",
                status="open",
                total_light_integration_s=35.0,
                observation_ids=["obs:0001"],
                frame_ids=[self.accepted_frame_30_id, self.accepted_frame_5_id],
            )
        )

        updated = self.datasets.update_dataset(
            Dataset(
                id="dataset:update",
                target_id="target:m42",
                name="Dataset Update Final",
                status="complete",
                total_light_integration_s=30.0,
                observation_ids=["obs:0001"],
                frame_ids=[self.accepted_frame_30_id],
            )
        )

        self.assertEqual(updated.name, "Dataset Update Final")
        self.assertEqual(updated.status, "complete")
        self.assertEqual(updated.frame_ids, [self.accepted_frame_30_id])

    def test_dataset_target_must_match_observation_target(self) -> None:
        with self.assertRaises(ValidationError):
            self.datasets.create_dataset(
                Dataset(
                    id="dataset:wrong-observation",
                    target_id="target:m42",
                    name="Wrong Observation Target",
                    total_light_integration_s=60.0,
                    observation_ids=["obs:1001"],
                    frame_ids=[self.m31_frame_id],
                )
            )

    def test_dataset_target_must_match_frame_observation_target(self) -> None:
        with self.assertRaises(ValidationError):
            self.datasets.create_dataset(
                Dataset(
                    id="dataset:wrong-frame",
                    target_id="target:m42",
                    name="Wrong Frame Target",
                    total_light_integration_s=60.0,
                    observation_ids=["obs:0001", "obs:1001"],
                    frame_ids=[self.m31_frame_id],
                )
            )

    def test_dataset_observations_must_cover_frame_observations(self) -> None:
        with self.assertRaises(ValidationError):
            self.datasets.create_dataset(
                Dataset(
                    id="dataset:missing-origin",
                    target_id="target:m42",
                    name="Missing Origin",
                    total_light_integration_s=35.0,
                    observation_ids=[],
                    frame_ids=[self.accepted_frame_30_id, self.accepted_frame_5_id],
                )
            )

    def test_dataset_integration_must_equal_accepted_light_membership(self) -> None:
        with self.assertRaises(ValidationError):
            self.datasets.create_dataset(
                Dataset(
                    id="dataset:bad-integration",
                    target_id="target:m42",
                    name="Bad Integration",
                    total_light_integration_s=45.0,
                    observation_ids=["obs:0001"],
                    frame_ids=[self.accepted_frame_30_id, self.accepted_frame_5_id],
                )
            )

    def test_dataset_ignores_rejected_and_unreviewed_frames_in_integration(self) -> None:
        created = self.datasets.create_dataset(
            Dataset(
                id="dataset:reviewed-only",
                target_id="target:m42",
                name="Reviewed Only",
                total_light_integration_s=35.0,
                observation_ids=["obs:0001"],
                frame_ids=[
                    self.accepted_frame_30_id,
                    self.accepted_frame_5_id,
                    self.rejected_frame_id,
                    self.unreviewed_frame_id,
                ],
            )
        )

        self.assertEqual(created.total_light_integration_s, 35.0)
        self.assertEqual(
            created.frame_ids,
            sorted(
                [
                    self.accepted_frame_30_id,
                    self.accepted_frame_5_id,
                    self.rejected_frame_id,
                    self.unreviewed_frame_id,
                ]
            ),
        )

    def test_delete_dataset_cascades_membership(self) -> None:
        self.datasets.create_dataset(
            Dataset(
                id="dataset:delete",
                target_id="target:m42",
                name="Delete Dataset",
                total_light_integration_s=35.0,
                observation_ids=["obs:0001"],
                frame_ids=[self.accepted_frame_30_id, self.accepted_frame_5_id],
            )
        )

        self.datasets.delete_dataset("dataset:delete")
        observation_links = self.connection.execute(
            "SELECT COUNT(*) FROM dataset_observations WHERE dataset_id = ?;",
            ("dataset:delete",),
        ).fetchone()[0]
        frame_links = self.connection.execute(
            "SELECT COUNT(*) FROM dataset_frames WHERE dataset_id = ?;",
            ("dataset:delete",),
        ).fetchone()[0]

        self.assertIsNone(self.datasets.get_dataset("dataset:delete"))
        self.assertEqual(observation_links, 0)
        self.assertEqual(frame_links, 0)

    def _seed_domain_graph(self) -> None:
        self.project_id, self.capture_id = seed_project_and_capture(self.connection)
        self.planning.create_target(
            Target(target_type="fixed_coordinate",
                id="target:m42",
                catalog="MESSIER",
                catalog_id="M42",
                name="Orion Nebula",
                ra_deg=83.8221,
                dec_deg=-5.3911,
            )
        )
        self.planning.create_target(
            Target(target_type="fixed_coordinate",
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
                        frame_type="light",
                        exposure_s=5.0,
                        frame_count=40,
                        filter_id="filter:l-pro-001",
                    ),
                ],
            )
        )
        self.planning.save_acquisition_plan(
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
        m31_plan = self.planning.get_acquisition_plan("plan:m31:first-light")
        self.m42_seq_30_id = m42_plan.sequences[0].id
        self.m42_seq_5_id = m42_plan.sequences[1].id
        self.m31_seq_60_id = m31_plan.sequences[0].id

        self.observations.create_observation(
            Observation(session_id=TEST_SESSION_ID,
                id="obs:0001",
                observation_number=1,
                target_id="target:m42",
                site_id="site:field-01",
                acquisition_plan_id="plan:m42:first-light",
                status="completed",
            )
        )
        self.observations.create_observation(
            Observation(session_id=TEST_SESSION_ID,
                id="obs:1001",
                observation_number=2,
                target_id="target:m31",
                site_id="site:field-01",
                acquisition_plan_id="plan:m31:first-light",
                status="completed",
            )
        )

        self.accepted_frame_30_id = self.frames.create_frame(
            Frame(
                project_id=self.project_id,
                capture_id=self.capture_id,
                observation_id="obs:0001",
                sequence_id=self.m42_seq_30_id,
                frame_type="light",
                rel_path="captures/Night1/data/m42/light_0030.fit",
                exposure_s=30.0,
                filter_id="filter:l-pro-001",
                accepted=True,
            )
        ).id
        self.accepted_frame_5_id = self.frames.create_frame(
            Frame(
                project_id=self.project_id,
                capture_id=self.capture_id,
                observation_id="obs:0001",
                sequence_id=self.m42_seq_5_id,
                frame_type="light",
                rel_path="captures/Night1/data/m42/light_0005.fit",
                exposure_s=5.0,
                filter_id="filter:l-pro-001",
                accepted=True,
            )
        ).id
        self.rejected_frame_id = self.frames.create_frame(
            Frame(
                project_id=self.project_id,
                capture_id=self.capture_id,
                observation_id="obs:0001",
                sequence_id=self.m42_seq_30_id,
                frame_type="light",
                rel_path="captures/Night1/data/m42/light_rejected.fit",
                exposure_s=30.0,
                filter_id="filter:l-pro-001",
                accepted=False,
                rejection_reason="Clouds",
            )
        ).id
        self.unreviewed_frame_id = self.frames.create_frame(
            Frame(
                project_id=self.project_id,
                capture_id=self.capture_id,
                observation_id="obs:0001",
                sequence_id=self.m42_seq_30_id,
                frame_type="light",
                rel_path="captures/Night1/data/m42/light_unreviewed.fit",
                exposure_s=30.0,
                filter_id="filter:l-pro-001",
            )
        ).id
        self.m31_frame_id = self.frames.create_frame(
            Frame(
                project_id=self.project_id,
                capture_id=self.capture_id,
                observation_id="obs:1001",
                sequence_id=self.m31_seq_60_id,
                frame_type="light",
                rel_path="captures/Night1/data/m31/light_0060.fit",
                exposure_s=60.0,
                accepted=True,
            )
        ).id


if __name__ == "__main__":
    unittest.main()
