"""DB-03 Wave 4B-2: the process-wide worker limit (decision D10), recovery of unreapable workers, exit cleanup and parent death.

Offline, real processes. The limit counts a worker from reservation until the process is CONFIRMED gone; a timeout,
``kill()`` or a lost pipe never frees a slot. Linux results here do not replace the Windows run.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.test_isolated_launcher import pid_alive
from tests.test_isolated_slot import ADDRESS, config, make, shm_names
from tsn_dss.engine.opencv_isolated_decoder import (
    MAX_LIVE_WORKERS, WorkerConfig, WorkerError, WorkerState as S, abandoned_worker_count, live_worker_count,
    reclaim_abandoned_workers, stop_all_workers,
)
from tsn_dss.engine.opencv_isolated_decoder import process as process_module

ROOT = Path(__file__).resolve().parents[1]
POSIX = sys.platform != "win32"


class FakePopen:
    """A process that never reports an exit unless the test says so. ``kill``/``terminate`` do nothing, like an uninterruptible process."""

    def __init__(self, *a, **k) -> None:
        self.pid = 999_999
        self.code = None
        self.signalled = []

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        if self.code is None:
            raise subprocess.TimeoutExpired("stuck", timeout)
        return self.code

    def terminate(self):
        self.signalled.append("terminate")

    def kill(self):
        self.signalled.append("kill")

    @property
    def returncode(self):
        return self.code


class LimitCase(unittest.TestCase):
    def setUp(self) -> None:
        self.baseline = live_worker_count()
        self.assertEqual(self.baseline, 0, "another test leaked a worker slot")
        self.workers: list = []

    def tearDown(self) -> None:
        for w in self.workers:
            w.stop()
        with process_module._ABANDONED_LOCK:
            del process_module._ABANDONED[:]
        self.assertEqual(live_worker_count(), 0)

    def new(self, fixture: str = "ok_ops", **override):
        w = make(fixture, **override)
        self.workers.append(w)
        return w


class LimitTests(LimitCase):
    def test_the_ceiling_is_two(self) -> None:
        self.assertEqual(MAX_LIVE_WORKERS, 2)
        with self.assertRaises(ValueError):
            WorkerConfig(max_live_workers=3)
        with self.assertRaises(ValueError):
            WorkerConfig(max_live_workers=0)

    def test_a_third_worker_is_refused_before_any_resource_is_created(self) -> None:
        a, b = self.new(), self.new()
        a.start(), b.start()
        self.assertEqual(live_worker_count(), 2)
        third = self.new()
        names = shm_names()
        with mock.patch.object(process_module.subprocess, "Popen", side_effect=AssertionError("spawned")), \
                mock.patch.object(process_module, "ParentSegment", side_effect=AssertionError("segment created")):
            with self.assertRaises(WorkerError) as ctx:
                third.start()
        self.assertEqual(ctx.exception.category, "worker_limit")
        self.assertIs(third.state, S.TERMINATED)
        self.assertEqual(live_worker_count(), 2)                                # the refused worker took no slot
        self.assertIsNone(third.pid)
        if names is not None:
            self.assertEqual(shm_names(), names)

    def test_a_confirmed_exit_frees_the_slot(self) -> None:
        a, b = self.new(), self.new()
        a.start(), b.start()
        a.stop()
        self.assertEqual(live_worker_count(), 1)
        c = self.new()
        c.start()
        self.assertEqual(live_worker_count(), 2)

    def test_the_configured_limit_can_be_lower(self) -> None:
        a = self.new(max_live_workers=1)
        a.start()
        b = self.new(max_live_workers=1)
        with self.assertRaises(WorkerError) as ctx:
            b.start()
        self.assertEqual(ctx.exception.category, "worker_limit")

    def test_failed_starts_give_the_slot_back(self) -> None:
        for fixture in ("crash_before_hello", "exit_before_hello", "wrong_nonce", "shm_refused", "no_attach"):
            with self.subTest(fixture):
                w = self.new(fixture)
                with self.assertRaises(WorkerError):
                    w.start()
                self.assertEqual(live_worker_count(), 0)
        w = self.new()
        with mock.patch.object(process_module.subprocess, "Popen", side_effect=OSError("x")):
            with self.assertRaises(WorkerError):
                w.start()
        self.assertEqual(live_worker_count(), 0)

    def test_a_worker_that_failed_an_operation_gives_the_slot_back_only_after_it_is_gone(self) -> None:
        w = self.new("hang_read")
        w.start()
        w.open(ADDRESS, 1000, 100)
        with self.assertRaises(WorkerError):
            w.read()
        self.assertEqual(live_worker_count(), 0)
        self.assertFalse(pid_alive(w.pid))

    def test_concurrent_starts_never_exceed_the_limit(self) -> None:
        for _ in range(3):
            attempts = [self.new("ok_ops") for _ in range(6)]
            barrier = threading.Barrier(len(attempts))
            outcomes: list = []

            def go(w) -> None:
                barrier.wait()
                try:
                    w.start()
                    outcomes.append("ok")
                except WorkerError as exc:
                    outcomes.append(exc.category)

            threads = [threading.Thread(target=go, args=(w,)) for w in attempts]
            for t in threads:
                t.start()
            peak = 0
            while any(t.is_alive() for t in threads):
                peak = max(peak, live_worker_count())
                time.sleep(0.005)
            for t in threads:
                t.join(30)
            self.assertLessEqual(peak, 2)
            self.assertEqual(outcomes.count("ok"), 2, outcomes)
            self.assertEqual(outcomes.count("worker_limit"), 4, outcomes)
            for w in attempts:
                w.stop()
            self.assertEqual(live_worker_count(), 0)

    def test_concurrent_starts_and_stops_keep_the_count_exact(self) -> None:
        errors: list = []

        def cycle() -> None:
            for _ in range(4):
                w = make("ok_ops")
                try:
                    w.start()
                except WorkerError as exc:
                    if exc.category != "worker_limit":
                        errors.append(exc.category)
                    continue
                time.sleep(0.02)
                w.stop()

        threads = [threading.Thread(target=cycle) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(errors, [])
        self.assertEqual(live_worker_count(), 0)


class UnreapableTests(LimitCase):
    """A worker that cannot be confirmed gone keeps its slot; only a later confirmed exit gives it back (owner condition)."""

    def stuck_worker(self, holder: list):
        def factory(*a, **k):
            popen = FakePopen()
            holder.append(popen)
            return popen

        w = self.new(start_deadline_s=0.3, terminate_wait_s=0.1, kill_wait_s=0.1, close_graceful_s=0.1)
        with mock.patch.object(process_module.subprocess, "Popen", factory):
            with self.assertRaises(WorkerError):
                w.start()
        return w

    def test_timeout_kill_and_a_lost_pipe_do_not_free_the_slot(self) -> None:
        holder: list = []
        w = self.stuck_worker(holder)
        self.assertTrue(w.abandoned)
        self.assertEqual(w.failure_category in {"worker_ipc_lost", "worker_handshake_timeout"}, True)
        self.assertEqual(holder[0].signalled, ["terminate", "kill"])             # both were tried; neither proved an exit
        self.assertEqual(abandoned_worker_count(), 1)
        self.assertEqual(live_worker_count(), 1)                                 # still counted
        report = w.stop()
        self.assertFalse(report.slot_released)
        self.assertTrue(report.containment_failed)
        self.assertIn("process_unreaped", report.uncertain)

    def test_two_unreapable_workers_block_every_new_start(self) -> None:
        h1: list = []
        h2: list = []
        self.stuck_worker(h1)
        self.stuck_worker(h2)
        self.assertEqual(live_worker_count(), 2)
        w = self.new()
        with self.assertRaises(WorkerError) as ctx:
            w.start()
        self.assertEqual(ctx.exception.category, "worker_limit")

    def test_a_confirmed_exit_recovers_the_slot_explicitly(self) -> None:
        holder: list = []
        self.stuck_worker(holder)
        self.assertEqual(reclaim_abandoned_workers(), 0)                         # still running: nothing to recover
        self.assertEqual(live_worker_count(), 1)
        holder[0].code = 0                                                       # the OS now confirms the exit
        self.assertEqual(reclaim_abandoned_workers(), 1)
        self.assertEqual(live_worker_count(), 0)
        self.assertEqual(abandoned_worker_count(), 0)
        self.assertEqual(reclaim_abandoned_workers(), 0)                         # idempotent

    def test_a_confirmed_exit_recovers_the_slot_at_the_next_start(self) -> None:
        h1: list = []
        h2: list = []
        self.stuck_worker(h1)
        self.stuck_worker(h2)
        h1[0].code = 137
        w = self.new()
        w.start()                                                                # reclaimed automatically, then reserved
        self.assertEqual(live_worker_count(), 2)
        self.assertEqual(abandoned_worker_count(), 1)

    def test_a_poll_that_raises_is_not_evidence_of_an_exit(self) -> None:
        holder: list = []
        self.stuck_worker(holder)
        with mock.patch.object(holder[0], "poll", side_effect=OSError("denied")):
            self.assertEqual(reclaim_abandoned_workers(), 0)
        self.assertEqual(live_worker_count(), 1)

    def test_concurrent_recovery_and_starts_do_not_over_release(self) -> None:
        holder: list = []
        self.stuck_worker(holder)
        holder[0].code = 0
        results: list = []

        def recover() -> None:
            results.append(reclaim_abandoned_workers())

        threads = [threading.Thread(target=recover) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(sum(results), 1)                                        # exactly one thread recovered the slot
        self.assertEqual(live_worker_count(), 0)


class ExitCleanupTests(LimitCase):
    def test_stop_all_workers_contains_every_live_worker(self) -> None:
        a, b = self.new(), self.new()
        a.start(), b.start()
        a.open(ADDRESS, 1000, 1000)
        self.assertEqual(stop_all_workers(), 2)
        for w in (a, b):
            self.assertIs(w.state, S.TERMINATED)
            self.assertFalse(pid_alive(w.pid))
        self.assertEqual(live_worker_count(), 0)

    def test_atexit_is_registered_once_and_only_when_a_worker_is_reserved(self) -> None:
        with mock.patch.object(process_module.atexit, "register") as register, mock.patch.object(process_module, "_ATEXIT_REGISTERED", False):
            a = self.new()
            a.start()
            b = self.new()
            b.start()
        register.assert_called_once_with(process_module.stop_all_workers)

    def run_probe(self, mode: str, timeout: float = 60):
        with tempfile.TemporaryDirectory() as tmp:
            report = os.path.join(tmp, "report.json")
            began = time.monotonic()
            result = subprocess.run([sys.executable, str(ROOT / "tests" / "isolated_shm_probe.py"), report, mode], cwd=ROOT,
                                    capture_output=True, text=True, timeout=timeout, env={**os.environ, "PYTHONPATH": str(ROOT)})
            elapsed = time.monotonic() - began
            return result, json.loads(Path(report).read_text(encoding="ascii")), elapsed

    def wait_gone(self, pid: int, seconds: float = 10) -> bool:
        end = time.monotonic() + seconds
        while pid_alive(pid) and time.monotonic() < end:
            time.sleep(0.1)
        return not pid_alive(pid)

    def kill_leftover(self, pid: int) -> None:
        if pid_alive(pid):
            os.kill(pid, signal.SIGKILL) if POSIX else subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)

    def test_a_normal_exit_without_stop_contains_the_worker(self) -> None:
        result, report, _ = self.run_probe("exit_clean")
        self.assertEqual(result.returncode, 0, result.stderr)
        gone = self.wait_gone(report["pid"], 3)
        self.kill_leftover(report["pid"])
        self.assertTrue(gone, "the exit handler left the worker running")
        self.assertEqual(result.stderr, "", "warnings from the resource tracker or the exit handler")

    def test_an_exit_with_a_stubborn_worker_is_bounded_and_ends_it(self) -> None:
        result, report, elapsed = self.run_probe("exit_stubborn")
        gone = self.wait_gone(report["pid"], 3)
        self.kill_leftover(report["pid"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(gone)
        self.assertLess(elapsed, 15, "the interpreter was held back by the worker")
        self.assertEqual(result.stderr, "")

    def test_exit_cleanup_is_best_effort_and_a_hard_death_of_the_host_bypasses_it(self) -> None:
        """Documented limit: with no atexit run the worker is ended by its own lifeline watchdog, not by the host."""
        result, report, _ = self.run_probe("die_after_ready")
        self.assertEqual(result.returncode, 9, result.stderr)
        gone = self.wait_gone(report["pid"], 10)
        self.kill_leftover(report["pid"])
        self.assertTrue(gone, "the worker survived its host (lifeline watchdog failed)")

    @unittest.skipUnless(shm_names() is not None, "no /dev/shm")
    def test_a_host_that_dies_after_the_handshake_leaves_no_segment_name(self) -> None:
        before = shm_names()
        result, report, _ = self.run_probe("die_after_ready")
        self.wait_gone(report["pid"], 10)
        self.kill_leftover(report["pid"])
        self.assertEqual(shm_names() - before, set(), "the name was unlinked at the end of the handshake, so nothing can remain")

    @unittest.skipUnless(shm_names() is not None, "no /dev/shm")
    def test_a_host_that_dies_before_the_handshake_is_cleaned_up_by_the_resource_tracker(self) -> None:
        """The one POSIX window: the name exists until the handshake ends. Python's resource tracker (a separate process
        started by the host) removes it after the host died. This is a property of CPython, observed here, not a guarantee of ours."""
        before = shm_names()
        result, report, _ = self.run_probe("die_before_hello")
        self.assertEqual(result.returncode, 9, result.stderr)
        self.assertTrue(report["segment"])
        self.wait_gone(report["pid"], 10)
        self.kill_leftover(report["pid"])
        end = time.monotonic() + 10
        while shm_names() - before and time.monotonic() < end:
            time.sleep(0.1)
        self.assertEqual(shm_names() - before, set(), "the tracker did not remove the segment (documented limitation if it fails)")


if __name__ == "__main__":
    unittest.main()
