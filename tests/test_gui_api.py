from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

from tsn_dss.engine.projects import ProjectStorage
from tsn_dss.gui.http_api import create_http_server


class ProjectStorageListingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.projects_root = Path(self.temp_dir.name) / "projects"
        self.storage = ProjectStorage(self.projects_root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_list_projects_returns_capture_and_run_counts(self) -> None:
        layout = self.storage.ensure_project("orion_nebula")
        (layout.captures_dir / "OrionNebula").mkdir(parents=True, exist_ok=True)
        (layout.runs_dir / "processing_m42_v001").mkdir(parents=True, exist_ok=True)

        projects = self.storage.list_projects()

        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0].slug, "orion_nebula")
        self.assertEqual(projects[0].capture_names, ("OrionNebula",))
        self.assertEqual(projects[0].run_names, ("processing_m42_v001",))

    def test_get_project_returns_exact_project(self) -> None:
        self.storage.ensure_project("orion_nebula")

        project = self.storage.get_project("orion_nebula")

        assert project is not None
        self.assertEqual(project.slug, "orion_nebula")

    def test_create_project_creates_directories(self) -> None:
        project = self.storage.create_project("m31_new")

        self.assertEqual(project.slug, "m31_new")
        self.assertTrue((self.projects_root / "m31_new" / "captures").is_dir())
        self.assertTrue((self.projects_root / "m31_new" / "runs").is_dir())

    def test_set_project_sky_target_persists_metadata(self) -> None:
        self.storage.create_project("m31_new")

        project = self.storage.set_project_sky_target("m31_new", "M31")

        self.assertEqual(project.sky_target, "M31")
        self.assertTrue((self.projects_root / "m31_new" / "project.json").exists())


class GuiApiServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.projects_root = Path(self.temp_dir.name) / "projects"
        self.source_capture = Path(self.temp_dir.name) / "OrionNebula"
        for relative_path in ("biases/bias_001.CR2", "darks/dark_001.CR2", "flats/flat_001.CR2", "lights/light_001.CR2"):
            destination = self.source_capture / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("x", encoding="utf-8")
        self.fake_script = Path(self.temp_dir.name) / "OSC_Preprocessing.ssf"
        self.fake_script.write_text("# fake script\n", encoding="utf-8")
        self.fake_siril_cli = Path(self.temp_dir.name) / "fake_siril_cli.py"
        self.fake_siril_cli.write_text(
            "\n".join(
                [
                    "import argparse",
                    "import pathlib",
                    "import sys",
                    "import time",
                    "parser = argparse.ArgumentParser(add_help=False)",
                    "parser.add_argument('-d')",
                    "parser.add_argument('-s')",
                    "args = parser.parse_args()",
                    "workdir = pathlib.Path(args.d)",
                    "print('Converting lights...', flush=True)",
                    "time.sleep(0.05)",
                    "print('Calibration step...', flush=True)",
                    "time.sleep(0.05)",
                    "print('Registration step...', flush=True)",
                    "time.sleep(0.05)",
                    "print('Stacking step...', flush=True)",
                    "time.sleep(0.05)",
                    "print('result_120s.fit written', flush=True)",
                    "(workdir / 'result_120s.fit').write_text('fake fits', encoding='utf-8')",
                    "sys.exit(0)",
                ]
            ),
            encoding="utf-8",
        )
        storage = ProjectStorage(self.projects_root)
        layout = storage.ensure_project("orion_nebula")
        self._copy_capture_tree(self.source_capture, layout.captures_dir / "OrionNebula")
        (layout.runs_dir / "processing_m42_v001").mkdir(parents=True, exist_ok=True)

        self.server = create_http_server(
            host="127.0.0.1",
            port=0,
            projects_root=self.projects_root,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp_dir.cleanup()

    def test_health_endpoint_reports_ok(self) -> None:
        payload = self._read_json("/api/health")

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["service"], "tsn-dss-api")
        self.assertEqual(payload["projects_root"], str(self.projects_root.resolve()))

    def test_projects_endpoint_lists_local_projects(self) -> None:
        payload = self._read_json("/api/projects")

        self.assertEqual(len(payload["projects"]), 1)
        project = payload["projects"][0]
        self.assertEqual(project["slug"], "orion_nebula")
        self.assertEqual(project["capture_count"], 1)
        self.assertEqual(project["run_count"], 1)
        self.assertEqual(project["capture_names"], ["OrionNebula"])
        self.assertEqual(project["run_names"], ["processing_m42_v001"])

    def test_project_detail_endpoint_returns_selected_project(self) -> None:
        payload = self._read_json("/api/projects/orion_nebula")

        project = payload["project"]
        self.assertEqual(project["slug"], "orion_nebula")
        self.assertEqual(project["captures_dir"], str((self.projects_root / "orion_nebula" / "captures")))
        self.assertEqual(project["runs_dir"], str((self.projects_root / "orion_nebula" / "runs")))
        self.assertIsNone(project["sky_target"])

    def test_create_project_endpoint_creates_new_project(self) -> None:
        payload = self._send_json("/api/projects", {"slug": "m42_gui"})

        project = payload["project"]
        self.assertEqual(project["slug"], "m42_gui")
        self.assertTrue((self.projects_root / "m42_gui" / "captures").is_dir())
        self.assertTrue((self.projects_root / "m42_gui" / "runs").is_dir())

    def test_import_capture_endpoint_copies_capture_into_project(self) -> None:
        self._send_json("/api/projects", {"slug": "m42_gui"})

        payload = self._send_json(
            "/api/import-capture",
            {
                "project_slug": "m42_gui",
                "capture_name": "OrionNebula",
                "source_dir": str(self.source_capture),
                "move": False,
            },
        )

        project = payload["project"]
        self.assertEqual(project["slug"], "m42_gui")
        self.assertTrue(
            (self.projects_root / "m42_gui" / "captures" / "OrionNebula" / "lights" / "light_001.CR2").exists()
        )
        self.assertTrue(self.source_capture.exists())

    def test_project_run_endpoint_executes_and_reports_logs(self) -> None:
        payload = self._send_json(
            "/api/project-runs",
            {
                "project_slug": "orion_nebula",
                "capture_name": "OrionNebula",
                "script_path": str(self.fake_script),
                "executable_parts": [sys.executable, str(self.fake_siril_cli)],
            },
        )

        run_id = payload["run"]["id"]
        terminal_payload = self._wait_for_run(run_id)
        run = terminal_payload["run"]

        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["progress_pct"], 100)
        self.assertIn("Stacking step", run["combined_log"])
        self.assertTrue(Path(run["output_path"]).exists())
        self.assertTrue(Path(run["status_path"]).exists())
        self.assertTrue(Path(run["stdout_log_path"]).exists())

    def test_project_runs_can_be_listed(self) -> None:
        self._send_json(
            "/api/project-runs",
            {
                "project_slug": "orion_nebula",
                "capture_name": "OrionNebula",
                "script_path": str(self.fake_script),
                "executable_parts": [sys.executable, str(self.fake_siril_cli)],
            },
        )

        payload = self._read_json("/api/project-runs?project_slug=orion_nebula")

        self.assertGreaterEqual(len(payload["runs"]), 1)
        self.assertEqual(payload["runs"][0]["project_slug"], "orion_nebula")

    def test_project_run_output_endpoint_serves_fits_artifact(self) -> None:
        payload = self._send_json(
            "/api/project-runs",
            {
                "project_slug": "orion_nebula",
                "capture_name": "OrionNebula",
                "script_path": str(self.fake_script),
                "executable_parts": [sys.executable, str(self.fake_siril_cli)],
            },
        )

        run_id = payload["run"]["id"]
        self._wait_for_run(run_id)

        response = self._read_response(f"/api/project-runs/{run_id}/output")

        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers.get_content_type(), "application/octet-stream")
        self.assertEqual(response.read(), b"fake fits")

    def test_generate_preview_endpoint_returns_preview_error_and_log(self) -> None:
        payload = self._send_json(
            "/api/project-runs",
            {
                "project_slug": "orion_nebula",
                "capture_name": "OrionNebula",
                "script_path": str(self.fake_script),
                "executable_parts": [sys.executable, str(self.fake_siril_cli)],
            },
        )

        run_id = payload["run"]["id"]
        self._wait_for_run(run_id)

        retry_payload = self._send_json(f"/api/project-runs/{run_id}/generate-preview", {})
        run = retry_payload["run"]

        self.assertIsNone(run["preview_path"])
        self.assertIn("not Siril", run["preview_error"])
        self.assertTrue(Path(run["preview_log_path"]).exists())

        response = self._read_response(f"/api/project-runs/{run_id}/preview-log")
        self.assertEqual(response.status, 200)
        self.assertIn("Preview export skipped", response.read().decode("utf-8"))

    def test_completed_runs_are_restored_after_server_restart(self) -> None:
        payload = self._send_json(
            "/api/project-runs",
            {
                "project_slug": "orion_nebula",
                "capture_name": "OrionNebula",
                "script_path": str(self.fake_script),
                "executable_parts": [sys.executable, str(self.fake_siril_cli)],
            },
        )

        run_id = payload["run"]["id"]
        self._wait_for_run(run_id)

        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

        self.server = create_http_server(
            host="127.0.0.1",
            port=0,
            projects_root=self.projects_root,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

        restored_payload = self._read_json("/api/project-runs?project_slug=orion_nebula")
        restored_runs = restored_payload["runs"]
        self.assertEqual(restored_runs[0]["id"], run_id)
        self.assertEqual(restored_runs[0]["status"], "completed")

        retry_payload = self._send_json(f"/api/project-runs/{run_id}/generate-preview", {})
        self.assertEqual(retry_payload["run"]["id"], run_id)

    def test_project_sky_target_can_be_updated_via_api(self) -> None:
        payload = self._send_json(
            "/api/projects/orion_nebula/sky-target",
            {"sky_target": "M42"},
        )

        project = payload["project"]
        self.assertEqual(project["sky_target"], "M42")

        detail = self._read_json("/api/projects/orion_nebula")
        self.assertEqual(detail["project"]["sky_target"], "M42")

    def _read_json(self, path: str) -> dict[str, object]:
        with self._read_response(path) as response:
            return json.loads(response.read().decode("utf-8"))

    def _read_response(self, path: str):
        return urlopen(f"{self.base_url}{path}")

    def _send_json(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))

    def _wait_for_run(self, run_id: str, timeout_s: float = 5.0) -> dict[str, object]:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            payload = self._read_json(f"/api/project-runs/{run_id}")
            if payload["run"]["status"] in {"completed", "failed"}:
                return payload
            time.sleep(0.05)
        self.fail(f"Run did not finish before timeout: {run_id}")

    def _copy_capture_tree(self, source_root: Path, destination_root: Path) -> None:
        for source in source_root.rglob("*"):
            if not source.is_file():
                continue
            relative = source.relative_to(source_root)
            destination = destination_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())


if __name__ == "__main__":
    unittest.main()
