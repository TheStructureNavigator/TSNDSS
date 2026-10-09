"""DB-03 Wave 4: DB-02 evidence -> gate -> manager -> OpenCV adapter -> fake cv2 (offline)."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tsn_dss.engine.device_runtime import SequentialIdGenerator
from tsn_dss.engine.device_runtime.preview_manager import OpenRefusal as R
from tsn_dss.engine.device_runtime.preview_models import ImageFreshness as F
from tsn_dss.engine.device_runtime.preview_models import PixelFormat
from tsn_dss.engine.device_runtime.preview_readiness import GateReason as G
from tsn_dss.engine.device_runtime.preview_stream import PreviewStreamState as S
from tsn_dss.engine.opencv_preview_decoder import OpenCvDecoderConfig, make_opencv_decoder_factory
from tsn_dss.engine.seestar_preview import build_preview_manager
from tsn_dss.engine.seestar_provider import SeestarUnreachable

try:
    from opencv_fakes import FakeCapture, FakeCv2, bgr, loader
    from seestar_preview_support import POLICY, Flow
    from seestar_support import HOST
except ModuleNotFoundError:  # pragma: no cover
    from tests.opencv_fakes import FakeCapture, FakeCv2, bgr, loader
    from tests.seestar_preview_support import POLICY, Flow
    from tests.seestar_support import HOST

MAIN, WIDE = "4554", "4555"


class Rig:
    """A Wave 3 Flow whose manager decodes through the OpenCV adapter on a fake cv2."""

    def __init__(self, config: OpenCvDecoderConfig | None = None) -> None:
        FakeCv2.reset()
        self.flow = Flow()
        self.loader_calls = 0
        self.factory_calls: list[str] = []
        inner = make_opencv_decoder_factory(config, module_loader=self._counting_loader)

        def factory(camera):
            self.factory_calls.append(camera)
            return inner(camera)

        self.manager = build_preview_manager(
            runtime=self.flow.runtime, connection=self.flow.connection, config=self.flow.config,
            decoder_factory=factory, clock=self.flow.clock, id_generator=SequentialIdGenerator(), policy=POLICY,
        )

    def _counting_loader(self):
        self.loader_calls += 1
        return loader()

    def captures(self, port: str):
        return [c for c in FakeCapture.instances if c.address.endswith(f":{port}/stream")]

    def open_both(self):
        return self.manager.open_stream("main"), self.manager.open_stream("wide")


def frames(n=20):
    return [(True, bgr(4, 2, fill=i)) for i in range(n)]


class GateTests(unittest.TestCase):
    def test_a_refused_gate_creates_no_decoder_and_never_loads_or_opens_opencv(self) -> None:
        rig = Rig()
        rig.flow.set_camera_ready("main", False)
        rig.flow.drop_camera_block("wide")
        outcomes = rig.open_both()
        self.assertEqual([o.refusal for o in outcomes], [R.GATE_DENIED, R.GATE_DENIED])
        self.assertEqual(rig.factory_calls, [])
        self.assertEqual(rig.loader_calls, 0)
        self.assertEqual(FakeCapture.instances, [])

    def test_a_ready_connection_with_no_camera_evidence_opens_nothing(self) -> None:
        rig = Rig()
        self.assertEqual(rig.flow.connection.state.value, "ready")
        rig.flow.transport.fail_next("device_state", SeestarUnreachable("unreachable"))
        rig.flow.transport.fail_next("app_state", SeestarUnreachable("unreachable"))
        self.assertEqual(rig.manager.open_stream("main").decision.reason, G.NO_EVIDENCE)
        self.assertEqual((rig.loader_calls, FakeCapture.instances), (0, []))

    def test_building_and_polling_without_opening_touch_nothing(self) -> None:
        rig = Rig()
        rig.manager.poll_all(); rig.manager.view("main"); rig.manager.close_all()
        self.assertEqual((rig.factory_calls, rig.loader_calls, FakeCapture.instances), ([], 0, []))


class FlowTests(unittest.TestCase):
    def test_both_cameras_through_ffmpeg_with_their_own_endpoints(self) -> None:
        rig = Rig(OpenCvDecoderConfig(open_timeout_ms=4000, read_timeout_ms=2500))
        FakeCv2.scripts = {MAIN: frames(), WIDE: frames()}
        main, wide = rig.open_both()
        self.assertTrue(main.opened and wide.opened)
        for port in (MAIN, WIDE):
            (capture,) = rig.captures(port)
            self.assertEqual(capture.api, FakeCv2.CAP_FFMPEG)
            self.assertEqual(capture.params, [53, 4000, 54, 2500])
        results = {r.source_label: r for r in rig.manager.poll_all()}
        for camera in ("main", "wide"):
            evidence = results[camera].evidence
            self.assertEqual(evidence.pixel_format, PixelFormat.BGR8)
            self.assertIs(evidence.freshness, F.FRESH)
            self.assertFalse(evidence.simulated)  # the adapter is not a simulator, even over a fake module
            self.assertIsNone(evidence.provider_reported_at)
            self.assertTrue(rig.manager.view(camera).fresh)

    def test_one_camera_lost_the_other_keeps_running(self) -> None:
        rig = Rig()
        FakeCv2.scripts = {MAIN: [(True, bgr()), RuntimeError(f"drop at {HOST}")], WIDE: frames()}
        rig.open_both()
        rig.manager.poll_all()
        results = {r.source_label: r for r in rig.manager.poll_all()}
        self.assertIs(results["main"].state, S.LOST)
        self.assertEqual(results["main"].error_category, "read_failed")
        self.assertIs(results["wide"].state, S.RECEIVING)
        self.assertEqual(rig.captures(MAIN)[0].release_calls, 1)
        self.assertEqual(rig.captures(WIDE)[0].release_calls, 0)
        self.assertTrue(rig.manager.view("wide").fresh)

    def test_a_failed_read_is_a_lost_stream_and_the_adapter_never_reconnects(self) -> None:
        rig = Rig()
        FakeCv2.scripts = {MAIN: [(False, None)], WIDE: frames()}
        rig.open_both()
        for _ in range(4):
            result = {r.source_label: r for r in rig.manager.poll_all()}["main"]
        self.assertIs(result.state, S.LOST)
        self.assertEqual(len(rig.captures(MAIN)), 1)  # no reconnect, no second capture
        self.assertEqual(rig.captures(MAIN)[0].read_calls, 1)

    def test_recovery_only_through_a_new_stream_after_new_evidence(self) -> None:
        rig = Rig()
        FakeCv2.scripts = {MAIN: [(True, bgr(fill=1)), (False, None)], WIDE: frames()}
        rig.open_both()
        rig.manager.poll_all(); rig.manager.poll_all()
        old_id = rig.manager.stream_id("main")
        self.assertIs(rig.manager.states()["main"], S.LOST)
        self.assertEqual(rig.manager.open_stream("main").decision.reason, G.PREDATES_LOSS)
        rig.flow.advance(1)
        rig.flow.set_camera_ready("main", False)
        self.assertEqual(rig.manager.open_stream("main").decision.reason, G.UNAVAILABLE)
        self.assertEqual(len(rig.captures(MAIN)), 1)  # still only the first capture
        rig.flow.advance(1)
        rig.flow.set_camera_ready("main", True)
        FakeCv2.scripts = {MAIN: frames()}
        out = rig.manager.open_stream("main")
        self.assertTrue(out.opened)
        self.assertNotEqual(out.stream_id, old_id)
        self.assertEqual(len(rig.captures(MAIN)), 2)
        self.assertEqual(rig.captures(MAIN)[0].release_calls, 1)  # the lost capture is not touched again
        self.assertEqual(rig.manager.poll("main").evidence.sequence, 1)
        self.assertIs(rig.manager.states()["wide"], S.RECEIVING)

    def test_open_failure_is_contained_per_camera(self) -> None:
        rig = Rig()
        FakeCv2.scripts = {WIDE: frames()}
        FakeCv2.refusing_ports = {MAIN}
        main, wide = rig.open_both()
        self.assertEqual((main.refusal, main.error_category), (R.OPEN_FAILED, "open_failed"))
        self.assertTrue(wide.opened)
        self.assertEqual(rig.captures(MAIN)[0].release_calls, 1)

    def test_unavailable_opencv_is_an_open_failure_with_a_category(self) -> None:
        FakeCv2.reset()
        flow = Flow()

        def broken():
            raise ImportError(f"cv2 not found near {HOST}")

        manager = build_preview_manager(
            runtime=flow.runtime, connection=flow.connection, config=flow.config,
            decoder_factory=make_opencv_decoder_factory(module_loader=broken), clock=flow.clock,
        )
        out = manager.open_stream("main")
        self.assertEqual((out.refusal, out.error_category), (R.OPEN_FAILED, "opencv_unavailable"))
        self.assertEqual(FakeCapture.instances, [])

    def test_disconnect_releases_every_capture_once(self) -> None:
        rig = Rig()
        FakeCv2.scripts = {MAIN: frames(), WIDE: frames()}
        rig.open_both()
        rig.flow.runtime.disconnect(rig.flow.connection)
        rig.manager.poll_all(); rig.manager.poll_all()
        self.assertEqual([c.release_calls for c in FakeCapture.instances], [1, 1])

    def test_a_stalled_stream_is_never_presented_as_fresh(self) -> None:
        rig = Rig()
        FakeCv2.scripts = {MAIN: [(True, bgr())]}
        rig.manager.open_stream("main")
        rig.manager.poll("main")
        self.assertTrue(rig.manager.view("main").fresh)
        rig.flow.advance(6)
        self.assertFalse(rig.manager.view("main").fresh)
        self.assertIsNone(rig.manager.fresh_pixels("main"))


class RedactionTests(unittest.TestCase):
    def test_no_output_reveals_the_host_or_the_address(self) -> None:
        rig = Rig()
        FakeCv2.scripts = {MAIN: [RuntimeError(f"failure at rtsp://{HOST}:4554/stream")], WIDE: frames()}
        outcomes = list(rig.open_both())
        polls = list(rig.manager.poll_all())
        views = [rig.manager.view(c) for c in ("main", "wide")]
        report = rig.manager.close_all()
        everything = repr(outcomes) + repr(polls) + repr(views) + repr(report) + repr(rig.manager)
        self.assertNotIn(HOST, everything)
        self.assertNotIn("rtsp://", everything)


class EvidenceOnlyTests(unittest.TestCase):
    def test_a_full_opencv_flow_leaves_the_canonical_database_and_directory_untouched(self) -> None:
        from tsn_dss.engine.sqlite.db import initialize_database

        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tsn.db"
            initialize_database(db_path).close()

            def state():
                con = sqlite3.connect(str(db_path))
                try:
                    names = [r[0] for r in con.execute("select name from sqlite_master where type='table' order by name")]
                    counts = {n: con.execute(f'select count(*) from "{n}"').fetchone()[0] for n in names}
                finally:
                    con.close()
                return hashlib.sha256(db_path.read_bytes()).hexdigest(), counts

            before, files = state(), sorted(os.listdir(tmp))
            os.chdir(tmp)
            try:
                rig = Rig()
                FakeCv2.scripts = {MAIN: frames(3), WIDE: frames(3)}
                rig.open_both()
                for _ in range(3):
                    rig.manager.poll_all()
                rig.manager.close_all()
                self.assertEqual(len(FakeCapture.instances), 2)  # the flow really ran
            finally:
                os.chdir(previous)
            self.assertEqual(state(), before)
            self.assertEqual(sorted(os.listdir(tmp)), files)


if __name__ == "__main__":
    unittest.main()
