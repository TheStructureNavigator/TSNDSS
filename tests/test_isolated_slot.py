"""DB-03 Wave 4B-2: the shared slot, the parent-enforced operations, slot integrity, and shared-memory cleanup.

Everything here is offline: real worker processes (``tests.process_fixtures``), no OpenCV, no network, no device. The
production worker has no decoder yet, so ``open``/``read`` are exercised only through fixture workers; what is tested is the
parent's side: deadlines, validation, copying and cleanup. The Linux results do not stand in for a Windows run.
"""

from __future__ import annotations

import os
import re
import struct
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tsn_dss.engine.device_runtime.errors import InvalidTransition
from tsn_dss.engine.opencv_isolated_decoder import (
    WorkerConfig, WorkerError, WorkerImage, WorkerProcess, WorkerState as S, live_worker_count,
)
from tsn_dss.engine.opencv_isolated_decoder import process as process_module
from tsn_dss.engine.opencv_isolated_decoder import protocol as P
from tsn_dss.engine.opencv_isolated_decoder import segment as seg
from tsn_dss.engine.opencv_isolated_decoder import shm as shm_module
from tsn_dss.engine.opencv_isolated_decoder.shm import ParentSegment, SegmentClosed, SegmentUnavailable, WorkerSegment, crc32_of

ROOT = Path(__file__).resolve().parents[1]
POSIX = sys.platform != "win32"
SLOT = 4096
FAST = dict(start_deadline_s=3.0, ping_deadline_s=0.5, close_graceful_s=0.3, terminate_wait_s=0.5, kill_wait_s=0.5,
            open_margin_s=0.3, read_margin_s=0.3, slot_bytes=SLOT)
ADDRESS = "rtsp://camera.invalid/stream"


def config(**override) -> WorkerConfig:
    return WorkerConfig(**{**FAST, **override})


def make(fixture: str, **override) -> WorkerProcess:
    return WorkerProcess(config(**override), _entry_module="tests.process_fixtures", _extra_args=("--fixture", fixture))


def shm_names() -> set | None:
    """Names of our segments in /dev/shm (32 hex digits). None where the platform has no such directory."""
    if not os.path.isdir("/dev/shm"):
        return None
    return {n for n in os.listdir("/dev/shm") if re.fullmatch(r"[0-9a-f]{32}", n)}


class Isolated(unittest.TestCase):
    """Every test must leave no worker, no slot and no segment behind."""

    def setUp(self) -> None:
        self.workers: list = []
        self.slots_before = live_worker_count()
        self.names_before = shm_names()

    def tearDown(self) -> None:
        for w in self.workers:
            w.stop()
        self.assertEqual(live_worker_count(), self.slots_before, "a worker slot was leaked")
        if self.names_before is not None:
            self.assertEqual(shm_names() - self.names_before, set(), "a shared segment was leaked")

    def started(self, fixture: str, **override) -> WorkerProcess:
        w = make(fixture, **override)
        self.workers.append(w)
        w.start()
        return w

    def opened(self, fixture: str, **override) -> WorkerProcess:
        w = self.started(fixture, **override)
        w.open(ADDRESS, 1000, 1000)
        return w

    def assertGone(self, w: WorkerProcess) -> None:
        self.assertIs(w.state, S.TERMINATED)
        self.assertTrue(w.popen_released)
        self.assertIsNone(w._segment)
        self.assertIsNone(w._conn)


# --- the segment object itself (no process) -----------------------------------------------------------------------------


class ParentSegmentTests(unittest.TestCase):
    def test_the_parent_creates_a_segment_with_a_valid_empty_header(self) -> None:
        segment = ParentSegment(1000)
        try:
            header = segment.read_header()
            self.assertEqual((header.slot_bytes, header.handshake_nonce, header.begin_seq, header.end_seq, header.layout_version), (1000, 0, 0, 0, 2))
        finally:
            segment.close()

    def test_close_and_unlink_are_idempotent_and_close_unlinks(self) -> None:
        before = shm_names()
        segment = ParentSegment(1000)
        if before is not None:
            self.assertEqual(len(shm_names() - before), 1 if POSIX else 0)
        segment.unlink_name()
        segment.unlink_name()
        self.assertTrue(segment.name_unlinked)
        self.assertTrue(segment.close())
        self.assertTrue(segment.close())
        self.assertTrue(segment.closed)
        if before is not None:
            self.assertEqual(shm_names() - before, set())
        with self.assertRaises(SegmentClosed):
            segment.read_header()
        with self.assertRaises(SegmentClosed):
            segment.copy_pixels(1)

    def test_a_close_without_an_explicit_unlink_removes_the_name(self) -> None:
        before = shm_names()
        segment = ParentSegment(1000)
        segment.close()
        if before is not None:
            self.assertEqual(shm_names() - before, set())

    def test_the_copy_belongs_to_the_parent(self) -> None:
        segment = ParentSegment(1000)
        try:
            worker = WorkerSegment.attach(segment.name, 1000)
            worker.publish(1, 4, 2, 3, bytes(range(24)))
            copy = segment.copy_pixels(24)
            self.assertIsInstance(copy, bytes)
            worker.buffer[64] = 99                                         # the worker writes after the copy
            self.assertEqual(copy, bytes(range(24)))                       # the parent's data does not change
            self.assertEqual(segment.copy_pixels(1), b"\x63")
            worker.close()
            self.assertTrue(segment.close(), "an exported view of the segment was left behind")
        finally:
            segment.close()

    def test_copy_ranges_are_checked(self) -> None:
        segment = ParentSegment(100)
        try:
            for bad in (0, -1, 101, True, 1.5, None):
                with self.assertRaises(P.ProtocolError, msg=repr(bad)):
                    segment.copy_pixels(bad)
        finally:
            segment.close()

    def test_a_slot_size_outside_the_range_is_refused(self) -> None:
        for bad in (0, -5, 10 ** 10):
            with self.assertRaises(P.ProtocolError):
                ParentSegment(bad)

    def test_the_repr_has_no_name(self) -> None:
        segment = ParentSegment(100)
        try:
            self.assertNotIn(segment.name, repr(segment))
        finally:
            segment.close()


class WorkerSegmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parent = ParentSegment(1000)
        self.addCleanup(self.parent.close)

    def test_attach_records_the_handshake_in_the_shared_header(self) -> None:
        worker = WorkerSegment.attach(self.parent.name, 1000)
        worker.record_handshake(0xABCDEF0123456789, 4242)
        header = self.parent.read_header()
        self.assertEqual((header.handshake_nonce, header.worker_pid), (0xABCDEF0123456789, 4242))
        worker.close()

    def test_attach_refuses_what_is_not_a_valid_segment(self) -> None:
        with self.assertRaises(SegmentUnavailable):
            WorkerSegment.attach("no_such_segment_0123456789", 1000)
        with self.assertRaises(SegmentUnavailable):
            WorkerSegment.attach(self.parent.name, 999)                    # the parent's header says 1000
        buf = self.parent._shm.buf
        buf[0:4] = b"XXXX"
        with self.assertRaises(SegmentUnavailable):
            WorkerSegment.attach(self.parent.name, 1000)
        buf[0:4] = b"TDS1"
        struct.pack_into("<H", buf, 4, 1)                                 # a v1 header: no implicit compatibility
        with self.assertRaises(SegmentUnavailable):
            WorkerSegment.attach(self.parent.name, 1000)

    def test_publish_writes_fences_metadata_pixels_and_crc_in_a_consistent_header(self) -> None:
        worker = WorkerSegment.attach(self.parent.name, 1000)
        pixels = bytes(range(24))
        worker.publish(5, 4, 2, 3, pixels)
        header = self.parent.read_header()
        self.assertEqual((header.begin_seq, header.end_seq, header.width, header.height, header.pixel_format, header.nbytes),
                         (5, 5, 4, 2, 3, 24))
        self.assertEqual(header.crc32, crc32_of(pixels))
        self.assertTrue(seg.fence_is_stable(header, 5))
        worker.close()

    def test_publish_refuses_an_inconsistent_image(self) -> None:
        worker = WorkerSegment.attach(self.parent.name, 1000)
        for args in ((1, 4, 2, 3, bytes(23)), (1, 4, 2, 1, bytes(24)), (1, 40, 30, 1, bytes(1200)), (1, 4, 2, 3, b"")):
            with self.assertRaises(P.ProtocolError, msg=args[:4]):
                worker.publish(*args)
        worker.close()

    @unittest.skipUnless(POSIX and os.path.isdir("/dev/shm"), "POSIX shared-memory names")
    def test_a_worker_process_that_attaches_and_exits_does_not_destroy_the_segment(self) -> None:
        """track=False: the worker's own resource tracker must not unlink the parent's segment when the worker exits."""
        code = ("from tsn_dss.engine.opencv_isolated_decoder.shm import WorkerSegment\n"
                f"w = WorkerSegment.attach({self.parent.name!r}, 1000)\nw.record_handshake(7, 8)\nw.close()\n")
        result = subprocess.run([sys.executable, "-P", "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60,
                                env={**os.environ, "PYTHONPATH": str(ROOT)})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "", "resource tracker warnings in the worker")
        self.assertTrue((Path("/dev/shm") / self.parent.name).exists())
        self.assertEqual(self.parent.read_header().handshake_nonce, 7)


# --- handshake with the segment ---------------------------------------------------------------------------------------


class HandshakeSegmentTests(Isolated):
    def test_ready_means_attached_and_verified_not_opened(self) -> None:
        w = self.started("ok_ops")
        self.assertIs(w.state, S.READY)
        self.assertFalse(w.decoder_open)
        self.assertIsNotNone(w.worker_pid)
        self.assertTrue(w._segment.name_unlinked)                           # POSIX: the name is gone once the handshake is complete
        if self.names_before is not None:
            self.assertEqual(shm_names() - self.names_before, set())

    def test_the_production_worker_attaches_and_answers_ping(self) -> None:
        w = WorkerProcess(config())                                         # production entry: no fixture
        self.workers.append(w)
        w.start()
        w.ping()
        self.assertIsNotNone(w.worker_pid)
        if POSIX:                                                           # never required on Windows: a venv launcher can sit in between
            self.assertEqual(w.worker_pid, w.pid)
        self.assertFalse(w.decoder_open)

    def test_the_production_worker_has_no_decoder(self) -> None:
        w = WorkerProcess(config())
        self.workers.append(w)
        w.start()
        with self.assertRaises(WorkerError) as ctx:
            w.open(ADDRESS, 1000, 1000)
        self.assertEqual(ctx.exception.category, "invalid_state")           # the worker's answer to OPEN until 4B-3
        self.assertFalse(w.decoder_open)
        self.assertGone(w)

    def test_a_worker_that_never_attached_is_not_ready(self) -> None:
        w = make("no_attach")
        self.workers.append(w)
        with self.assertRaises(WorkerError) as ctx:
            w.start()
        self.assertEqual(ctx.exception.category, "worker_protocol_error")   # its nonce is not in the shared header
        self.assertGone(w)

    def test_a_wrong_nonce_in_the_shared_header_is_not_ready(self) -> None:
        w = make("header_nonce_wrong")
        self.workers.append(w)
        with self.assertRaises(WorkerError) as ctx:
            w.start()
        self.assertEqual(ctx.exception.category, "worker_protocol_error")
        self.assertGone(w)

    def test_a_worker_that_cannot_attach_reports_shm_unavailable(self) -> None:
        w = make("shm_refused")
        self.workers.append(w)
        with self.assertRaises(WorkerError) as ctx:
            w.start()
        self.assertEqual(ctx.exception.category, "shm_unavailable")
        self.assertGone(w)

    def test_a_worker_that_dies_right_after_attaching_leaves_nothing(self) -> None:
        w = make("exit_after_attach")
        self.workers.append(w)
        with self.assertRaises(WorkerError) as ctx:
            w.start()
        self.assertIn(ctx.exception.category, {"worker_crashed", "worker_ipc_lost"})
        self.assertGone(w)

    def test_a_worker_that_never_answers_leaves_no_segment(self) -> None:
        w = make("no_hello", start_deadline_s=0.5)
        self.workers.append(w)
        with self.assertRaises(WorkerError) as ctx:
            w.start()
        self.assertEqual(ctx.exception.category, "worker_handshake_timeout")
        self.assertGone(w)

    def test_a_segment_that_cannot_be_created_spawns_nothing(self) -> None:
        w = make("ok_ops")
        self.workers.append(w)
        with mock.patch.object(process_module, "ParentSegment", side_effect=SegmentUnavailable), \
                mock.patch.object(process_module.subprocess, "Popen", side_effect=AssertionError("spawned")):
            with self.assertRaises(WorkerError) as ctx:
                w.start()
        self.assertEqual(ctx.exception.category, "shm_unavailable")
        self.assertGone(w)

    def test_a_spawn_failure_after_the_segment_was_created_removes_the_segment(self) -> None:
        w = make("ok_ops")
        self.workers.append(w)
        with mock.patch.object(process_module.subprocess, "Popen", side_effect=OSError("boom")):
            with self.assertRaises(WorkerError) as ctx:
                w.start()
        self.assertEqual(ctx.exception.category, "worker_start_failed")
        self.assertGone(w)

    def test_without_a_slot_the_launcher_behaves_as_in_4b1(self) -> None:
        w = self.started("ok", slot_bytes=0)
        self.assertIsNone(w._segment)
        w.ping()
        with self.assertRaises(InvalidTransition):
            w.read()


# --- operations and deadlines --------------------------------------------------------------------------------------------


class OperationTests(Isolated):
    def test_open_then_reads_deliver_parent_owned_images(self) -> None:
        w = self.opened("ok_ops")
        self.assertTrue(w.decoder_open)
        for seq in (1, 2, 3):
            image = w.read()
            self.assertIsInstance(image, WorkerImage)
            self.assertIsInstance(image.pixels, bytes)
            self.assertEqual((image.seq, image.width, image.height, image.pixel_format), (seq, 4, 2, 3))
            self.assertEqual(image.pixels, bytes((seq * 7 + i) % 251 for i in range(24)))
            self.assertEqual(image.decode_ns, 123)
        self.assertIs(w.state, S.READY)

    def test_a_stream_is_not_open_just_because_the_worker_is_ready(self) -> None:
        w = self.started("ok_ops")
        self.assertFalse(w.decoder_open)
        with self.assertRaises(InvalidTransition):
            w.read()
        self.assertIs(w.state, S.READY)                                       # a misuse does not fail the worker
        w.open(ADDRESS, 1000, 1000)
        self.assertTrue(w.decoder_open)
        with self.assertRaises(InvalidTransition):
            w.open(ADDRESS, 1000, 1000)
        self.assertIs(w.state, S.READY)

    def test_invalid_open_arguments_are_a_caller_error_not_a_worker_failure(self) -> None:
        w = self.started("ok_ops")
        for args in (("http://x.invalid/a", 1000, 1000), ("rtsp://x.invalid/a", 5, 1000), ("rtsp://x.invalid/a", 1000, 10 ** 9), ("", 1000, 1000)):
            with self.assertRaises(ValueError):
                w.open(*args)
        self.assertIs(w.state, S.READY)
        self.assertFalse(w.decoder_open)

    def test_open_failures_map_to_their_category_and_end_the_worker(self) -> None:
        for fixture, category in (("open_error", "open_failed"), ("open_unknown_status", "worker_protocol_error"),
                                  ("open_wrong_op", "worker_protocol_error"), ("open_image_instead", "worker_protocol_error"),
                                  ("crash_open", "worker_crashed")):
            with self.subTest(fixture):
                w = self.started(fixture)
                with self.assertRaises(WorkerError) as ctx:
                    w.open(ADDRESS, 1000, 1000)
                self.assertEqual(ctx.exception.category, category)
                self.assertFalse(w.decoder_open)
                self.assertEqual(w.failure_category, category)
                self.assertGone(w)

    def test_read_failures_map_to_their_category_and_end_the_worker(self) -> None:
        for fixture, category in (("read_error", "read_failed"), ("read_ok_status", "worker_protocol_error"),
                                  ("read_wrong_op", "worker_protocol_error"), ("image_wrong_op", "worker_protocol_error"),
                                  ("garbage_after_open", "worker_protocol_error"), ("crash_read", "worker_crashed"),
                                  ("exit_after_open_read", "worker_crashed")):
            with self.subTest(fixture):
                w = self.opened(fixture)
                with self.assertRaises(WorkerError) as ctx:
                    w.read()
                self.assertEqual(ctx.exception.category, category)
                self.assertEqual(w.failure_category, category)
                self.assertGone(w)

    def hard_deadline(self, fixture: str, operation, category: str, budget: float) -> None:
        w = self.started(fixture)
        began = time.monotonic()
        with self.assertRaises(WorkerError) as ctx:
            operation(w)
        elapsed = time.monotonic() - began
        self.assertEqual(ctx.exception.category, category)
        self.assertLess(elapsed, budget, f"the parent was blocked for {elapsed:.2f}s")
        self.assertGreater(elapsed, 0.2)
        self.assertGone(w)
        self.assertFalse(w.abandoned)
        pid = w.pid
        self.assertFalse(_alive(pid), "the hung worker is still running")
        self.assertTrue(_gone_within(w.worker_pid), "the process that wrote the shared header (the real worker) is still running")

    def test_a_hung_open_is_ended_at_the_hard_deadline(self) -> None:
        self.hard_deadline("hang_open", lambda w: w.open(ADDRESS, 100, 100), "open_deadline_exceeded", 0.4 + 0.3 + 0.5 + 0.5 + 1.5)

    def test_a_worker_spinning_in_open_while_holding_the_gil_is_ended_too(self) -> None:
        self.hard_deadline("spin_open", lambda w: w.open(ADDRESS, 100, 100), "open_deadline_exceeded", 4.0)

    def test_a_hung_read_is_ended_at_the_hard_deadline(self) -> None:
        w = self.opened("hang_read")
        began = time.monotonic()
        with self.assertRaises(WorkerError) as ctx:
            w.read()
        self.assertEqual(ctx.exception.category, "read_deadline_exceeded")
        self.assertLess(time.monotonic() - began, 1.0 + 0.3 + 0.3 + 0.5 + 0.5 + 1.0)
        self.assertGone(w)
        self.assertFalse(_alive(w.pid))
        self.assertTrue(_gone_within(w.worker_pid), "the real worker is still running")

    def test_a_worker_spinning_in_read_is_ended_too(self) -> None:
        w = self.opened("spin_read")
        with self.assertRaises(WorkerError) as ctx:
            w.read()
        self.assertEqual(ctx.exception.category, "read_deadline_exceeded")
        self.assertGone(w)
        self.assertTrue(_gone_within(w.worker_pid), "a worker spinning with the GIL held outlived its containment")

    def test_a_worker_that_answers_nothing_to_read_hits_the_read_deadline(self) -> None:
        w = self.opened("no_image")
        with self.assertRaises(WorkerError) as ctx:
            w.read()
        self.assertEqual(ctx.exception.category, "read_deadline_exceeded")
        self.assertGone(w)

    def test_a_slow_but_alive_open_succeeds_inside_its_deadline(self) -> None:
        w = self.started("slow_open")
        began = time.monotonic()
        w.open(ADDRESS, 1000, 1000)
        self.assertGreater(time.monotonic() - began, 0.35)
        self.assertTrue(w.decoder_open)

    def test_the_hard_deadline_follows_the_requested_timeouts(self) -> None:
        """OPEN ends at open_timeout + open_margin (D12). A 1.2 s timeout must not be cut at 0.4 s."""
        w = self.started("slow_open")
        w.open(ADDRESS, 1200, 1000)                                           # replies after 0.4 s: well inside 1.2 s + margin
        self.assertTrue(w.decoder_open)

    def test_a_killed_open_stream_is_reported_as_uncertain(self) -> None:
        w = self.opened("stuck_stream_open")
        with self.assertRaises(WorkerError):
            w.read()
        report = w.stop()
        self.assertIn("stream_terminated", report.uncertain)                 # the stream was open and the process was ended by force (U5)
        self.assertTrue(report.segment_released)
        self.assertTrue(report.slot_released)
        self.assertFalse(report.containment_failed)

    def test_a_graceful_close_of_an_open_stream_is_not_uncertain(self) -> None:
        w = self.opened("ok_ops")
        report = w.stop()
        self.assertEqual(report.uncertain, ())
        self.assertEqual(report.steps, ("close",))
        self.assertEqual(report.exit_code, 0)

    def test_a_stream_ended_by_force_is_flagged_for_the_device_side_effect(self) -> None:
        w = self.opened("hang_read")
        errors: list = []
        thread = threading.Thread(target=lambda: _capture(errors, w.read))
        thread.start()
        time.sleep(0.2)
        report = w.stop()                                                      # from another thread, while the read is in flight
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIn("operation_interrupted", report.uncertain)
        self.assertIsInstance(errors[0], WorkerError)
        self.assertIn(errors[0].category, {"worker_ipc_lost", "worker_crashed"})        # which one the reader sees first is a race


# --- slot integrity -----------------------------------------------------------------------------------------------------


class IntegrityTests(Isolated):
    BAD = ("torn_frame", "stale_fence_only", "bad_crc", "pixels_changed_after_crc", "lying_width", "lying_nbytes", "oversize_nbytes",
           "format_mismatch", "header_overwritten", "magic_corrupt", "version_corrupt", "unknown_pixel_format_in_header")

    def test_every_corrupted_or_inconsistent_slot_is_rejected_without_delivering_pixels(self) -> None:
        for fixture in self.BAD:
            with self.subTest(fixture):
                w = self.opened(fixture)
                with self.assertRaises(WorkerError) as ctx:
                    w.read()
                self.assertEqual(ctx.exception.category, "worker_protocol_error")
                self.assertEqual(w.failure_category, "worker_protocol_error")
                self.assertGone(w)

    def test_a_sequence_that_skips_or_repeats_is_rejected(self) -> None:
        for fixture in ("seq_skip", "seq_repeat"):
            with self.subTest(fixture):
                w = self.opened(fixture)
                self.assertEqual(w.read().seq, 1)
                with self.assertRaises(WorkerError) as ctx:
                    w.read()
                self.assertEqual(ctx.exception.category, "worker_protocol_error")
                self.assertGone(w)

    def test_a_write_by_the_worker_while_the_parent_owns_the_slot_is_detected(self) -> None:
        w = self.opened("ok_ops")
        segment = w._segment
        real = segment.copy_pixels

        def copy_then_worker_writes(nbytes):
            data = real(nbytes)
            struct.pack_into("<I", segment._shm.buf, 28, 99)                  # the worker moves a fence after the parent started to copy
            return data

        with mock.patch.object(segment, "copy_pixels", copy_then_worker_writes):
            with self.assertRaises(WorkerError) as ctx:
                w.read()
        self.assertEqual(ctx.exception.category, "worker_protocol_error")
        self.assertGone(w)

    def test_a_late_write_to_the_pixels_does_not_reach_the_delivered_copy(self) -> None:
        w = self.opened("ok_ops")
        segment = w._segment
        real = segment.copy_pixels

        def copy_then_pixels_change(nbytes):
            data = real(nbytes)
            segment._shm.buf[64] = (segment._shm.buf[64] + 1) % 256         # a hostile worker could do this; the CRC cannot prevent it
            return data

        with mock.patch.object(segment, "copy_pixels", copy_then_pixels_change):
            image = w.read()
        self.assertEqual(image.pixels, bytes((7 + i) % 251 for i in range(24)))   # the copy was taken before the change
        self.assertNotEqual(w._segment.copy_pixels(1), image.pixels[:1])          # the segment itself did change

    def test_the_crc_is_checked_on_the_parents_copy_not_on_the_segment(self) -> None:
        w = self.opened("ok_ops")
        segment = w._segment
        real = segment.copy_pixels

        def corrupt_copy(nbytes):
            return bytes([real(nbytes)[0] ^ 1]) + real(nbytes)[1:]

        with mock.patch.object(segment, "copy_pixels", corrupt_copy):
            with self.assertRaises(WorkerError) as ctx:
                w.read()
        self.assertEqual(ctx.exception.category, "worker_protocol_error")

    def test_no_view_of_the_segment_is_ever_returned(self) -> None:
        w = self.opened("ok_ops")
        image = w.read()
        self.assertIs(type(image.pixels), bytes)
        self.assertNotIn("memoryview", repr(image))
        w.stop()
        self.assertGone(w)                                                    # closing did not hit a BufferError: no export was left


# --- concurrency, stop and shared memory ----------------------------------------------------------------------------------


def _capture(errors: list, call) -> None:
    try:
        errors.append(call())
    except BaseException as exc:                                                # noqa: BLE001 - the test inspects the exact type
        errors.append(exc)


def _alive(pid) -> bool:
    from tests.test_isolated_launcher import pid_alive
    return pid_alive(pid)


def _gone_within(pid, seconds: float = 5.0) -> bool:
    """True when ``pid`` is not running (a worker that ended by its own lifeline watchdog needs a moment)."""
    end = time.monotonic() + seconds
    while _alive(pid) and time.monotonic() < end:
        time.sleep(0.05)
    return not _alive(pid)


class ConcurrencyTests(Isolated):
    def test_stop_from_another_thread_never_uses_a_closed_buffer(self) -> None:
        for _ in range(15):
            w = self.opened("ok_ops")
            errors: list = []

            def reader() -> None:
                try:
                    while True:
                        w.read()
                except BaseException as exc:                                    # noqa: BLE001
                    errors.append(exc)

            thread = threading.Thread(target=reader)
            thread.start()
            time.sleep(0.05)
            w.stop()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], WorkerError, repr(errors[0]))      # never ValueError / TypeError / BufferError / SegmentClosed
            self.assertIn(errors[0].category, {"worker_ipc_lost", "worker_crashed"})
            self.assertGone(w)

    def test_the_mapping_is_closed_only_after_an_active_copy_has_finished(self) -> None:
        w = self.opened("ok_ops")
        segment = w._segment
        real = segment.copy_pixels
        copying, release = threading.Event(), threading.Event()

        def slow_copy(nbytes):
            copying.set()
            release.wait(6)
            return real(nbytes)

        results: list = []
        with mock.patch.object(segment, "copy_pixels", slow_copy):
            reader = threading.Thread(target=lambda: _capture(results, w.read))
            reader.start()
            self.assertTrue(copying.wait(3))
            stopper = threading.Thread(target=w.stop)
            stopper.start()
            time.sleep(1.4)                                                    # longer than stop()'s own wait for the operation to leave
            self.assertFalse(segment.closed, "the mapping was closed under an active copy")
            release.set()
            reader.join(5)
            stopper.join(5)
        self.assertFalse(reader.is_alive() or stopper.is_alive())
        self.assertTrue(isinstance(results[0], (WorkerImage, WorkerError)), repr(results[0]))
        self.assertTrue(segment.closed)
        self.assertGone(w)

    def test_concurrent_stops_all_return_the_same_report(self) -> None:
        w = self.opened("ok_ops")
        reports: list = []
        threads = [threading.Thread(target=lambda: reports.append(w.stop())) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(len(reports), 4)
        self.assertEqual(len({id(r) for r in reports}), 1)
        self.assertGone(w)

    def test_an_operation_after_stop_is_refused(self) -> None:
        w = self.opened("ok_ops")
        w.stop()
        with self.assertRaises(WorkerError) as ctx:
            w.read()
        self.assertEqual(ctx.exception.category, "worker_ipc_lost")

    def test_the_state_stays_readable_while_an_operation_hangs(self) -> None:
        """No shared lock is held across a wait that the worker can stretch: readers of the object are never blocked by it."""
        w = self.opened("hang_read")
        errors: list = []
        thread = threading.Thread(target=lambda: _capture(errors, w.read))
        thread.start()
        time.sleep(0.2)
        began = time.monotonic()
        for _ in range(50):
            repr(w), w.state, w.transitions, w.decoder_open, live_worker_count()
        self.assertLess(time.monotonic() - began, 0.3)
        with self.assertRaises(InvalidTransition):
            w.open(ADDRESS, 1000, 1000)                                        # busy: refused immediately, state untouched by the refusal
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(errors[0], WorkerError)
        self.assertEqual(errors[0].category, "read_deadline_exceeded")

    def test_restart_means_a_new_worker_and_a_new_segment(self) -> None:
        first = self.opened("ok_ops")
        name = first._segment.name
        first.stop()
        with self.assertRaises(WorkerError):
            first.read()
        second = self.opened("ok_ops")
        self.assertNotEqual(second._segment.name, name)
        self.assertEqual(second.read().seq, 1)                                  # the sequence starts again with the new worker
        with self.assertRaises(InvalidTransition):
            first.start()                                                       # a terminated object is never reused

    def test_an_interrupted_start_leaves_nothing(self) -> None:
        w = make("no_hello", start_deadline_s=5.0)
        self.workers.append(w)
        errors: list = []
        thread = threading.Thread(target=lambda: _capture(errors, w.start))
        thread.start()
        time.sleep(0.4)
        w.stop()
        thread.join(8)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(errors[0], WorkerError)
        self.assertGone(w)


class LauncherIdentityTests(Isolated):
    """Diagnostic for the Windows venv launcher (design: ``sys.executable`` may be a redirector that starts the real interpreter).

    What is checked is the consequence that matters, not the pid equality: after containment, BOTH the process the launcher
    started (``Popen``) and the process that actually wrote the shared header (the real worker) must be gone, including for a
    worker that spins while holding the GIL (its lifeline watchdog cannot help there). The identities are written to stderr so
    the operator can attach them to the report. A pid mismatch is information, not a failure.
    """

    def test_both_the_started_process_and_the_real_worker_are_gone_after_stop(self) -> None:
        for fixture in ("ok_ops", "spin_read"):
            with self.subTest(fixture):
                w = self.opened(fixture)
                popen_pid, worker_pid = w.pid, w.worker_pid
                in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
                sys.stderr.write(f"LAUNCHER-DIAG fixture={fixture} platform={sys.platform} venv={in_venv} executable={Path(sys.executable).name} "
                                 f"popen_pid={popen_pid} worker_pid={worker_pid} same={popen_pid == worker_pid}\n")
                if fixture == "spin_read":
                    with self.assertRaises(WorkerError):
                        w.read()
                w.stop()
                self.assertTrue(_gone_within(popen_pid), "the process started by the launcher is still running")
                self.assertTrue(_gone_within(worker_pid), "the real worker outlived the launcher process")
                self.assertGone(w)


class RedactionTests(Isolated):
    def test_nothing_shows_the_address_or_the_segment_name(self) -> None:
        w = self.opened("ok_ops")
        name = w._segment.name
        image = w.read()
        text = " ".join([repr(w), repr(image), repr(w._segment), repr(w.transitions), repr(config())])
        self.assertNotIn("camera.invalid", text)
        self.assertNotIn(name, text)
        report = w.stop()
        self.assertNotIn(name, repr(report))
        self.assertNotIn("camera.invalid", repr(report))
        for failure in ("open_error", "hang_open"):
            f = self.started(failure)
            with self.assertRaises(WorkerError) as ctx:
                f.open(ADDRESS, 100, 100)
            self.assertNotIn("camera.invalid", repr(ctx.exception) + str(ctx.exception) + repr(f))

    def test_the_command_line_of_the_worker_has_neither_address_nor_segment(self) -> None:
        w = self.opened("ok_ops")
        name = w._segment.name
        if POSIX and os.path.exists(f"/proc/{w.pid}/cmdline"):
            cmdline = Path(f"/proc/{w.pid}/cmdline").read_bytes().decode(errors="replace")
            self.assertNotIn(name, cmdline)
            self.assertNotIn("camera.invalid", cmdline)
            self.assertNotIn("rtsp", cmdline)


if __name__ == "__main__":
    unittest.main()
