"""DB-03 Wave 4B-3a: the decoder worker -- ``DecoderHandler`` in process, and as a real process behind a fake ``cv2``.

Offline: no OpenCV, no NumPy, no network, no device. The fake ``cv2`` lives only in the test entry point
``tests/decoder_worker_fixture.py``; production has no fixture selection. The Linux results here do not replace a Windows run.
"""

from __future__ import annotations

import importlib.util
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

from tests.test_isolated_slot import ADDRESS, FAST, POSIX, Isolated, _gone_within
from tsn_dss.engine.device_runtime.errors import InvalidTransition
from tsn_dss.engine.device_runtime.preview_models import ImageLimits, PixelFormat, PreviewPixels
from tsn_dss.engine.opencv_isolated_decoder import (
    DECODER_ENTRY, PRODUCTION_ENTRY, WorkerConfig, WorkerError, WorkerProcess, WorkerState as S, decoder_main,
)
from tsn_dss.engine.opencv_isolated_decoder import protocol as P
from tsn_dss.engine.opencv_isolated_decoder import worker_decoder as WD
from tsn_dss.engine.opencv_isolated_decoder.shm import ParentSegment, crc32_of
from tsn_dss.engine.seestar_preview import DecoderError

ROOT = Path(__file__).resolve().parents[1]
SLOT = 4096


def decode(replies):
    return [P.decode(r) for r in replies]


class Double:
    """A decoder double with the ``ImageDecoder`` shape (open/read/close)."""

    def __init__(self, config=None, *, open_error=None, images=(), read_error=None, close_error=None):
        self.config, self.open_error, self.images = config, open_error, list(images)
        self.read_error, self.close_error = read_error, close_error
        self.endpoints, self.closed = [], 0

    def open(self, endpoint):
        self.endpoints.append(endpoint)
        if self.open_error is not None:
            raise self.open_error

    def read(self):
        if self.read_error is not None:
            raise self.read_error
        return self.images.pop(0) if self.images else None

    def close(self):
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


def pixels(width=4, height=2, fmt=PixelFormat.BGR8, fill=7):
    return PreviewPixels(width, height, fmt, bytes([fill]) * (width * height * fmt.bytes_per_pixel))


class InProcess(unittest.TestCase):
    """The handler with a real ParentSegment in this process (the segment is attached the same way the worker does)."""

    def setUp(self) -> None:
        self.parent = ParentSegment(SLOT)
        self.addCleanup(self.parent.close)
        self.made: list = []

    def handler(self, double=None, init=True, **kw):
        double = double or Double(**kw)
        def factory(config):
            double.config = config
            self.made.append(double)
            return double
        h = WD.DecoderHandler(factory)
        if init:
            (hello,), _ = self.send(h, P.Init(1, 1, 99, self.parent.name, SLOT, 64, 32))
            self.assertEqual(hello.status, 0)
            self.addCleanup(lambda: h.slot and h.slot.close())
        return h, double

    def send(self, h, message):
        replies, done = h.handle(message)
        return decode(replies), done

    def status(self, h, message):
        (reply,), _ = self.send(h, message)
        return P.category_for_status(reply.status) if isinstance(reply, P.Result) else reply

    def open(self, h, op=1):
        return self.send(h, P.Open(op, 5000, 3000, ADDRESS))


class HandlerCommandTests(InProcess):
    def test_open_read_close_happy_path_writes_the_slot_through_the_slot_api(self) -> None:
        h, d = self.handler(images=[pixels(fill=5), pixels(fill=6)])
        (opened,), done = self.open(h)
        self.assertEqual((opened, done), (P.Result(1, 0), False))
        for seq, fill in ((1, 5), (2, 6)):
            (ready,), done = self.send(h, P.Read(10 + seq))
            self.assertIsInstance(ready, P.ImageReady)
            self.assertEqual((ready.op_id, ready.seq, ready.width, ready.height, ready.pixel_format, ready.nbytes), (10 + seq, seq, 4, 2, 3, 24))
            header = self.parent.read_header()
            self.assertEqual((header.begin_seq, header.end_seq, header.nbytes, header.crc32), (seq, seq, 24, crc32_of(bytes([fill]) * 24)))
            self.assertEqual(self.parent.copy_pixels(24), bytes([fill]) * 24)
            self.assertGreater(ready.decode_ns, 0)
        (closed,), done = self.send(h, P.Close(20))
        self.assertEqual((closed, done), (P.Result(20, 0), True))
        self.assertEqual(d.closed, 1)

    def test_gray_is_format_1_and_rgb_is_refused(self) -> None:
        h, _ = self.handler(images=[pixels(fmt=PixelFormat.GRAY8), pixels(fmt=PixelFormat.RGB8)])
        self.open(h)
        (ready,), _ = self.send(h, P.Read(2))
        self.assertEqual((ready.pixel_format, ready.nbytes), (1, 8))
        self.assertEqual(self.status(h, P.Read(3)), "unsupported_format")              # rgb8 cannot come from OpenCV; never put on the wire
        self.assertEqual(self.status(h, P.Read(4)), "invalid_state")                   # and the decoder is dead afterwards

    def test_the_adapter_gets_the_limits_from_init_and_the_timeouts_from_open(self) -> None:
        h, d = self.handler()
        self.send(h, P.Open(1, 4321, 2345, ADDRESS))
        cfg = d.config
        self.assertEqual((cfg.open_timeout_ms, cfg.read_timeout_ms), (4321, 2345))
        self.assertEqual(cfg.limits, ImageLimits(max_width=64, max_height=32, max_image_bytes=SLOT, max_buffer_bytes=SLOT))
        self.assertEqual(len(d.endpoints), 1)
        self.assertEqual(d.endpoints[0].address, ADDRESS)

    def test_sequence_and_state_rules(self) -> None:
        h, _ = self.handler(images=[pixels()])
        self.assertEqual(self.status(h, P.Read(1)), "not_open")                       # READ before OPEN
        self.assertEqual(self.status(h, P.Open(2, 5000, 3000, ADDRESS)), "invalid_state")    # a single-use decoder is dead after any error
        h, _ = self.handler(images=[pixels()])
        self.assertEqual(self.open(h)[0][0], P.Result(1, 0))
        self.assertEqual(self.status(h, P.Open(2, 5000, 3000, ADDRESS)), "invalid_state")    # one stream per worker
        self.assertEqual(self.status(h, P.Read(3)), "invalid_state")                  # dead after the refused second open

    def test_without_a_slot_open_and_read_are_invalid_state(self) -> None:
        h, _ = self.handler(init=False)
        self.send(h, P.Init(1, 1, 5, "", 0, 10, 10))
        self.assertEqual(self.status(h, P.Open(1, 5000, 3000, ADDRESS)), "invalid_state")
        self.assertEqual(self.status(h, P.Read(2)), "invalid_state")

    def test_commands_before_init_end_the_process(self) -> None:
        h = WD.DecoderHandler(lambda c: Double())
        for message in (P.Open(1, 5000, 3000, ADDRESS), P.Read(1), P.Close(1), P.Ping(1)):
            with mock.patch.object(WD.os, "_exit", side_effect=SystemExit) as ex, self.assertRaises(SystemExit):
                h.handle(message)
            ex.assert_called_once_with(WD.EXIT_PROTOCOL)

    def test_a_read_that_returns_nothing_is_read_failed(self) -> None:
        h, _ = self.handler(images=[])
        self.open(h)
        self.assertEqual(self.status(h, P.Read(2)), "read_failed")

    def test_oversize_and_wrong_types_are_refused_before_the_slot(self) -> None:
        big = PreviewPixels(64, 32, PixelFormat.BGR8, bytes(64 * 32 * 3))                # 6144 > slot 4096
        h, _ = self.handler(images=[big])
        self.open(h)
        self.assertEqual(self.status(h, P.Read(2)), "image_exceeds_limits")
        self.assertEqual(self.parent.read_header().begin_seq, 0)                          # the slot was never touched
        odd = mock.Mock(pixel_format=PixelFormat.BGR8, data=bytearray(24), width=4, height=2)
        h2, _ = self.handler(images=[odd])
        self.open(h2)
        self.assertEqual(self.status(h2, P.Read(2)), "decoder_bad_output")

    def test_close_without_open_and_a_failing_release(self) -> None:
        h, d = self.handler()
        (closed,), done = self.send(h, P.Close(1))
        self.assertEqual((closed, done), (P.Result(1, 0), True))
        self.assertEqual(d.closed, 0)
        h2, d2 = self.handler(close_error=RuntimeError("x"))
        self.open(h2)
        (closed,), done = self.send(h2, P.Close(2))
        self.assertEqual((P.category_for_status(closed.status), done), ("release_failed", True))
        self.assertEqual(d2.closed, 1)


class HandlerStatusTests(InProcess):
    def test_every_table_category_maps_to_its_code(self) -> None:
        for code, category in P.STATUS_TABLE.items():
            with self.subTest(category):
                h, _ = self.handler(open_error=DecoderError(category))
                (reply,), _ = self.open(h)
                self.assertEqual(reply.status, code)

    def test_read_errors_map_the_same_way(self) -> None:
        for category in ("read_failed", "unsupported_depth", "dimensions_exceed_limits", "image_exceeds_limits", "decoder_bad_output"):
            h, _ = self.handler(read_error=DecoderError(category))
            self.open(h)
            self.assertEqual(self.status(h, P.Read(2)), category)

    def test_anything_outside_the_table_is_worker_internal(self) -> None:
        for error in (DecoderError("dimensions_out_of_range"), DecoderError("image_too_large"), DecoderError("data_not_bytes"),
                      DecoderError("pixels_missing"), DecoderError("decoder_error"), RuntimeError("boom"), ValueError(), KeyError("k")):
            with self.subTest(repr(error)):
                h, _ = self.handler(open_error=error)
                (reply,), _ = self.open(h)
                self.assertEqual(reply.status, int(P.Status.WORKER_INTERNAL))

    def test_an_unexpected_exception_never_escapes_the_handler(self) -> None:
        h, d = self.handler()
        d.open = mock.Mock(side_effect=BaseException.__new__(RuntimeError))
        self.assertEqual(self.status(h, P.Open(1, 5000, 3000, ADDRESS)), "worker_internal")

    def test_no_text_crosses_the_wire(self) -> None:
        h, _ = self.handler(open_error=DecoderError("open_failed"))
        replies, _ = h.handle(P.Open(1, 5000, 3000, ADDRESS))
        for raw in replies:
            self.assertNotIn(b"camera", raw)
            self.assertNotIn(b"rtsp", raw)
            self.assertLessEqual(len(raw), 16)

    def test_the_handler_keeps_no_copy_of_the_address(self) -> None:
        h, _ = self.handler(images=[pixels()])
        self.open(h)
        self.send(h, P.Read(2))
        self.assertNotIn(ADDRESS, repr(vars(h)).replace(repr(h._decoder), ""))
        self.assertNotIn("camera.invalid", repr(h))


class IsolationOfImportsTests(unittest.TestCase):
    def run_code(self, code: str):
        result = subprocess.run([sys.executable, "-P", "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60,
                                env={**os.environ, "PYTHONPATH": str(ROOT)})
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_importing_the_decoder_modules_loads_no_opencv_numpy_or_wave4(self) -> None:
        out = self.run_code(
            "import sys\nimport tsn_dss.engine.opencv_isolated_decoder.decoder_main\nimport tsn_dss.engine.opencv_isolated_decoder.worker_decoder\n"
            "print(sorted(m for m in sys.modules if m.split('.')[0] in {'cv2','numpy','PIL','av'} or m.startswith('tsn_dss.engine.opencv_preview_decoder')))")
        self.assertEqual(out, "[]")

    def test_the_handshake_only_entry_is_unchanged_and_still_has_no_decoder(self) -> None:
        self.assertEqual(PRODUCTION_ENTRY, "tsn_dss.engine.opencv_isolated_decoder.worker_main")
        self.assertEqual(DECODER_ENTRY, "tsn_dss.engine.opencv_isolated_decoder.decoder_main")
        out = self.run_code("import tsn_dss.engine.opencv_isolated_decoder.worker_main as m\nprint(hasattr(m, 'DecoderHandler'))")
        self.assertEqual(out, "False")


class EntryPointTests(unittest.TestCase):
    def test_bad_arguments_exit_with_64_like_the_handshake_only_worker(self) -> None:
        for argv in ([], ["--ctl", "x", "--proto", "1"], ["--ctl", "5", "--proto", "2"], ["--ctl", "-1", "--proto", "1"], ["--proto", "1"]):
            self.assertEqual(decoder_main.main(argv), 64, argv)

    def test_the_config_flag_selects_the_entry_and_needs_a_slot(self) -> None:
        self.assertEqual(WorkerProcess(WorkerConfig(**FAST))._entry, PRODUCTION_ENTRY)
        self.assertEqual(WorkerProcess(WorkerConfig(**{**FAST, "decoder_worker": True}))._entry, DECODER_ENTRY)
        with self.assertRaises(ValueError):
            WorkerConfig(decoder_worker=True)                      # slot_bytes == 0
        with self.assertRaises(ValueError):
            WorkerConfig(**{**FAST, "decoder_worker": 1})
        self.assertFalse(WorkerConfig().decoder_worker)


# --- real processes: the real handler and adapter behind a fake cv2 -----------------------------------------------------------------


class RealProcess(Isolated):
    def worker(self, fixture: str, report: str | None = None, **override) -> WorkerProcess:
        extra = ("--fixture", fixture) + (("--report", report) if report else ())
        w = WorkerProcess(WorkerConfig(**{**FAST, "max_width": 64, "max_height": 64, **override}),
                          _entry_module="tests.decoder_worker_fixture", _extra_args=extra)
        self.workers.append(w)
        w.start()
        return w

    def report(self, name="report.json"):
        return Path(tempfile.mkdtemp()) / name


class DecoderWorkerFlowTests(RealProcess):
    def test_bgr_images_flow_end_to_end_through_the_real_handler_and_adapter(self) -> None:
        w = self.worker("ok_varying")
        w.open(ADDRESS, 1000, 1000)
        self.assertTrue(w.decoder_open)
        for seq in (1, 2, 3):
            image = w.read()
            self.assertEqual((image.seq, image.width, image.height, image.pixel_format), (seq, 4, 2, 3))
            self.assertEqual(image.pixels, bytes([10 + seq - 1]) * 24)

    def test_gray_images_use_format_1(self) -> None:
        w = self.worker("ok_gray")
        w.open(ADDRESS, 1000, 1000)
        image = w.read()
        self.assertEqual((image.pixel_format, image.width, image.height, len(image.pixels)), (1, 4, 2, 8))

    def test_the_fake_capture_receives_the_address_once_the_ffmpeg_backend_and_the_timeouts(self) -> None:
        path = self.report()
        w = self.worker("ok_bgr", str(path))
        w.open(ADDRESS, 4321, 2345)
        w.read()
        data = json.loads(path.read_text())
        self.assertEqual(len(data["captures"]), 1)
        capture = data["captures"][0]
        self.assertEqual((capture["address"], capture["api"], capture["params"]), (ADDRESS, 1900, [53, 4321, 54, 2345]))
        self.assertEqual(capture["reads"], 1)
        self.assertNotIn(ADDRESS, json.dumps(data["argv"]))
        w.stop()
        data = json.loads(path.read_text())
        self.assertEqual((data["stage"], data["captures"][0]["released"]), ("closed", 1))        # CLOSE released the capture exactly once

    def test_nothing_of_wave_4_or_opencv_is_imported_before_open(self) -> None:
        path = self.report()
        w = self.worker("ok_bgr", str(path))
        before = json.loads(path.read_text())
        self.assertEqual((before["stage"], before["wave4_imported"], before["cv2_imported"]), ("init_seen", False, False))
        w.ping()
        w.open(ADDRESS, 1000, 1000)
        after = json.loads(path.read_text())
        self.assertTrue(after["wave4_imported"])
        self.assertFalse(after["cv2_imported"])                                                     # the fake module is never registered as cv2

    def test_open_failures_arrive_as_their_wave_4_category(self) -> None:
        for fixture, category in (("open_refused", "open_failed"), ("backend_mismatch", "backend_mismatch"),
                                  ("backend_error", "backend_unverified"), ("version_old", "opencv_version_unsupported"),
                                  ("version_unknown", "opencv_version_unknown"), ("construct_error", "open_failed"),
                                  ("loader_missing", "opencv_unavailable")):
            with self.subTest(fixture):
                w = self.worker(fixture)
                with self.assertRaises(WorkerError) as ctx:
                    w.open(ADDRESS, 1000, 1000)
                self.assertEqual(ctx.exception.category, category)
                self.assertFalse(w.decoder_open)
                self.assertIs(w.state, S.TERMINATED)
                self.assertNotIn("camera.invalid", repr(ctx.exception) + repr(w))

    def test_read_failures_arrive_as_their_wave_4_category(self) -> None:
        for fixture, category, extra in (("read_exhausted", "read_failed", {}), ("none_frame", "decoder_bad_output", {}),
                                         ("float_frame", "unsupported_depth", {}), ("four_channels", "unsupported_format", {}),
                                         ("too_wide", "dimensions_exceed_limits", {"max_width": 8, "max_height": 8}),
                                         ("too_big", "image_exceeds_limits", {})):
            with self.subTest(fixture):
                w = self.worker(fixture, **extra)
                w.open(ADDRESS, 1000, 1000)
                with self.assertRaises(WorkerError) as ctx:
                    w.read()
                self.assertEqual(ctx.exception.category, category)
                self.assertIs(w.state, S.TERMINATED)

    def test_the_parent_limits_catch_an_image_the_worker_was_not_asked_to_refuse(self) -> None:
        """The parent keeps its own limits: a frame larger than the parent's slot can never be accepted."""
        w = self.worker("too_big", slot_bytes=SLOT)
        w.open(ADDRESS, 1000, 1000)
        with self.assertRaises(WorkerError):
            w.read()

    def test_not_open_and_invalid_state_answers_leave_no_orphan_and_free_the_slot(self) -> None:
        """Raw misuse of the wire (the public API refuses it before sending): the worker answers with a status, nothing else changes."""
        from tsn_dss.engine.opencv_isolated_decoder import live_worker_count
        w = self.worker("ok_bgr")
        deadline = time.monotonic() + 3
        w._send(P.Read(900))                                                       # READ before OPEN
        self.assertEqual(P.category_for_status(w._expect(P.Result, deadline, "x").status), "not_open")
        w._send(P.Open(901, 1000, 1000, ADDRESS))                                  # the decoder is dead after the refusal
        self.assertEqual(P.category_for_status(w._expect(P.Result, deadline, "x").status), "invalid_state")
        w._send(P.Ping(902))                                                       # the command loop itself is still alive
        self.assertEqual(w._expect(P.Pong, deadline, "x").op_id, 902)
        self.assertEqual(live_worker_count(), 1)                                   # still counted while it runs
        pid, worker_pid = w.pid, w.worker_pid
        report = w.stop()
        self.assertEqual((report.steps, report.exit_code, set(report.uncertain) - {"intermediate_launcher"}), (("close",), 0, set()))
        self.assertTrue(_gone_within(pid) and _gone_within(worker_pid))
        self.assertEqual(live_worker_count(), 0)                                   # released only after the confirmed exit
        self.assertTrue(report.slot_released and report.segment_released)

    def test_a_failing_release_does_not_hold_the_stop_back(self) -> None:
        w = self.worker("release_error")
        w.open(ADDRESS, 1000, 1000)
        began = time.monotonic()
        report = w.stop()
        self.assertLess(time.monotonic() - began, 2.0)
        self.assertEqual(report.steps, ("close",))
        self.assertIs(w.state, S.TERMINATED)

    def test_the_address_appears_nowhere_in_the_parents_view_of_the_worker(self) -> None:
        w = self.worker("ok_bgr")
        w.open(ADDRESS, 1000, 1000)
        w.read()
        text = " ".join([repr(w), repr(w.transitions)])
        self.assertNotIn("camera.invalid", text)
        if POSIX and os.path.exists(f"/proc/{w.pid}/cmdline"):
            self.assertNotIn("camera.invalid", Path(f"/proc/{w.pid}/cmdline").read_bytes().decode(errors="replace"))
        self.assertNotIn("camera.invalid", repr(w.stop()))


class DecoderWorkerContainmentTests(RealProcess):
    def contained(self, fixture, category, operation, budget):
        w = self.worker(fixture)
        if operation == "read":
            w.open(ADDRESS, 1000, 1000)
        began = time.monotonic()
        with self.assertRaises(WorkerError) as ctx:
            w.open(ADDRESS, 100, 100) if operation == "open" else w.read()
        elapsed = time.monotonic() - began
        self.assertEqual(ctx.exception.category, category)
        self.assertLess(elapsed, budget, f"{fixture}: the parent was blocked for {elapsed:.2f}s")
        self.assertIs(w.state, S.TERMINATED)
        self.assertTrue(_gone_within(w.pid) and _gone_within(w.worker_pid), "the hung worker is still running")
        self.assertFalse(w.abandoned)

    def test_a_capture_that_blocks_in_read_is_ended_at_the_hard_deadline(self) -> None:
        self.contained("hang_read", "read_deadline_exceeded", "read", 1.0 + 0.3 + 0.3 + 0.5 + 0.5 + 1.0)

    def test_a_capture_spinning_in_read_with_the_gil_held_is_ended_too(self) -> None:
        self.contained("spin_read", "read_deadline_exceeded", "read", 4.0)

    def test_a_constructor_that_never_returns_is_ended_at_the_open_deadline(self) -> None:
        self.contained("hang_construct", "open_deadline_exceeded", "open", 0.1 + 0.3 + 0.3 + 0.5 + 0.5 + 1.5)

    def test_a_crash_inside_read_is_reported_as_a_crash(self) -> None:
        self.contained("crash_read", "worker_crashed", "read", 3.0)

    def test_a_hung_camera_does_not_stop_the_other_one(self) -> None:
        main = self.worker("hang_read")
        wide = self.worker("ok_varying")
        main.open(ADDRESS, 1000, 1000)
        wide.open(ADDRESS, 1000, 1000)
        errors: list = []

        def hung() -> None:
            try:
                main.read()
            except WorkerError as exc:
                errors.append(exc)

        thread = threading.Thread(target=hung)
        thread.start()
        began = time.monotonic()
        images = [wide.read() for _ in range(3)]
        self.assertLess(time.monotonic() - began, 1.0)                                  # the other worker kept delivering meanwhile
        self.assertEqual([i.seq for i in images], [1, 2, 3])
        thread.join(10)
        self.assertEqual(errors[0].category, "read_deadline_exceeded")
        self.assertIs(wide.state, S.READY)


class ProductionDecoderEntryTests(Isolated):
    def test_the_production_decoder_entry_starts_answers_ping_and_has_no_opencv_here(self) -> None:
        w = WorkerProcess(WorkerConfig(**{**FAST, "decoder_worker": True}))
        self.workers.append(w)
        w.start()
        w.ping()
        self.assertIs(w.state, S.READY)
        self.assertFalse(w.decoder_open)
        if importlib.util.find_spec("cv2") is not None:
            self.skipTest("OpenCV is installed here: OPEN would attempt a real connection, which these tests never do")
        with self.assertRaises(WorkerError) as ctx:
            w.open(ADDRESS, 1000, 1000)
        self.assertEqual(ctx.exception.category, "opencv_unavailable")
        self.assertFalse(w.decoder_open)


if __name__ == "__main__":
    unittest.main()
