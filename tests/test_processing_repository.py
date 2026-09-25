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
    ProcessingRun,
    Site,
    Target,
)
from tsn_dss.engine.sqlite.datasets import DatasetRepository
from tsn_dss.engine.sqlite.db import initialize_database
from tsn_dss.engine.sqlite.frames import FrameRepository
from tsn_dss.engine.sqlite.observation import ObservationRepository
from tsn_dss.engine.sqlite.planning import PlanningRepository, ValidationError
from tsn_dss.engine.sqlite.processing import ProcessingRunRepository


class ProcessingRunRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "processing.db"
        self.connection = initialize_database(self.db_path)
        self.planning = PlanningRepository(self.connection)
        self.observations = ObservationRepository(self.connection)
        self.frames = FrameRepository(self.connection)
        self.datasets = DatasetRepository(self.connection)
        self.processing = ProcessingRunRepository(self.connection)
        self._seed_domain_graph()

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def test_create_processing_run_persists_pipeline_and_parameters(self) -> None:
        created = self.processing.create_processing_run(
            ProcessingRun(
                id="processing:m42:first-light:v001",
                dataset_id="dataset:m42:first-light",
                version_label="v001",
                engine_name="Siril",
                engine_version="1.2.0",
                pipeline=["calibration", "registration", "stacking"],
                parameters={"drizzle": False, "rejection": "winsorized"},
                status="planned",
                notes="Initial processing pass",
            )
        )

        self.assertEqual(created.pipeline, ["calibration", "registration", "stacking"])
        self.assertEqual(created.parameters["rejection"], "winsorized")

    def test_list_processing_runs_can_filter_by_dataset_and_status(self) -> None:
        self.processing.create_processing_run(
            ProcessingRun(
                id="processing:m42:first-light:v001",
                dataset_id="dataset:m42:first-light",
                version_label="v001",
                engine_name="Siril",
                status="planned",
            )
        )
        self.processing.create_processing_run(
            ProcessingRun(
                id="processing:m42:first-light:v002",
                dataset_id="dataset:m42:first-light",
                version_label="v002",
                engine_name="Siril",
                status="running",
            )
        )
        self.processing.create_processing_run(
            ProcessingRun(
                id="processing:m31:first-light:v001",
                dataset_id="dataset:m31:first-light",
                version_label="v001",
                engine_name="PixInsight",
                status="planned",
            )
        )

        filtered = self.processing.list_processing_runs(
            dataset_id="dataset:m42:first-light",
            status="planned",
        )

        self.assertEqual([run.id for run in filtered], ["processing:m42:first-light:v001"])

    def test_update_processing_run_updates_paths_and_notes(self) -> None:
        self.processing.create_processing_run(
            ProcessingRun(
                id="processing:m42:first-light:v001",
                dataset_id="dataset:m42:first-light",
                version_label="v001",
                engine_name="Siril",
                status="planned",
            )
        )

        updated = self.processing.update_processing_run(
            ProcessingRun(
                id="processing:m42:first-light:v001",
                dataset_id="dataset:m42:first-light",
                version_label="v001",
                engine_name="Siril",
                engine_version="1.2.0",
                pipeline=["stacking", "stretch"],
                parameters={"stretch": "asinh"},
                linear_stack_path="output/m42_linear.fit",
                preview_path="output/m42_preview.jpg",
                status="planned",
                notes="Prepared for run",
            )
        )

        self.assertEqual(updated.linear_stack_path, "output/m42_linear.fit")
        self.assertEqual(updated.parameters["stretch"], "asinh")

    def test_processing_status_lifecycle_to_completed_output(self) -> None:
        self.processing.create_processing_run(
            ProcessingRun(
                id="processing:m42:first-light:v001",
                dataset_id="dataset:m42:first-light",
                version_label="v001",
                engine_name="Siril",
                pipeline=["stacking", "stretch"],
                status="planned",
            )
        )

        running = self.processing.set_processing_run_status(
            "processing:m42:first-light:v001",
            "running",
            started_at="2026-08-21 23:10:00",
        )
        completed = self.processing.set_processing_run_status(
            "processing:m42:first-light:v001",
            "completed",
            finished_at="2026-08-21 23:25:00",
            final_image_path="output/m42_final.tif",
        )

        self.assertEqual(running.status, "running")
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.final_image_path, "output/m42_final.tif")

    def test_invalid_processing_status_transition_is_rejected(self) -> None:
        self.processing.create_processing_run(
            ProcessingRun(
                id="processing:m42:first-light:v001",
                dataset_id="dataset:m42:first-light",
                version_label="v001",
                engine_name="Siril",
                status="planned",
            )
        )

        with self.assertRaises(ValidationError):
            self.processing.set_processing_run_status(
                "processing:m42:first-light:v001",
                "completed",
                final_image_path="output/m42_final.tif",
            )

    def test_completed_processing_run_requires_final_image_path(self) -> None:
        with self.assertRaises(ValidationError):
            self.processing.create_processing_run(
                ProcessingRun(
                    id="processing:m42:first-light:v001",
                    dataset_id="dataset:m42:first-light",
                    version_label="v001",
                    engine_name="Siril",
                    status="completed",
                )
            )

    def test_missing_dataset_is_rejected(self) -> None:
        with self.assertRaises(Exception):
            self.processing.create_processing_run(
                ProcessingRun(
                    id="processing:missing:v001",
                    dataset_id="dataset:missing",
                    version_label="v001",
                    engine_name="Siril",
                )
            )

    def test_unique_dataset_version_is_enforced(self) -> None:
        self.processing.create_processing_run(
            ProcessingRun(
                id="processing:m42:first-light:v001",
                dataset_id="dataset:m42:first-light",
                version_label="v001",
                engine_name="Siril",
            )
        )

        with self.assertRaises(Exception):
            self.processing.create_processing_run(
                ProcessingRun(
                    id="processing:m42:first-light:v001b",
                    dataset_id="dataset:m42:first-light",
                    version_label="v001",
                    engine_name="Siril",
                )
            )

    def test_full_m42_workflow_reaches_output(self) -> None:
        run = self.processing.create_processing_run(
            ProcessingRun(
                id="processing:m42:first-light:v001",
                dataset_id="dataset:m42:first-light",
                version_label="v001",
                engine_name="Siril",
                pipeline=["calibration", "registration", "stacking", "stretch"],
                parameters={"background_extraction": True},
                status="planned",
            )
        )
        run = self.processing.set_processing_run_status(
            run.id,
            "running",
            started_at="2026-08-21 23:10:00",
        )
        run = self.processing.set_processing_run_status(
            run.id,
            "completed",
            finished_at="2026-08-21 23:25:00",
            final_image_path="output/m42_final.tif",
        )

        dataset = self.datasets.get_dataset("dataset:m42:first-light")

        self.assertEqual(dataset.total_light_integration_s, 35.0)
        self.assertEqual(run.final_image_path, "output/m42_final.tif")
        self.assertEqual(run.status, "completed")

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

        self.observations.create_observation(
            Observation(
                id="obs:0001",
                observation_number=1,
                target_id="target:m42",
                site_id="site:field-01",
                acquisition_plan_id="plan:m42:first-light",
                status="completed",
            )
        )
        self.observations.create_observation(
            Observation(
                id="obs:1001",
                observation_number=2,
                target_id="target:m31",
                site_id="site:field-01",
                acquisition_plan_id="plan:m31:first-light",
                status="completed",
            )
        )

        accepted_frame_30_id = self.frames.create_frame(
            Frame(
                project_id=self.project_id,
                capture_id=self.capture_id,
                observation_id="obs:0001",
                sequence_id=m42_plan.sequences[0].id,
                frame_type="light",
                rel_path="captures/Night1/data/m42/light_0030.fit",
                exposure_s=30.0,
                filter_id="filter:l-pro-001",
                accepted=True,
            )
        ).id
        accepted_frame_5_id = self.frames.create_frame(
            Frame(
                project_id=self.project_id,
                capture_id=self.capture_id,
                observation_id="obs:0001",
                sequence_id=m42_plan.sequences[1].id,
                frame_type="light",
                rel_path="captures/Night1/data/m42/light_0005.fit",
                exposure_s=5.0,
                filter_id="filter:l-pro-001",
                accepted=True,
            )
        ).id
        m31_frame_id = self.frames.create_frame(
            Frame(
                project_id=self.project_id,
                capture_id=self.capture_id,
                observation_id="obs:1001",
                sequence_id=m31_plan.sequences[0].id,
                frame_type="light",
                rel_path="captures/Night1/data/m31/light_0060.fit",
                exposure_s=60.0,
                accepted=True,
            )
        ).id

        self.datasets.create_dataset(
            Dataset(
                id="dataset:m42:first-light",
                target_id="target:m42",
                name="M42 First Light Dataset",
                status="complete",
                total_light_integration_s=35.0,
                observation_ids=["obs:0001"],
                frame_ids=[accepted_frame_30_id, accepted_frame_5_id],
            )
        )
        self.datasets.create_dataset(
            Dataset(
                id="dataset:m31:first-light",
                target_id="target:m31",
                name="M31 First Light Dataset",
                status="complete",
                total_light_integration_s=60.0,
                observation_ids=["obs:1001"],
                frame_ids=[m31_frame_id],
            )
        )


if __name__ == "__main__":
    unittest.main()
