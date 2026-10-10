"""DB-03 Wave 4B-3c: Wave 3 flow (DB-02 evidence -> gate -> manager) with isolated decoder workers behind a fake ``cv2``.

Real worker processes running the real 4B-3a handler and Wave 4 adapter against the fake module of ``tests/decoder_worker_fixture.py``.
No OpenCV, no NumPy, no network, no device. Existing Wave 3/4 suites cover the manager, gate and adapter themselves; this file covers
only the composition: decoder selection, MAIN/WIDE, the mandatory ``before_connect``, cleanup and the parent's imports.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.seestar_preview_support import POLICY, Flow
from tests.test_isolated_slot import ADDRESS, shm_names
from tsn_dss.engine.device_runtime import SequentialIdGenerator
from tsn_dss.engine.device_runtime.preview_manager import OpenRefusal as R
from tsn_dss.engine.device_runtime.preview_models import ImageFreshness as F
from tsn_dss.engine.device_runtime.preview_models import ImageLimits, PixelFormat
from tsn_dss.engine.device_runtime.preview_readiness import GateReason as G
from tsn_dss.engine.device_runtime.preview_stream import PreviewStreamState as S
from tsn_dss.engine.opencv_isolated_decoder import (
    IsolatedDecoderConfig, WorkerConfig, WorkerProcess, build_isolated_preview_manager, live_worker_count,
)
from tsn_dss.engine.opencv_isolated_decoder import process as process_module

ROOT = Path(__file__).resolve().parents[1]
LIMITS = ImageLimits(max_width=64, max_height=64, max_image_bytes=4096, max_buffer_bytes=4096)
WORKER = WorkerConfig(start_deadline_s=3.0, ping_deadline_s=0.5, close_graceful_s=0.3, terminate_wait_s=0.5, kill_wait_s=0.5,
                      open_margin_s=0.3, read_margin_s=0.3)


class Rig:
    """A Wave 3 Flow whose manager is the isolated one. Workers are made from ``fixtures`` in the order the manager opens streams."""

    def __init__(self, *fixtures: str, on_worker=None, python_version=None) -> None:
        self.flow = Flow()
        self.fixtures, self.made, self.configs, self.on_worker = list(fixtures), [], [], on_worker
        self.report_dir = Path(tempfile.mkdtemp())
        self.isolated = IsolatedDecoderConfig(open_timeout_ms=1000, read_timeout_ms=1000, limits=LIMITS, worker=WORKER)
        self.calls_before = len(self.flow.transport.calls)
        self.manager = build_isolated_preview_manager(
            runtime=self.flow.runtime, connection=self.flow.connection, config=self.flow.config, isolated=self.isolated,
            clock=self.flow.clock, id_generator=SequentialIdGenerator(), policy=POLICY,
            _worker_factory=self._make_worker, _python_version=python_version,
        )
        self.calls_after_build = len(self.flow.transport.calls)

    def _make_worker(self, config, **kw):
        if self.on_worker:
            self.on_worker(len(self.made))
        index = len(self.made)
        name = self.fixtures[index]
        self.configs.append(config)
        worker = WorkerProcess(config, _entry_module="tests.decoder_worker_fixture", _extra_args=(
            "--fixture", name, "--report", str(self.report_dir / f"w{index}.json")), **kw)
        self.made.append(worker)
        return worker

    def report(self, index: int) -> dict:
        return json.loads((self.report_dir / f"w{index}.json").read_text())

    def open_both(self):
        return self.manager.open_stream("main"), self.manager.open_stream("wide")


class IntegrationCase(unittest.TestCase):
    def setUp(self) -> None:
        self.rigs: list = []
        self.slots = live_worker_count()
        self.names = shm_names()

    def tearDown(self) -> None:
        for rig in self.rigs:
            rig.manager.close_all()
        with process_module._ABANDONED_LOCK:
            del process_module._ABANDONED[:]
        self.assertEqual(live_worker_count(), self.slots, "a worker slot was leaked")
        if self.names is not None:
            self.assertEqual(shm_names() - self.names, set(), "a shared segment was leaked")

    def rig(self, *fixtures, **kw) -> Rig:
        rig = Rig(*fixtures, **kw)
        self.rigs.append(rig)
        return rig


class SelectionAndGateTests(IntegrationCase):
    def test_building_the_manager_touches_nothing(self) -> None:
        rig = self.rig("ok_varying", "ok_bgr")
        rig.manager.poll_all(); rig.manager.view("main"); rig.manager.close_all()
        self.assertEqual((rig.made, live_worker_count() - self.slots), ([], 0))
        self.assertEqual(rig.calls_after_build, rig.calls_before)                       # building reads nothing from the device either

    def test_the_parent_imports_neither_opencv_nor_numpy_nor_the_wave_4_adapter(self) -> None:
        code = ("import sys\nfrom tsn_dss.engine.opencv_isolated_decoder import build_isolated_preview_manager\n"
                "print(sorted(m for m in sys.modules if m.split('.')[0] in {'cv2','numpy','PIL','av'}), 'tsn_dss.engine.opencv_preview_decoder' in sys.modules)")
        result = subprocess.run([sys.executable, "-P", "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60,
                                env={**os.environ, "PYTHONPATH": str(ROOT)})
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "[] False"), result.stderr)

    def test_a_refused_gate_creates_no_decoder_and_no_process(self) -> None:
        rig = self.rig("ok_varying", "ok_bgr")
        rig.flow.set_camera_ready("main", False)
        rig.flow.drop_camera_block("wide")
        outcomes = rig.open_both()
        self.assertEqual([o.refusal for o in outcomes], [R.GATE_DENIED, R.GATE_DENIED])
        self.assertEqual((rig.made, live_worker_count() - self.slots), ([], 0))

    def test_the_python_gate_reaches_the_stream_as_a_category(self) -> None:
        rig = self.rig("ok_bgr", "ok_bgr", python_version=(3, 12))
        outcome = rig.manager.open_stream("main")
        self.assertEqual((outcome.refusal, outcome.error_category), (R.OPEN_FAILED, "python_unsupported"))
        self.assertEqual(live_worker_count() - self.slots, 0)

    def test_one_imagelimits_object_drives_the_slot_the_worker_and_the_manager(self) -> None:
        rig = self.rig("ok_bgr", "ok_bgr")
        self.assertIs(rig.manager._limits, rig.isolated.limits)
        rig.manager.open_stream("main")
        cfg = rig.configs[0]
        self.assertEqual((cfg.slot_bytes, cfg.max_width, cfg.max_height, cfg.decoder_worker), (4096, 64, 64, True))


class BothCamerasTests(IntegrationCase):
    def test_main_and_wide_get_their_own_workers_and_deliver_preview_evidence(self) -> None:
        rig = self.rig("ok_varying", "ok_bgr")
        main, wide = rig.open_both()
        self.assertTrue(main.opened and wide.opened)
        self.assertEqual(live_worker_count() - self.slots, 2)                            # at most two workers (D10)
        self.assertEqual(len({w.pid for w in rig.made}), 2)
        results = {r.source_label: r for r in rig.manager.poll_all()}
        for camera in ("main", "wide"):
            evidence = results[camera].evidence
            self.assertEqual((type(evidence).__name__, evidence.pixel_format, evidence.simulated), ("PreviewImageEvidence", PixelFormat.BGR8, False))
            self.assertIs(evidence.freshness, F.FRESH)
            self.assertIsNone(evidence.provider_reported_at)
        self.assertEqual(rig.manager.fresh_pixels("main").data, bytes([10]) * 24)         # each camera has its own stream
        self.assertEqual(rig.manager.fresh_pixels("wide").data, bytes([7]) * 24)
        self.assertEqual(rig.report(0)["captures"][0]["address"].rsplit(":", 1)[1].split("/")[0], "4554")
        self.assertEqual(rig.report(1)["captures"][0]["address"].rsplit(":", 1)[1].split("/")[0], "4555")

    def test_one_camera_lost_the_other_keeps_running_and_the_lost_slot_is_freed(self) -> None:
        rig = self.rig("read_exhausted", "ok_varying")
        rig.open_both()
        results = {r.source_label: r for r in rig.manager.poll_all()}
        self.assertIs(results["main"].state, S.LOST)
        self.assertEqual(results["main"].error_category, "read_failed")
        self.assertIs(results["wide"].state, S.RECEIVING)
        self.assertEqual(live_worker_count() - self.slots, 1)                             # the lost stream's worker is gone and reaped
        self.assertTrue(rig.manager.view("wide").fresh)

    def test_close_all_ends_every_worker_and_leaves_nothing(self) -> None:
        rig = self.rig("ok_varying", "ok_bgr")
        rig.open_both()
        rig.manager.poll_all()
        report = rig.manager.close_all()
        self.assertEqual((report.closed, report.errors), (("main", "wide"), ()))
        self.assertEqual(live_worker_count() - self.slots, 0)
        for index in (0, 1):
            self.assertEqual(rig.report(index)["captures"][0]["released"], 1)


class MandatoryBeforeConnectTests(IntegrationCase):
    def test_evidence_lost_between_the_gate_and_the_connection_stops_the_open(self) -> None:
        rig_holder: list = []

        def lose_evidence(index: int) -> None:                                           # runs after the manager's gate allowed, before the worker exists
            rig_holder[0].flow.set_camera_ready("main", False)

        rig = self.rig("ok_bgr", "ok_bgr", on_worker=lose_evidence)
        rig_holder.append(rig)
        outcome = rig.manager.open_stream("main")
        self.assertTrue(outcome.decision.allowed)                                        # the gate allowed it ...
        self.assertEqual((outcome.refusal, outcome.error_category), (R.OPEN_FAILED, "readiness_revoked"))     # ... the check before OPEN did not
        self.assertEqual(rig.report(0)["captures"], [])                                  # OPEN was never sent: no VideoCapture exists
        self.assertEqual(live_worker_count() - self.slots, 0)

    def test_the_check_does_not_block_a_camera_that_is_still_ready(self) -> None:
        rig = self.rig("ok_bgr", "ok_bgr")
        self.assertTrue(rig.manager.open_stream("main").opened)


class NoControlTests(IntegrationCase):
    def test_nothing_but_reads_reaches_the_device_and_no_canonical_records_are_made(self) -> None:
        rig = self.rig("ok_varying", "ok_bgr")
        rig.open_both()
        rig.manager.poll_all()
        rig.manager.close_all()
        methods = {call[0] for call in rig.flow.transport.calls}
        self.assertTrue(methods <= {"read_device_state", "read_app_state", "test_connection", "discover_via_udp"}, methods)
        text = repr(rig.flow.transport.calls)
        for command in ("iscope_start_view", "start_view", "stop_view", "scope_"):
            self.assertNotIn(command, text)
        for result in rig.manager.poll_all():
            self.assertNotIn(type(result.evidence).__name__ if result.evidence else "", {"Capture", "Frame", "Dataset", "ProcessingRun"})


if __name__ == "__main__":
    unittest.main()
