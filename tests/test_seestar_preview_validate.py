"""DB-03 Wave 5 (preparation): the attended validation script, offline. A fake DB-02 runtime and fake-cv2 workers; no device, no network,
no OpenCV. The script itself is never run against hardware here."""

from __future__ import annotations

import ast
import contextlib
import io
import importlib.util
import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests.seestar_support import HOST
from tests.test_isolated_integration import IntegrationCase, Rig

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "seestar_preview_validate.py"
spec = importlib.util.spec_from_file_location("seestar_preview_validate", SCRIPT)
V = importlib.util.module_from_spec(spec)
sys.modules["seestar_preview_validate"] = V
spec.loader.exec_module(V)


class ValidatePreviewTests(IntegrationCase):
    def run_it(self, rig, cameras, frames=3):
        return V.validate_preview(rig.manager, cameras, frames, 60.0)

    def row(self, report, camera):
        return next(r for r in report["cameras"] if r["camera"] == camera)

    def test_both_cameras_pass_and_the_report_has_only_counts_formats_and_times(self) -> None:
        rig = self.rig("ok_varying", "ok_bgr")
        report = self.run_it(rig, ("main", "wide"))
        self.assertEqual(report["overall"]["verdict"], "PASS")
        self.assertEqual(report["overall"]["workers_during_run"], 2)
        self.assertEqual(report["overall"]["cleanup"], {"closed": ["main", "wide"], "close_errors": [], "live_workers": 0, "abandoned_workers": 0})
        main = self.row(report, "main")
        self.assertEqual((main["verdict"], main["gate_allowed"], main["opened"], main["frames"], main["format"], main["width"], main["height"]),
                         ("PASS", True, True, 3, "bgr8", 4, 2))
        self.assertEqual(main["distinct_content"], 3)                       # the fake stream changes; a frozen stream would show 1
        self.assertGreater(main["open_s"], 0)
        self.assertEqual(set(main["poll_s"]), {"min", "median", "max"})
        self.assertEqual(self.row(report, "wide")["frames"], 3)

    def test_main_alone_and_wide_alone(self) -> None:
        for camera in ("main", "wide"):
            with self.subTest(camera):
                rig = self.rig("ok_bgr", "ok_bgr")
                report = self.run_it(rig, (camera,))
                self.assertEqual([r["camera"] for r in report["cameras"]], [camera])
                self.assertEqual((report["overall"]["verdict"], report["overall"]["workers_during_run"]), ("PASS", 1))
                self.assertEqual(len(rig.made), 1)                          # the other camera was never touched

    def test_a_refused_gate_is_reported_and_nothing_is_opened(self) -> None:
        rig = self.rig("ok_bgr", "ok_bgr")
        rig.flow.set_camera_ready("main", False)
        report = self.run_it(rig, ("main",))
        row = self.row(report, "main")
        self.assertEqual((row["gate_allowed"], row["opened"], row["frames"], row["verdict"]), (False, False, 0, "FAIL"))
        self.assertIsNotNone(row["gate_reason"])
        self.assertEqual(row["fail_reasons"], ["gate_denied"])
        self.assertEqual((rig.made, report["overall"]["verdict"]), ([], "FAIL"))

    def test_open_failure_lost_stream_and_a_good_camera_are_told_apart(self) -> None:
        rig = self.rig("open_refused", "ok_bgr")
        report = self.run_it(rig, ("main", "wide"))
        main, wide = self.row(report, "main"), self.row(report, "wide")
        self.assertEqual((main["fail_reasons"], main["error_category"]), (["open_failed"], "open_failed"))
        self.assertEqual(wide["verdict"], "PASS")
        self.assertEqual(report["overall"]["verdict"], "FAIL")
        rig = self.rig("read_exhausted", "ok_bgr")
        report = self.run_it(rig, ("main", "wide"))
        main = self.row(report, "main")
        self.assertEqual((main["final_state"], main["error_category"], main["verdict"]), ("lost", "read_failed", "FAIL"))
        self.assertIn("stream_lost", main["fail_reasons"])
        self.assertEqual((self.row(report, "wide")["verdict"], report["overall"]["cleanup"]["live_workers"]), ("PASS", 0))

    def test_too_few_frames_is_a_failure_even_when_the_stream_is_not_lost(self) -> None:
        rig = self.rig("ok_bgr", "ok_bgr")
        report = V.validate_preview(rig.manager, ("main",), 3, 0.0)           # the budget is used up before the first read
        row = self.row(report, "main")
        self.assertEqual((row["opened"], row["frames"], row["final_state"], row["fail_reasons"], row["verdict"]),
                         (True, 0, None, ["too_few_frames"], "FAIL"))

    def test_a_worker_left_after_cleanup_fails_the_run(self) -> None:
        rig = self.rig("ok_bgr", "ok_bgr")
        with mock.patch.object(V, "abandoned_worker_count", return_value=1):
            report = self.run_it(rig, ("main",))
        self.assertEqual((self.row(report, "main")["verdict"], report["overall"]["verdict"], report["overall"]["fail_reasons"]),
                         ("PASS", "FAIL", ["cleanup_not_clean"]))

    def test_the_report_carries_no_address_host_or_pixel_data(self) -> None:
        rig = self.rig("ok_varying", "ok_bgr")
        text = json.dumps(self.run_it(rig, ("main", "wide")))
        self.assertNotIn(HOST, text)
        self.assertNotIn("rtsp", text.lower())
        self.assertIsNone(re.search(r"[0-9a-f]{32,}", text))                # no digest, no segment name, no key material
        self.assertEqual(set(self.row(json.loads(text), "main")),
                         {"camera", "gate_allowed", "gate_reason", "opened", "open_refusal", "error_category", "frames", "format", "width", "height",
                          "bytes", "freshness", "distinct_content", "open_s", "poll_s", "final_state", "fail_reasons", "verdict"})


class PrerequisiteAndStaticTests(unittest.TestCase):
    def test_missing_prerequisites_exit_2_before_anything_is_contacted(self) -> None:
        with mock.patch.object(V, "connect_real", side_effect=AssertionError("contacted")), mock.patch.dict("os.environ", {}, clear=False):
            for argv in (["--host", "192.0.2.1", "--camera", "main"], ["--host", "192.0.2.1", "--camera", "both", "--frames", "0"],
                         ["--host", "192.0.2.1", "--camera", "wide", "--frames", "31"]):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(V.main(argv), 2, argv)
                self.assertIn("[PREREQUISITE]", out.getvalue())

    def test_the_script_has_no_control_command_no_image_write_and_no_opencv_import(self) -> None:
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        for node in ast.walk(tree):                                          # strip docstrings: the docstring says what it never does
            if isinstance(node, (ast.Module, ast.FunctionDef)) and node.body and isinstance(node.body[0], ast.Expr) and isinstance(
                    getattr(node.body[0], "value", None), ast.Constant):
                node.body = node.body[1:] or [ast.Pass()]
        code = ast.unparse(tree)
        for banned in ("start_view", "iscope", "stop_view", "fresh_pixels", "latest_pixels", "pixels_for", ".data", "import cv2", "numpy", "socket", "pickle"):
            self.assertNotIn(banned, code, banned)
        self.assertEqual(code.count("write_text("), 1)                       # the report is the only file written
        self.assertNotIn("print(host", code)
        self.assertIn("build_isolated_preview_manager(", code)               # it goes through the composition root, nothing else


if __name__ == "__main__":
    unittest.main()
