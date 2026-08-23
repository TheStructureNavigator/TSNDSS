from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from tsn_dss.domain.models import (
    AcquisitionPlan,
    AcquisitionSequence,
    Dataset,
    Frame,
    Observation,
    ProcessingRun,
    Site,
    Target,
)
from tsn_dss.engine.projects import ProjectStorage
from tsn_dss.engine.siril import (
    SirilOutputNotFoundError,
    SirilProcessingService,
    SirilRunner,
    find_osc_preprocessing_result,
)
from tsn_dss.engine.sqlite.datasets import DatasetRepository
from tsn_dss.engine.sqlite.db import initialize_database
from tsn_dss.engine.sqlite.frames import FrameRepository
from tsn_dss.engine.sqlite.observation import ObservationRepository
from tsn_dss.engine.sqlite.planning import PlanningRepository
from tsn_dss.engine.sqlite.processing import ProcessingRunRepository


class ProjectStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)
        self.projects_root = self.temp_path / "projects"
        self.source_capture = self.temp_path / "OrionNebula"
        _create_raw_capture(self.source_capture)
        self.storage = ProjectStorage(self.projects_root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_import_capture_copies_raw_dataset_into_project_structure(self) -> None:
        destination = self.storage.import_capture(
            "orion_nebula",
            "OrionNebula",
            self.source_capture,
            move=False,
        )

        self.assertTrue(self.source_capture.exists())
        self.assertTrue((destination / "lights" / "light_001.CR2").exists())
        self.assertTrue((self.projects_root / "orion_nebula" / "captures").exists())

    def test_prepare_siril_run_creates_isolated_workspace(self) -> None:
        destination = self.storage.import_capture(
            "orion_nebula",
            "OrionNebula",
            self.source_capture,
            move=False,
        )

        run_layout = self.storage.prepare_siril_run(
            "orion_nebula",
            "processing:m42:siril:v001",
            frame_sources=[
                ("bias", destination / "biases" / "bias_001.CR2"),
                ("dark", destination / "darks" / "dark_001.CR2"),
                ("flat", destination / "flats" / "flat_001.CR2"),
                ("light", destination / "lights" / "light_001.CR2"),
            ],
        )

        self.assertTrue((run_layout.workspace_dir / "biases" / "bias_001.CR2").exists())
        self.assertTrue((run_layout.workspace_dir / "darks" / "dark_001.CR2").exists())
        self.assertTrue(run_layout.artifacts_dir.is_dir())
        self.assertTrue(run_layout.logs_dir.is_dir())

    def test_prepare_siril_run_uses_only_selected_dataset_frames(self) -> None:
        destination = self.storage.import_capture(
            "orion_nebula",
            "OrionNebula",
            self.source_capture,
            move=False,
        )

        run_layout = self.storage.prepare_siril_run(
            "orion_nebula",
            "processing:m42:siril:v002",
            frame_sources=[
                ("bias", destination / "biases" / "bias_001.CR2"),
                ("dark", destination / "darks" / "dark_001.CR2"),
                ("flat", destination / "flats" / "flat_001.CR2"),
                ("light", destination / "lights" / "light_001.CR2"),
            ],
        )

        light_files = sorted(path.name for path in (run_layout.workspace_dir / "lights").iterdir())
        self.assertEqual(light_files, ["light_001.CR2"])
        self.assertFalse((run_layout.workspace_dir / "lights" / "light_002.CR2").exists())

    def test_prepare_siril_run_falls_back_to_copy_when_linking_fails(self) -> None:
        destination = self.storage.import_capture(
            "orion_nebula",
            "OrionNebula",
            self.source_capture,
            move=False,
        )

        with patch("tsn_dss.engine.projects.os.link", side_effect=OSError("link blocked")):
            run_layout = self.storage.prepare_siril_run(
                "orion_nebula",
                "processing:m42:siril:v003",
                frame_sources=[
                    ("bias", destination / "biases" / "bias_001.CR2"),
                    ("dark", destination / "darks" / "dark_001.CR2"),
                    ("flat", destination / "flats" / "flat_001.CR2"),
                    ("light", destination / "lights" / "light_001.CR2"),
                ],
            )

        self.assertTrue((run_layout.workspace_dir / "lights" / "light_001.CR2").exists())


class SirilRunnerAndServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)

        self.projects_root = self.temp_path / "projects"
        self.storage = ProjectStorage(self.projects_root)
        self.source_capture = self.temp_path / "OrionNebula"
        _create_raw_capture(self.source_capture)
        self.capture_root = self.storage.import_capture("orion_nebula", "OrionNebula", self.source_capture, move=False)

        self.fake_siril_cli = self.temp_path / "fake_siril_cli.py"
        self.fake_siril_cli.write_text(
            textwrap.dedent(
                """
                import argparse
                import pathlib
                import sys

                parser = argparse.ArgumentParser(add_help=False)
                parser.add_argument("-d")
                parser.add_argument("-s")
                args = parser.parse_args()

                workdir = pathlib.Path(args.d)
                script = pathlib.Path(args.s)

                print(f"WORKDIR={workdir}")
                print(f"SCRIPT={script.name}")

                if not script.exists():
                    print("missing script", file=sys.stderr)
                    sys.exit(2)

                if "fail" in script.stem:
                    print("simulated failure", file=sys.stderr)
                    sys.exit(3)

                (workdir / "result_120s.fit").write_text("fake fits", encoding="utf-8")
                """
            ),
            encoding="utf-8",
        )

        self.fake_script = self.temp_path / "OSC_Preprocessing.ssf"
        self.fake_script.write_text("# fake script\n", encoding="utf-8")
        self.fake_fail_script = self.temp_path / "OSC_Preprocessing_fail.ssf"
        self.fake_fail_script.write_text("# fake fail script\n", encoding="utf-8")

        self.db_path = self.temp_path / "processing.db"
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

    def test_siril_runner_captures_stdout_stderr_and_exit_code(self) -> None:
        runner = SirilRunner((sys.executable, self.fake_siril_cli))
        run_layout = self.storage.prepare_siril_run(
            "orion_nebula",
            "processing:m42:siril:v-runner",
            frame_sources=[
                ("bias", self.capture_root / "biases" / "bias_001.CR2"),
                ("dark", self.capture_root / "darks" / "dark_001.CR2"),
                ("flat", self.capture_root / "flats" / "flat_001.CR2"),
                ("light", self.capture_root / "lights" / "light_001.CR2"),
            ],
        )

        result = runner.run_script(
            working_directory=run_layout.workspace_dir,
            script_path=self.fake_script,
        )

        self.assertEqual(result.exit_code, 0)
        self.assertIn("WORKDIR=", result.stdout)
        self.assertEqual(result.stderr, "")
        self.assertEqual(
            find_osc_preprocessing_result(run_layout.workspace_dir).name,
            "result_120s.fit",
        )

    def test_processing_service_updates_processing_run_and_artifacts_on_success(self) -> None:
        self.processing.create_processing_run(
            ProcessingRun(
                id="processing:m42:siril:v001",
                dataset_id="dataset:m42:first-light",
                version_label="v001",
                engine_name="siril-cli",
                pipeline=["OSC_Preprocessing"],
                parameters={},
                status="planned",
            )
        )

        service = SirilProcessingService(
            project_storage=self.storage,
            runner=SirilRunner((sys.executable, self.fake_siril_cli)),
            processing_runs=self.processing,
        )

        result = service.execute_osc_preprocessing(
            processing_run_id="processing:m42:siril:v001",
            project_slug="orion_nebula",
            capture_name="OrionNebula",
            script_path=self.fake_script,
        )

        updated = self.processing.get_processing_run("processing:m42:siril:v001")

        self.assertEqual(result.exit_code, 0)
        assert updated is not None
        self.assertEqual(updated.status, "completed")
        self.assertTrue(updated.final_image_path.endswith("result_120s.fit"))
        self.assertEqual(updated.linear_stack_path, updated.final_image_path)
        self.assertEqual(updated.parameters["dataset_id"], "dataset:m42:first-light")
        self.assertEqual(updated.parameters["project_slug"], "orion_nebula")
        self.assertEqual(updated.parameters["capture_name"], "OrionNebula")
        self.assertEqual(updated.parameters["exit_code"], 0)
        self.assertEqual(updated.parameters["script_path"], str(self.fake_script))
        self.assertGreaterEqual(updated.parameters["duration_seconds"], 0.0)
        self.assertTrue(Path(updated.parameters["stdout_log_path"]).exists())
        self.assertTrue(Path(updated.parameters["stderr_log_path"]).exists())
        self.assertTrue(Path(updated.final_image_path).exists())
        workspace_lights = sorted(path.name for path in (Path(updated.parameters["workspace_dir"]) / "lights").iterdir())
        self.assertEqual(workspace_lights, ["light_001.CR2"])

    def test_processing_service_marks_run_failed_when_siril_fails(self) -> None:
        self.processing.create_processing_run(
            ProcessingRun(
                id="processing:m42:siril:v002",
                dataset_id="dataset:m42:first-light",
                version_label="v002",
                engine_name="siril-cli",
                pipeline=["OSC_Preprocessing"],
                parameters={},
                status="planned",
            )
        )

        service = SirilProcessingService(
            project_storage=self.storage,
            runner=SirilRunner((sys.executable, self.fake_siril_cli)),
            processing_runs=self.processing,
        )

        result = service.execute_osc_preprocessing(
            processing_run_id="processing:m42:siril:v002",
            project_slug="orion_nebula",
            capture_name="OrionNebula",
            script_path=self.fake_fail_script,
        )

        updated = self.processing.get_processing_run("processing:m42:siril:v002")

        self.assertEqual(result.exit_code, 3)
        assert updated is not None
        self.assertEqual(updated.status, "failed")
        self.assertIsNone(updated.final_image_path)
        self.assertEqual(updated.parameters["exit_code"], 3)
        self.assertTrue(Path(updated.parameters["stderr_log_path"]).exists())

    def test_processing_service_raises_when_output_is_missing(self) -> None:
        no_output_cli = self.temp_path / "fake_siril_no_output.py"
        no_output_cli.write_text(
            textwrap.dedent(
                """
                import argparse
                parser = argparse.ArgumentParser(add_help=False)
                parser.add_argument("-d")
                parser.add_argument("-s")
                parser.parse_args()
                print("ok")
                """
            ),
            encoding="utf-8",
        )

        self.processing.create_processing_run(
            ProcessingRun(
                id="processing:m42:siril:v003",
                dataset_id="dataset:m42:first-light",
                version_label="v003",
                engine_name="siril-cli",
                pipeline=["OSC_Preprocessing"],
                parameters={},
                status="planned",
            )
        )

        service = SirilProcessingService(
            project_storage=self.storage,
            runner=SirilRunner((sys.executable, no_output_cli)),
            processing_runs=self.processing,
        )

        with self.assertRaises(SirilOutputNotFoundError):
            service.execute_osc_preprocessing(
                processing_run_id="processing:m42:siril:v003",
                project_slug="orion_nebula",
                capture_name="OrionNebula",
                script_path=self.fake_script,
            )

        updated = self.processing.get_processing_run("processing:m42:siril:v003")
        assert updated is not None
        self.assertEqual(updated.status, "failed")

    def _seed_domain_graph(self) -> None:
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
        self.planning.create_site(
            Site(
                id="site:field-01",
                name="Field Site",
                latitude_deg=50.0,
                longitude_deg=19.0,
                bortle_class=4,
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
                        sequence_order=5,
                        frame_type="bias",
                        frame_count=1,
                    ),
                    AcquisitionSequence(
                        sequence_order=6,
                        frame_type="dark",
                        exposure_s=30.0,
                        frame_count=1,
                    ),
                    AcquisitionSequence(
                        sequence_order=7,
                        frame_type="flat",
                        exposure_s=1.0,
                        frame_count=1,
                    ),
                    AcquisitionSequence(
                        sequence_order=10,
                        frame_type="light",
                        exposure_s=30.0,
                        frame_count=4,
                    ),
                ],
            )
        )
        plan = self.planning.get_acquisition_plan("plan:m42:first-light")
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
        bias_id = self.frames.create_frame(
            Frame(
                observation_id="obs:0001",
                sequence_id=plan.sequences[0].id,
                frame_type="bias",
                file_path=str(self.capture_root / "biases" / "bias_001.CR2"),
            )
        ).id
        dark_id = self.frames.create_frame(
            Frame(
                observation_id="obs:0001",
                sequence_id=plan.sequences[1].id,
                frame_type="dark",
                file_path=str(self.capture_root / "darks" / "dark_001.CR2"),
                exposure_s=30.0,
            )
        ).id
        flat_id = self.frames.create_frame(
            Frame(
                observation_id="obs:0001",
                sequence_id=plan.sequences[2].id,
                frame_type="flat",
                file_path=str(self.capture_root / "flats" / "flat_001.CR2"),
                exposure_s=1.0,
            )
        ).id
        light_id = self.frames.create_frame(
            Frame(
                observation_id="obs:0001",
                sequence_id=plan.sequences[3].id,
                frame_type="light",
                file_path=str(self.capture_root / "lights" / "light_001.CR2"),
                exposure_s=30.0,
                accepted=True,
            )
        ).id
        self.frames.create_frame(
            Frame(
                observation_id="obs:0001",
                sequence_id=plan.sequences[3].id,
                frame_type="light",
                file_path=str(self.capture_root / "lights" / "light_002.CR2"),
                exposure_s=30.0,
                accepted=True,
            )
        )
        self.datasets.create_dataset(
            Dataset(
                id="dataset:m42:first-light",
                target_id="target:m42",
                name="M42 First Light Dataset",
                status="complete",
                total_light_integration_s=30.0,
                observation_ids=["obs:0001"],
                frame_ids=[bias_id, dark_id, flat_id, light_id],
            )
        )


def _create_raw_capture(root: Path) -> None:
    files = {
        "biases/bias_001.CR2": "bias",
        "darks/dark_001.CR2": "dark",
        "flats/flat_001.CR2": "flat",
        "lights/light_001.CR2": "light",
        "lights/light_002.CR2": "extra light outside dataset",
    }
    for relative_path, content in files.items():
        destination = root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
