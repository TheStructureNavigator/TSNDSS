"""DB-03 Wave 3: DB-02 evidence -> gate -> manager -> fake decoder -> pixels/evidence (offline)."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import unittest
from dataclasses import fields, is_dataclass
from pathlib import Path

from tsn_dss.engine.device_runtime.preview_manager import OpenRefusal as R
from tsn_dss.engine.device_runtime.preview_models import ImageFreshness as F
from tsn_dss.engine.device_runtime.preview_models import PreviewSourceError
from tsn_dss.engine.device_runtime.preview_readiness import GateReason as G
from tsn_dss.engine.device_runtime.preview_simulator import make_pixels
from tsn_dss.engine.device_runtime.preview_stream import PreviewStreamState as S
from tsn_dss.engine.seestar_preview import DecoderError, SeestarPreviewConfig
from tsn_dss.engine.seestar_provider import SeestarUnreachable

try:
    from seestar_preview_support import FakeDecoder, Flow
    from seestar_support import HOST
except ModuleNotFoundError:  # pragma: no cover
    from tests.seestar_preview_support import FakeDecoder, Flow
    from tests.seestar_support import HOST

READS = {"read_device_state", "read_app_state", "test_connection"}


def open_both(flow: Flow):
    return flow.manager.open_stream("main"), flow.manager.open_stream("wide")


class FullFlowTests(unittest.TestCase):
    def test_both_cameras_flow_from_device_evidence_to_pixels(self) -> None:
        flow = Flow()
        main, wide = open_both(flow)
        self.assertTrue(main.opened and wide.opened)
        self.assertEqual(main.decision.reason, G.ALLOWED)
        results = {r.source_label: r for r in flow.manager.poll_all()}
        for camera in ("main", "wide"):
            ev = results[camera].evidence
            self.assertEqual(ev.source_label, camera)
            self.assertEqual(ev.connection_id, flow.connection.connection_id)
            self.assertEqual(ev.provider_id, flow.connection.provider_id)
            self.assertIs(ev.freshness, F.FRESH)
            self.assertTrue(ev.simulated)  # the decoder is a fake, and says so
            self.assertIsNone(ev.provider_reported_at)
            self.assertEqual(flow.manager.fresh_pixels(camera), make_pixels(0))
            self.assertTrue(flow.manager.view(camera).fresh)
        self.assertNotEqual(flow.manager.stream_id("main"), flow.manager.stream_id("wide"))

    def test_each_camera_reads_its_own_endpoint(self) -> None:
        flow = Flow()
        open_both(flow)
        main_decoder, wide_decoder = flow.decoders.for_camera("main")[0], flow.decoders.for_camera("wide")[0]
        self.assertEqual(main_decoder.endpoints[0].address, f"rtsp://{HOST}:4554/stream")
        self.assertEqual(wide_decoder.endpoints[0].address, f"rtsp://{HOST}:4555/stream")
        self.assertEqual((main_decoder.camera, wide_decoder.camera), ("main", "wide"))

    def test_only_allow_listed_device_reads_happen_during_the_whole_flow(self) -> None:
        flow = Flow()
        open_both(flow)
        flow.manager.poll_all()
        flow.manager.close_all()
        flow.runtime.disconnect(flow.connection)
        self.assertTrue({c[0] for c in flow.transport.calls} <= READS)

    def test_building_the_manager_touches_neither_device_nor_decoder(self) -> None:
        flow = Flow()
        self.assertEqual(flow.calls_before_build, flow.calls_after_build)
        self.assertEqual(flow.decoders.calls, [])
        flow.manager.poll_all()
        flow.manager.view("main")
        flow.manager.close_all()
        self.assertEqual(flow.calls_before_build, len(flow.transport.calls))
        self.assertEqual(flow.decoders.calls, [])

    def test_main_alone_and_wide_alone(self) -> None:
        for camera, other in (("main", "wide"), ("wide", "main")):
            flow = Flow()
            flow.set_camera_ready(other, False)
            out = flow.manager.open_stream(camera)
            self.assertTrue(out.opened, camera)
            denied = flow.manager.open_stream(other)
            self.assertEqual((denied.refusal, denied.decision.reason), (R.GATE_DENIED, G.UNAVAILABLE))
            self.assertEqual(flow.decoders.calls, [camera])
            self.assertEqual(flow.manager.active_labels, (camera,))
            self.assertIsNotNone(flow.manager.poll(camera).evidence)


class RefusalTests(unittest.TestCase):
    def assert_no_decoder_activity(self, flow: Flow) -> None:
        self.assertEqual(flow.decoders.calls, [])
        self.assertEqual(sum(d.open_calls for d in flow.decoders.decoders), 0)

    def test_no_fresh_available_means_no_decoder_is_even_created(self) -> None:
        flow = Flow()
        flow.set_camera_ready("main", False)
        flow.drop_camera_block("wide")
        main, wide = open_both(flow)
        self.assertEqual(main.decision.reason, G.UNAVAILABLE)
        self.assertEqual(wide.decision.reason, G.UNKNOWN)
        self.assertFalse(main.opened or wide.opened)
        self.assert_no_decoder_activity(flow)

    def test_connection_ready_does_not_open_anything(self) -> None:
        flow = Flow()
        self.assertEqual(flow.connection.state.value, "ready")
        flow.drop_camera_block("main")
        flow.drop_camera_block("wide")
        for out in open_both(flow):
            self.assertIs(out.refusal, R.GATE_DENIED)
        self.assert_no_decoder_activity(flow)

    def test_evidence_read_failure_blocks_opening(self) -> None:
        flow = Flow()
        flow.transport.fail_next("device_state", SeestarUnreachable("unreachable"))
        flow.transport.fail_next("app_state", SeestarUnreachable("unreachable"))
        out = flow.manager.open_stream("main")
        self.assertEqual((out.refusal, out.decision.reason), (R.GATE_DENIED, G.NO_EVIDENCE))
        self.assert_no_decoder_activity(flow)
        self.assertTrue(flow.manager.open_stream("main").opened)  # next attempt reads anew

    def test_retained_stale_values_do_not_open_anything(self) -> None:
        flow = Flow()
        flow.runtime.read_telemetry(flow.connection)  # known values are now retained by the provider
        flow.transport.fail_next("app_state", SeestarUnreachable("unreachable"))
        out = flow.manager.open_stream("main")
        self.assertEqual(out.decision.reason, G.UNKNOWN)
        self.assert_no_decoder_activity(flow)

    def test_cached_evidence_older_than_the_limit_is_refused(self) -> None:
        flow = Flow(max_age_s=10.0)
        cached = flow.runtime.read_telemetry(flow.connection)

        class CachingReader:
            def read_telemetry(self, connection):
                return cached

        from tsn_dss.engine.seestar_preview import build_preview_manager

        manager = build_preview_manager(
            runtime=CachingReader(), connection=flow.connection, config=flow.config,
            decoder_factory=flow.decoders, clock=flow.clock,
        )
        self.assertTrue(manager.open_stream("main").opened)
        manager.close_all()
        flow.advance(10)
        flow.advance(1)  # evidence is now 11 s old
        out = manager.open_stream("wide")
        self.assertEqual(out.decision.reason, G.STALE_EVIDENCE)
        self.assertEqual(flow.decoders.calls.count("wide"), 0)

    def test_unconfigured_camera_is_refused_without_any_device_read(self) -> None:
        flow = Flow(config=SeestarPreviewConfig(host=HOST, cameras=("wide",)))
        reads = len(flow.transport.calls)
        out = flow.manager.open_stream("main")
        self.assertEqual(out.refusal, R.UNKNOWN_CAMERA)
        self.assertEqual(len(flow.transport.calls), reads)


class IsolationAndRecoveryTests(unittest.TestCase):
    def test_decoder_fault_on_one_camera_leaves_the_other_running(self) -> None:
        flow = Flow()
        flow.decoders.queues["main"] = [FakeDecoder("main", [make_pixels(1), DecoderError("stream_ended")])]
        open_both(flow)
        flow.manager.poll_all()
        results = {r.source_label: r for r in flow.manager.poll_all()}
        self.assertIs(results["main"].state, S.LOST)
        self.assertEqual(results["main"].error_category, "stream_ended")
        self.assertIs(results["wide"].state, S.RECEIVING)
        self.assertTrue(flow.manager.view("wide").fresh)
        self.assertEqual(flow.decoders.for_camera("main")[0].close_calls, 1)
        self.assertEqual(flow.decoders.for_camera("wide")[0].close_calls, 0)

    def test_open_failure_of_one_decoder_is_contained(self) -> None:
        flow = Flow()
        flow.decoders.queues["wide"] = [FakeDecoder("wide", open_error=DecoderError("connect_failed"))]
        main, wide = open_both(flow)
        self.assertTrue(main.opened)
        self.assertEqual((wide.refusal, wide.error_category), (R.OPEN_FAILED, "connect_failed"))
        self.assertEqual(flow.decoders.for_camera("wide")[0].close_calls, 1)

    def test_recovery_only_after_new_positive_evidence(self) -> None:
        flow = Flow()
        flow.decoders.queues["main"] = [
            FakeDecoder("main", [make_pixels(1), DecoderError("stream_ended")]),
            FakeDecoder("main", [make_pixels(7), make_pixels(8)]),
        ]
        open_both(flow)
        flow.manager.poll_all(); flow.manager.poll_all()
        old_id = flow.manager.stream_id("main")
        self.assertIs(flow.manager.states()["main"], S.LOST)
        # same host instant: the evidence is not newer than the loss
        out = flow.manager.open_stream("main")
        self.assertEqual(out.decision.reason, G.PREDATES_LOSS)
        # network is fine and time has passed, but the device says the camera is not serving
        flow.advance(1)
        flow.set_camera_ready("main", False)
        out = flow.manager.open_stream("main")
        self.assertEqual(out.decision.reason, G.UNAVAILABLE)
        self.assertEqual(len(flow.decoders.for_camera("main")), 1)  # no new decoder was created
        # the device reports it again: a new stream with a new identity and a new decoder
        flow.advance(1)
        flow.set_camera_ready("main", True)
        out = flow.manager.open_stream("main")
        self.assertTrue(out.opened)
        self.assertNotEqual(out.stream_id, old_id)
        self.assertEqual(len(flow.decoders.for_camera("main")), 2)
        self.assertEqual(flow.manager.poll("main").evidence.sequence, 1)
        self.assertEqual(flow.manager.fresh_pixels("main"), make_pixels(7))
        self.assertIs(flow.manager.states()["wide"], S.RECEIVING)  # untouched throughout

    def test_disconnect_closes_every_decoder_once_and_blocks_reopening(self) -> None:
        flow = Flow()
        open_both(flow)
        flow.runtime.disconnect(flow.connection)
        flow.manager.poll_all()
        flow.manager.poll_all()
        self.assertEqual([d.close_calls for d in flow.decoders.decoders], [1, 1])
        reads = len(flow.transport.calls)
        self.assertEqual(flow.manager.open_stream("main").refusal, R.CONNECTION_NOT_USABLE)
        self.assertEqual(len(flow.transport.calls), reads)
        self.assertEqual(len(flow.decoders.decoders), 2)

    def test_stalled_decoder_is_never_presented_as_fresh(self) -> None:
        flow = Flow()
        flow.decoders.queues["main"] = [FakeDecoder("main", [make_pixels(1)])]
        flow.manager.open_stream("main")
        flow.manager.poll("main")
        self.assertTrue(flow.manager.view("main").fresh)
        flow.advance(6)
        view = flow.manager.view("main")
        self.assertFalse(view.fresh)
        self.assertIsNone(flow.manager.fresh_pixels("main"))


class RedactionTests(unittest.TestCase):
    def test_host_and_address_never_leak_through_any_output(self) -> None:
        flow = Flow()
        flow.decoders.queues["main"] = [FakeDecoder("main", [RuntimeError(f"failure at {HOST}")])]
        flow.decoders.queues["wide"] = [FakeDecoder("wide", open_error=RuntimeError(f"cannot open rtsp://{HOST}:4555/stream"))]
        outcomes = list(open_both(flow))
        polls = list(flow.manager.poll_all())
        views = [flow.manager.view(c) for c in ("main", "wide")]
        report = flow.manager.close_all()
        everything = repr(outcomes) + repr(polls) + repr(views) + repr(report) + repr(flow.manager) + repr(flow.config)
        self.assertNotIn(HOST, everything)
        self.assertNotIn("rtsp://", everything)
        for item in (*outcomes, *polls):
            self.assertIn(getattr(item, "error_category", None), (None, "decoder_error"))


if __name__ == "__main__":
    unittest.main()


DOMAIN_WORDS = ("session", "observation", "capture", "frame", "dataset", "processing", "target", "project")


class EvidenceOnlyTests(unittest.TestCase):
    """REQ-018 and REQ-064 by behavior, for the Seestar integration."""

    def test_full_flow_leaves_the_canonical_database_and_directory_untouched(self) -> None:
        from tsn_dss.engine.sqlite.db import initialize_database

        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tsn.db"
            initialize_database(db_path).close()

            def state():
                con = sqlite3.connect(str(db_path))
                try:
                    tables = [r[0] for r in con.execute("select name from sqlite_master where type='table' order by name")]
                    counts = {t: con.execute(f'select count(*) from "{t}"').fetchone()[0] for t in tables}
                finally:
                    con.close()
                return hashlib.sha256(db_path.read_bytes()).hexdigest(), counts

            before, files = state(), sorted(os.listdir(tmp))
            self.assertGreater(len(before[1]), 5)
            os.chdir(tmp)
            try:
                flow = Flow()
                flow.decoders.queues["main"] = [FakeDecoder("main", [make_pixels(1), DecoderError("stream_ended")])]
                open_both(flow)
                flow.manager.poll_all(); flow.manager.poll_all()
                flow.advance(1)
                flow.manager.open_stream("main")
                flow.runtime.disconnect(flow.connection)
                flow.manager.poll_all()
                self.assertGreaterEqual(len(flow.decoders.decoders), 3)  # the flow really ran
            finally:
                os.chdir(previous)
            self.assertEqual(state(), before)
            self.assertEqual(sorted(os.listdir(tmp)), files)

    def test_integration_types_carry_no_domain_identity_or_claim(self) -> None:
        from tsn_dss.engine import seestar_preview as package
        from tsn_dss.engine.device_runtime.preview_readiness import ReadinessEvidence

        classes = [ReadinessEvidence] + [getattr(package, n) for n in package.__all__ if is_dataclass(getattr(package, n))]
        self.assertGreaterEqual(len(classes), 3)
        for cls in classes:
            names = {f.name for f in fields(cls)}
            self.assertFalse([n for n in names if any(w in n for w in DOMAIN_WORDS)], cls.__name__)
            self.assertFalse([n for n in dir(cls) if not n.startswith("_") and any(w in n for w in DOMAIN_WORDS)], cls.__name__)

    def test_a_stream_proves_nothing_about_domain_facts(self) -> None:
        flow = Flow()
        flow.manager.open_stream("main")
        evidence = flow.manager.poll("main").evidence
        self.assertTrue(evidence.is_runtime_evidence_only)
        self.assertFalse(evidence.is_canonical_record)
