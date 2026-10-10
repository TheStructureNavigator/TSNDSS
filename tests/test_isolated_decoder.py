"""DB-03 Wave 4B-3b: ``ProcessIsolatedDecoder`` -- the parent-side ``ImageDecoder`` over real isolated workers.

Real worker processes; the fake ``cv2`` exists only in the test entry ``tests/decoder_worker_fixture.py`` (the real 4B-3a handler and
Wave 4 adapter behind it) and the protocol fixtures of ``tests/process_fixtures.py`` (deliberately broken slots). No OpenCV, no
NumPy, no network, no device. Linux results here do not replace the Windows run (including a venv run) that the operator owns.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.test_isolated_limit import FakePopen
from tests.test_isolated_slot import ADDRESS, POSIX, Isolated, _gone_within
from tsn_dss.engine.device_runtime.preview_models import ImageLimits, PixelFormat, PreviewPixels, SourceImage
from tsn_dss.engine.opencv_isolated_decoder import (
    IsolatedDecoderConfig, OperationTimings, ProcessIsolatedDecoder, WorkerConfig, WorkerProcess, WorkerState as S,
    abandoned_worker_count, live_worker_count, make_isolated_decoder_factory, reclaim_abandoned_workers,
)
from tsn_dss.engine.opencv_isolated_decoder import process as process_module
from tsn_dss.engine.seestar_preview import DecoderError, ImageDecoder, StreamEndpoint
from tsn_dss.engine.seestar_preview.source import RtspPreviewSource

ROOT = Path(__file__).resolve().parents[1]
LIMITS = ImageLimits(max_width=64, max_height=64, max_image_bytes=4096, max_buffer_bytes=4096)
WORKER = WorkerConfig(start_deadline_s=3.0, ping_deadline_s=0.5, close_graceful_s=0.3, terminate_wait_s=0.5, kill_wait_s=0.5,
                      open_margin_s=0.3, read_margin_s=0.3)
DECODER_FIXTURE = "tests.decoder_worker_fixture"
PROTOCOL_FIXTURE = "tests.process_fixtures"


def cfg(**override) -> IsolatedDecoderConfig:
    return IsolatedDecoderConfig(**{"open_timeout_ms": 1000, "read_timeout_ms": 1000, "limits": LIMITS, "worker": WORKER, **override})


def entry_factory(fixture: str, entry: str = DECODER_FIXTURE, report: str | None = None):
    extra = ("--fixture", fixture) + (("--report", report) if report else ())

    def build(config, **kw):
        return WorkerProcess(config, _entry_module=entry, _extra_args=extra, **kw)
    return build


def endpoint(camera: str = "main", address: str = ADDRESS) -> StreamEndpoint:
    return StreamEndpoint(camera, address)


def category_of(call) -> str:
    try:
        call()
    except DecoderError as exc:
        return exc.category
    raise AssertionError("DecoderError not raised")


class DecoderCase(Isolated):
    def setUp(self) -> None:
        super().setUp()
        self.decoders: list = []

    def tearDown(self) -> None:
        for d in self.decoders:
            try:
                d.close()
            except DecoderError:
                pass
        with process_module._ABANDONED_LOCK:
            del process_module._ABANDONED[:]
        super().tearDown()

    def decoder(self, fixture: str = "ok_bgr", *, entry: str = DECODER_FIXTURE, report: str | None = None, config=None, **kw):
        d = ProcessIsolatedDecoder(config or cfg(), camera="main", _worker_factory=entry_factory(fixture, entry, report), **kw)
        self.decoders.append(d)
        return d

    def opened(self, fixture: str = "ok_bgr", **kw):
        d = self.decoder(fixture, **kw)
        d.open(endpoint())
        return d

    def report(self) -> Path:
        return Path(tempfile.mkdtemp()) / "report.json"


class ConstructionTests(unittest.TestCase):
    def test_the_constructor_starts_nothing_and_imports_nothing_heavy(self) -> None:
        factory = mock.Mock(side_effect=AssertionError("a worker was created by the constructor"))
        before = live_worker_count()
        d = ProcessIsolatedDecoder(cfg(), camera="main", _worker_factory=factory, _python_version=(3, 12))     # even on an old Python
        factory.assert_not_called()
        self.assertEqual(live_worker_count(), before)
        self.assertFalse(d.simulated)
        code = ("import sys\nfrom tsn_dss.engine.opencv_isolated_decoder import ProcessIsolatedDecoder, make_isolated_decoder_factory\n"
                "d = ProcessIsolatedDecoder()\nmake_isolated_decoder_factory()('main')\n"
                "print(sorted(m for m in sys.modules if m.split('.')[0] in {'cv2','numpy','PIL','av','multiprocessing'} and m != 'multiprocessing' "
                "and not m.startswith('multiprocessing.')) , 'tsn_dss.engine.opencv_preview_decoder' in sys.modules)")
        result = subprocess.run([sys.executable, "-P", "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60,
                                env={**os.environ, "PYTHONPATH": str(ROOT)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "[] False")

    def test_it_implements_the_existing_interface_and_nothing_more(self) -> None:
        d = ProcessIsolatedDecoder()
        self.assertIsInstance(d, ImageDecoder)
        self.assertEqual({n for n in dir(ImageDecoder) if not n.startswith("_")}, {"open", "read", "close"})
        public = {n for n in dir(d) if not n.startswith("_")}
        self.assertTrue({"open", "read", "close", "simulated"} <= public)
        self.assertFalse(public & {"start", "stop", "arm", "park", "slew", "capture", "frame", "session"})

    def test_config_validation(self) -> None:
        for kw in ({"open_timeout_ms": 99}, {"read_timeout_ms": 60001}, {"open_timeout_ms": True}, {"read_timeout_ms": "3000"},
                   {"limits": object()}, {"worker": object()}):
            with self.assertRaises(ValueError, msg=str(kw)):
                IsolatedDecoderConfig(**kw)
        IsolatedDecoderConfig(open_timeout_ms=100, read_timeout_ms=60000)

    def test_one_imagelimits_object_is_authoritative_for_the_slot_and_the_worker_limits(self) -> None:
        base = WorkerConfig(slot_bytes=123, max_width=7, max_height=9, decoder_worker=False)
        derived = IsolatedDecoderConfig(limits=ImageLimits(max_width=800, max_height=600, max_image_bytes=1_000_000, max_buffer_bytes=2_000_000),
                                        worker=base).worker_config()
        self.assertEqual((derived.slot_bytes, derived.max_width, derived.max_height, derived.decoder_worker), (1_000_000, 800, 600, True))
        default = IsolatedDecoderConfig().worker_config()
        self.assertEqual((default.slot_bytes, default.max_width, default.max_height), (32 * 1024 * 1024, 4096, 4096))
        self.assertEqual((default.start_deadline_s, default.open_margin_s, default.read_margin_s), (10.0, 1.5, 1.0))        # D12 unchanged

    def test_the_factory_builds_a_new_unstarted_decoder_per_stream(self) -> None:
        factory = make_isolated_decoder_factory(cfg())
        a, b = factory("main"), factory("wide")
        self.assertIsNot(a, b)
        self.assertIn("main", repr(a))
        self.assertEqual(live_worker_count(), 0)


class PythonGateTests(DecoderCase):
    def test_an_old_python_is_refused_at_open_before_any_worker_exists(self) -> None:
        d = ProcessIsolatedDecoder(cfg(), camera="main", _python_version=(3, 12))
        self.decoders.append(d)
        self.assertEqual(category_of(lambda: d.open(endpoint())), "python_unsupported")
        self.assertEqual(live_worker_count(), 0)
        d.close()                                                                  # safe: there is nothing to stop
        self.assertEqual(category_of(lambda: d.read()), "not_open")

    def test_the_supported_version_passes_the_gate(self) -> None:
        d = ProcessIsolatedDecoder(cfg(), camera="main", _worker_factory=entry_factory("ok_bgr"), _python_version=(3, 13, 0))
        self.decoders.append(d)
        d.open(endpoint())
        self.assertEqual(d.read().width, 4)


class LifecycleTests(DecoderCase):
    def test_open_read_close_deliver_parent_owned_pixels(self) -> None:
        d = self.opened("ok_varying")
        for n in range(3):
            pixels = d.read()
            self.assertIsInstance(pixels, PreviewPixels)
            self.assertIsInstance(pixels.data, bytes)
            self.assertEqual((pixels.width, pixels.height, pixels.pixel_format), (4, 2, PixelFormat.BGR8))
            self.assertEqual(pixels.data, bytes([10 + n]) * 24)
        d.close()
        self.assertEqual(d.uncertain, ())
        self.assertEqual(d.last_stop_report.steps, ("close",))

    def test_gray_images(self) -> None:
        d = self.opened("ok_gray")
        pixels = d.read()
        self.assertEqual((pixels.pixel_format, pixels.width, pixels.height, len(pixels.data)), (PixelFormat.GRAY8, 4, 2, 8))

    def test_it_works_behind_the_rtsp_source_the_way_the_manager_uses_it(self) -> None:
        source = RtspPreviewSource(endpoint(), self.decoder("ok_varying"))
        source.open()
        item = source.read()
        self.assertIsInstance(item, SourceImage)
        self.assertIsNone(item.provider_reported_at)
        source.close()
        source.close()

    def test_errors_become_the_streams_error_categories_through_the_source(self) -> None:
        source = RtspPreviewSource(endpoint(), self.decoder("open_refused"))
        with self.assertRaises(Exception) as ctx:
            source.open()
        self.assertEqual(ctx.exception.category, "open_failed")

    def test_the_decoder_is_single_use(self) -> None:
        d = self.opened()
        self.assertEqual(category_of(lambda: d.open(endpoint())), "invalid_state")        # a second open on a live decoder
        d.read()                                                                           # ... which does not harm it
        d.close()
        self.assertEqual(category_of(lambda: d.open(endpoint())), "invalid_state")        # no reopening after close
        self.assertEqual(category_of(lambda: d.read()), "not_open")
        d.close()
        d.close()                                                                          # idempotent

    def test_a_new_decoder_after_a_closed_one_gets_a_new_worker_and_a_new_sequence(self) -> None:
        first = self.opened("ok_varying")
        first.read(), first.read()
        first.close()
        second = self.opened("ok_varying")
        self.assertEqual(second.read().data, bytes([10]) * 24)
        self.assertEqual(live_worker_count(), 1)

    def test_read_before_open_and_after_a_failed_open(self) -> None:
        d = self.decoder("open_refused")
        self.assertEqual(category_of(lambda: d.read()), "not_open")
        self.assertEqual(category_of(lambda: d.open(endpoint())), "open_failed")
        self.assertEqual(category_of(lambda: d.read()), "not_open")
        self.assertEqual(category_of(lambda: d.open(endpoint())), "invalid_state")
        d.close()

    def test_an_invalid_endpoint_is_refused_before_a_worker_exists(self) -> None:
        for bad in (endpoint(address="http://x.invalid/a"), endpoint(address=""), object(), None):
            d = self.decoder()
            self.assertEqual(category_of(lambda: d.open(bad)), "invalid_endpoint")
            self.assertEqual(live_worker_count(), 0)

    def test_an_address_the_protocol_refuses_ends_the_worker(self) -> None:
        d = self.decoder()
        self.assertEqual(category_of(lambda: d.open(endpoint(address="rtsp://has space.invalid/x"))), "invalid_endpoint")
        self.assertEqual(live_worker_count(), 0)

    def test_open_errors_use_the_wave_4_categories_and_leave_nothing_behind(self) -> None:
        for fixture, category in (("open_refused", "open_failed"), ("backend_mismatch", "backend_mismatch"),
                                  ("backend_error", "backend_unverified"), ("version_old", "opencv_version_unsupported"),
                                  ("version_unknown", "opencv_version_unknown"), ("loader_missing", "opencv_unavailable"),
                                  ("construct_error", "open_failed")):
            with self.subTest(fixture):
                d = self.decoder(fixture)
                self.assertEqual(category_of(lambda: d.open(endpoint())), category)
                self.assertEqual(live_worker_count(), 0)
                self.assertIsNone(d._worker._segment)

    def test_read_errors_use_the_wave_4_categories_and_end_the_worker(self) -> None:
        for fixture, category in (("read_exhausted", "read_failed"), ("none_frame", "decoder_bad_output"), ("float_frame", "unsupported_depth"),
                                  ("four_channels", "unsupported_format"), ("crash_read", "worker_crashed")):
            with self.subTest(fixture):
                d = self.opened(fixture)
                self.assertEqual(category_of(lambda: d.read()), category)
                self.assertEqual(live_worker_count(), 0)
                self.assertEqual(category_of(lambda: d.read()), "not_open")            # dead after any failure

    def test_a_busy_decoder_refuses_a_second_call_instead_of_waiting(self) -> None:
        d = self.opened("hang_read")
        errors: list = []
        thread = threading.Thread(target=lambda: errors.append(category_of(lambda: d.read())))
        thread.start()
        time.sleep(0.3)
        began = time.monotonic()
        self.assertEqual(category_of(lambda: d.read()), "busy")
        self.assertLess(time.monotonic() - began, 0.2)
        thread.join(10)
        self.assertEqual(errors, ["read_deadline_exceeded"])


class DeadlineAndContainmentTests(DecoderCase):
    def test_a_blocked_read_is_ended_at_the_hard_deadline_and_measured(self) -> None:
        d = self.opened("hang_read", config=cfg(read_timeout_ms=100))
        began = time.monotonic()
        self.assertEqual(category_of(lambda: d.read()), "read_deadline_exceeded")
        total = time.monotonic() - began
        self.assertLess(total, 0.1 + 0.3 + 0.3 + 0.5 + 0.5 + 1.0)
        t = d.timings
        self.assertEqual(t.read_count, 1)
        self.assertGreaterEqual(t.read_last_s, 0.1 + 0.3 - 0.02)                  # the real wait, not an estimate
        self.assertLessEqual(t.read_last_s, total + 0.02)
        pid, worker_pid = d._worker.pid, d._worker.worker_pid
        self.assertTrue(_gone_within(pid) and _gone_within(worker_pid))
        d.close()
        self.assertIsNotNone(d.timings.stop_s)

    def test_a_worker_spinning_with_the_gil_held_is_ended_too(self) -> None:
        d = self.opened("spin_read", config=cfg(read_timeout_ms=100))
        self.assertEqual(category_of(lambda: d.read()), "read_deadline_exceeded")
        self.assertTrue(_gone_within(d._worker.worker_pid))

    def test_a_constructor_that_never_returns_is_ended_at_the_open_deadline(self) -> None:
        d = self.decoder("hang_construct", config=cfg(open_timeout_ms=100))
        began = time.monotonic()
        self.assertEqual(category_of(lambda: d.open(endpoint())), "open_deadline_exceeded")
        total = time.monotonic() - began
        t = d.timings
        self.assertGreaterEqual(t.open_s, 0.1 + 0.3 - 0.02)
        self.assertGreaterEqual(t.open_total_s, t.start_s + t.open_s - 0.02)
        self.assertLessEqual(t.open_total_s, total + 0.02)
        self.assertTrue(_gone_within(d._worker.worker_pid))
        self.assertEqual(live_worker_count(), 0)

    def test_close_from_another_thread_wakes_a_blocked_read_without_hanging(self) -> None:
        d = self.opened("hang_read", config=cfg(read_timeout_ms=60000))
        errors: list = []
        thread = threading.Thread(target=lambda: errors.append(category_of(lambda: d.read())))
        thread.start()
        time.sleep(0.3)
        began = time.monotonic()
        d.close()
        self.assertLess(time.monotonic() - began, 3.0)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIn(errors[0], {"worker_ipc_lost", "worker_crashed"})
        self.assertIn("operation_interrupted", d.uncertain)                       # killed in flight: reported, not hidden
        self.assertTrue(_gone_within(d._worker.worker_pid))
        d.close()

    def test_a_stream_ended_by_force_is_flagged_and_not_reported_as_a_clean_close(self) -> None:
        d = self.opened("hang_read", config=cfg(read_timeout_ms=100))
        category_of(lambda: d.read())
        d.close()
        self.assertIn("stream_terminated", d.uncertain)

    def test_timings_cover_start_open_read_and_stop(self) -> None:
        d = self.opened("ok_bgr")
        d.read()
        d.read()
        d.close()
        t = d.timings
        self.assertIsInstance(t, OperationTimings)
        for value in (t.start_s, t.open_s, t.open_total_s, t.read_last_s, t.read_max_s, t.stop_s):
            self.assertGreater(value, 0)
        self.assertEqual(t.read_count, 2)
        self.assertGreaterEqual(t.open_total_s, t.start_s + t.open_s - 0.02)
        self.assertGreaterEqual(t.read_total_s, t.read_max_s)
        self.assertIsNone(t.before_connect_s)
        self.assertGreater(d.last_decode_ns, 0)                                    # a separate diagnostic, not a substitute


class ProtocolViolationTests(DecoderCase):
    """A worker that breaks the slot protocol never produces pixels; the worker is ended and the slot freed."""

    def test_corrupted_or_inconsistent_slots_are_rejected(self) -> None:
        for fixture in ("torn_frame", "stale_fence_only", "bad_crc", "pixels_changed_after_crc", "lying_width", "lying_nbytes",
                        "oversize_nbytes", "format_mismatch", "header_overwritten", "magic_corrupt", "version_corrupt",
                        "unknown_pixel_format_in_header", "image_wrong_op", "read_ok_status", "garbage_after_open"):
            with self.subTest(fixture):
                d = self.opened(fixture, entry=PROTOCOL_FIXTURE)
                self.assertEqual(category_of(lambda: d.read()), "worker_protocol_error")
                self.assertEqual(live_worker_count(), 0)
                self.assertEqual(category_of(lambda: d.read()), "not_open")

    def test_a_sequence_that_skips_or_repeats_is_rejected_after_the_first_good_image(self) -> None:
        for fixture in ("seq_skip", "seq_repeat"):
            with self.subTest(fixture):
                d = self.opened(fixture, entry=PROTOCOL_FIXTURE)
                self.assertEqual(d.read().width, 4)
                self.assertEqual(category_of(lambda: d.read()), "worker_protocol_error")

    def test_the_parents_limits_hold_even_when_the_worker_ignores_them(self) -> None:
        tight = cfg(limits=ImageLimits(max_width=2, max_height=2, max_image_bytes=4096, max_buffer_bytes=4096))
        d = self.opened("ok_ops", entry=PROTOCOL_FIXTURE, config=tight)            # the fixture publishes a 4x2 image regardless
        self.assertEqual(category_of(lambda: d.read()), "worker_protocol_error")

    def test_the_parent_revalidates_the_pixels_it_builds(self) -> None:
        d = self.opened("ok_bgr")
        image = d._worker.read()                                                    # a valid image from the worker ...
        tight = IsolatedDecoderConfig(limits=ImageLimits(max_width=2, max_height=2, max_image_bytes=64, max_buffer_bytes=64), worker=WORKER)
        d._config = tight                                                           # ... checked against stricter limits by the adapter
        with mock.patch.object(d._worker, "read", return_value=image):
            self.assertEqual(category_of(lambda: d.read()), "dimensions_exceed_limits")
        self.assertEqual(live_worker_count(), 0)

    def test_an_unknown_pixel_format_code_is_refused(self) -> None:
        d = self.opened("ok_bgr")
        image = d._worker.read()
        odd = type(image)(image.seq + 1, image.width, image.height, 9, image.pixels, image.decode_ns)
        with mock.patch.object(d._worker, "read", return_value=odd):
            self.assertEqual(category_of(lambda: d.read()), "unsupported_format")
        self.assertEqual(live_worker_count(), 0)


class TwoWorkersAndLimitTests(DecoderCase):
    def test_main_and_wide_run_side_by_side_and_a_third_is_refused(self) -> None:
        main, wide = self.opened("ok_varying"), self.opened("ok_bgr")
        self.assertEqual(live_worker_count(), 2)
        self.assertEqual(main.read().data, bytes([10]) * 24)
        self.assertEqual(wide.read().data, bytes([7]) * 24)
        third = self.decoder()
        self.assertEqual(category_of(lambda: third.open(endpoint("wide"))), "worker_limit")
        self.assertEqual(live_worker_count(), 2)
        main.close()
        fourth = self.opened()
        self.assertEqual(fourth.read().width, 4)

    def test_a_hung_camera_does_not_hold_the_other_one_back(self) -> None:
        main, wide = self.opened("hang_read", config=cfg(read_timeout_ms=100)), self.opened("ok_varying")
        errors: list = []
        thread = threading.Thread(target=lambda: errors.append(category_of(lambda: main.read())))
        thread.start()
        began = time.monotonic()
        self.assertEqual([wide.read().data[0] for _ in range(3)], [10, 11, 12])
        self.assertLess(time.monotonic() - began, 1.0)
        thread.join(10)
        self.assertEqual(errors, ["read_deadline_exceeded"])

    def test_concurrent_opens_never_exceed_two_workers(self) -> None:
        decoders = [self.decoder("ok_bgr") for _ in range(5)]
        barrier = threading.Barrier(len(decoders))
        outcomes: list = []

        def go(d) -> None:
            barrier.wait()
            try:
                d.open(endpoint())
                outcomes.append("ok")
            except DecoderError as exc:
                outcomes.append(exc.category)

        threads = [threading.Thread(target=go, args=(d,)) for d in decoders]
        for t in threads:
            t.start()
        peak = 0
        while any(t.is_alive() for t in threads):
            peak = max(peak, live_worker_count())
            time.sleep(0.005)
        for t in threads:
            t.join(30)
        self.assertLessEqual(peak, 2)
        self.assertEqual(sorted(outcomes), ["ok", "ok", "worker_limit", "worker_limit", "worker_limit"])

    def test_an_unreapable_worker_keeps_its_slot_and_close_says_so_once(self) -> None:
        holder: list = []

        def factory(*a, **k):
            popen = FakePopen()
            holder.append(popen)
            return popen

        config = cfg(worker=WorkerConfig(start_deadline_s=0.3, terminate_wait_s=0.1, kill_wait_s=0.1, close_graceful_s=0.1))
        d = ProcessIsolatedDecoder(config, camera="main")                           # the real WorkerProcess, a fake process behind it
        self.decoders.append(d)
        with mock.patch.object(process_module.subprocess, "Popen", factory):
            self.assertIn(category_of(lambda: d.open(endpoint())), {"worker_ipc_lost", "worker_handshake_timeout"})
        self.assertEqual(live_worker_count(), 1)                                    # still counted: nothing confirmed its exit
        self.assertEqual(abandoned_worker_count(), 1)
        self.assertEqual(category_of(lambda: d.close()), "containment_failed")      # explicit uncertainty, exactly once
        d.close()
        self.assertIn("process_unreaped", d.uncertain)
        self.assertFalse(d.last_stop_report.slot_released)
        self.assertEqual(reclaim_abandoned_workers(), 0)
        holder[0].code = 0                                                          # only a confirmed exit gives the slot back
        self.assertEqual(reclaim_abandoned_workers(), 1)
        self.assertEqual(live_worker_count(), 0)


class BeforeConnectTests(DecoderCase):
    def test_it_runs_after_ready_and_before_open_with_the_camera(self) -> None:
        seen: list = []
        path = self.report()

        def allow(camera):
            worker = d._worker
            seen.append((camera, worker.state, worker.decoder_open, json.loads(path.read_text())["captures"]))
            return True

        d = self.decoder(report=str(path), before_connect=allow)
        d.open(endpoint("wide"))
        self.assertEqual(seen, [("wide", S.READY, False, [])])                    # the stream was not opened yet
        self.assertGreater(d.timings.before_connect_s, 0)
        self.assertEqual(len(json.loads(path.read_text())["captures"]), 1)

    def test_a_refusal_ends_the_worker_and_open_is_never_sent(self) -> None:
        path = self.report()
        d = self.decoder(report=str(path), before_connect=lambda camera: False)
        self.assertEqual(category_of(lambda: d.open(endpoint())), "readiness_revoked")
        self.assertEqual(json.loads(path.read_text())["captures"], [])            # no VideoCapture was ever constructed
        self.assertIs(d._worker.state, S.TERMINATED)
        self.assertEqual(live_worker_count(), 0)
        self.assertFalse(d._worker.decoder_open)

    def test_an_exception_in_the_check_fails_closed_without_leaking_its_text(self) -> None:
        def broken(camera):
            raise RuntimeError("secret " + ADDRESS)

        d = self.decoder(before_connect=broken)
        with self.assertRaises(DecoderError) as ctx:
            d.open(endpoint())
        self.assertEqual(ctx.exception.category, "readiness_revoked")
        self.assertNotIn("secret", repr(ctx.exception) + str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)
        self.assertIsNone(ctx.exception.__context__)                              # the original exception is not chained, so its text cannot surface
        self.assertEqual(live_worker_count(), 0)

    def test_a_truthy_non_bool_is_not_a_free_pass_for_none(self) -> None:
        for value, expected in ((None, "readiness_revoked"), (0, "readiness_revoked"), ("", "readiness_revoked")):
            d = self.decoder(before_connect=lambda camera, v=value: v)
            self.assertEqual(category_of(lambda: d.open(endpoint())), expected)


class IntermediateLauncherTests(DecoderCase):
    def test_a_differing_header_pid_is_reported_as_a_diagnostic_and_changes_nothing_else(self) -> None:
        d = self.opened("pid_differs")
        self.assertTrue(d.intermediate_launcher)
        self.assertEqual(d.read().width, 4)
        pid = d._worker.pid
        d.close()
        self.assertIn("intermediate_launcher", d.uncertain)
        self.assertEqual(d.last_stop_report.steps, ("close",))                    # containment is exactly as without the token
        self.assertTrue(_gone_within(pid))
        self.assertEqual(live_worker_count(), 0)                                  # the slot follows the confirmed exit of the started process
        self.assertTrue(d.last_stop_report.slot_released)

    def test_the_same_pid_gives_no_token(self) -> None:
        d = self.opened("ok_bgr")
        self.assertFalse(d.intermediate_launcher)
        d.close()
        self.assertNotIn("intermediate_launcher", d.uncertain)


class RedactionTests(DecoderCase):
    SENTINEL = "rtsp://sentinel-host.invalid:8554/secret-path"

    def texts(self, d, exc=None):
        parts = [repr(d), str(d), repr(d.timings), repr(d.last_stop_report), repr(d.uncertain), repr(sorted(vars(d))), repr(d._worker), repr(d._worker.transitions)]
        if exc is not None:
            parts += [repr(exc), str(exc), repr(exc.args), repr(exc.__cause__), repr(exc.__context__)]
        return " ".join(parts)

    def test_the_address_appears_nowhere_on_success(self) -> None:
        d = self.decoder()
        d.open(endpoint(address=self.SENTINEL))
        d.read()
        for needle in ("sentinel-host", "secret-path", "rtsp://"):
            self.assertNotIn(needle, self.texts(d))
        if POSIX and os.path.exists(f"/proc/{d._worker.pid}/cmdline"):
            self.assertNotIn("sentinel-host", Path(f"/proc/{d._worker.pid}/cmdline").read_bytes().decode(errors="replace"))
            self.assertNotIn("sentinel-host", json.dumps(dict(os.environ)))
        d.close()
        self.assertNotIn("sentinel-host", self.texts(d))

    def test_the_address_appears_nowhere_on_failure(self) -> None:
        for fixture, op in (("open_refused", "open"), ("hang_construct", "open"), ("read_exhausted", "read"), ("crash_read", "read")):
            with self.subTest(fixture):
                d = self.decoder(fixture, config=cfg(open_timeout_ms=100, read_timeout_ms=100))
                try:
                    d.open(endpoint(address=self.SENTINEL))
                    d.read()
                except DecoderError as exc:
                    self.assertTrue(exc.__suppress_context__ or exc.__context__ is None)
                    self.assertNotIn("sentinel-host", self.texts(d, exc))
                    self.assertNotIn("secret-path", self.texts(d, exc))
                else:
                    self.fail("no error")

    def test_the_decoder_object_holds_no_copy_of_the_address(self) -> None:
        d = self.opened()
        values = repr([v for k, v in vars(d).items() if k not in {"_worker", "_worker_factory", "_config", "_worker_config"}])
        self.assertNotIn("camera.invalid", values)
        self.assertNotIn("camera.invalid", repr(vars(d._worker)))                  # the worker's own state keeps none either


if __name__ == "__main__":
    unittest.main()
