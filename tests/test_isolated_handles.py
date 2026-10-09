"""DB-03 Wave 4B-1: ownership and release of OS handles over repeated launch/stop cycles.

Strategy under test (no private CPython API): once the child is reaped, the launcher drops its only reference to the
``Popen``, which closes the child's process handle by reference counting. A child that cannot be reaped keeps its
``Popen`` in a visible registry. The Windows test counts the real process handles of THIS process and is meant to be run
by the operator on Windows (skipped elsewhere); the POSIX test counts file descriptors.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
import weakref
from unittest import mock

from tsn_dss.engine.opencv_isolated_decoder import WorkerConfig, WorkerError, WorkerProcess, abandoned_worker_count
from tsn_dss.engine.opencv_isolated_decoder import process as process_module

FAST = WorkerConfig(start_deadline_s=1.0, ping_deadline_s=0.5, close_graceful_s=0.3, terminate_wait_s=0.5, kill_wait_s=0.5)


def make(fixture: str, config: WorkerConfig = FAST) -> WorkerProcess:
    return WorkerProcess(config, _entry_module="tests.process_fixtures", _extra_args=("--fixture", fixture))


def open_handle_count():
    """Windows: GetProcessHandleCount with explicit argtypes/restype. POSIX: entries of /proc/self/fd. None if unavailable."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.GetProcessHandleCount.restype = wintypes.BOOL
        count = wintypes.DWORD(0)
        return int(count.value) if k.GetProcessHandleCount(k.GetCurrentProcess(), ctypes.byref(count)) else None
    return len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else None


class CounterTests(unittest.TestCase):
    def test_the_counter_reacts_to_an_extra_handle(self) -> None:
        first = open_handle_count()
        if first is None:
            self.skipTest("no handle counter on this platform")
        extra = os.open(os.devnull, os.O_RDONLY)
        raised = open_handle_count()
        os.close(extra)
        self.assertGreater(raised, first)
        self.assertEqual(open_handle_count(), first)


class PopenOwnershipTests(unittest.TestCase):
    def tracked(self):
        instances: list = []

        class Tracked(subprocess.Popen):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                instances.append(weakref.ref(self))

        return Tracked, instances

    def test_the_popen_is_released_after_a_normal_stop(self) -> None:
        Tracked, instances = self.tracked()
        with mock.patch.object(process_module.subprocess, "Popen", Tracked):
            w = make("ok")
            w.start(); w.ping(); w.stop()
        self.assertEqual(len(instances), 1)
        self.assertIsNone(instances[0](), "the Popen (and with it the process handle) is still referenced")
        self.assertTrue(w.popen_released)

    def test_the_popen_is_released_after_a_failed_start(self) -> None:
        for fixture in ("no_hello", "garbage_hello", "crash_before_hello", "no_pong"):
            Tracked, instances = self.tracked()
            with mock.patch.object(process_module.subprocess, "Popen", Tracked):
                w = make(fixture)
                with self.assertRaises(WorkerError):
                    w.start()
            self.assertEqual(len(instances), 1, fixture)
            self.assertIsNone(instances[0](), fixture)

    def test_the_popen_is_released_after_a_forced_containment(self) -> None:
        Tracked, instances = self.tracked()
        with mock.patch.object(process_module.subprocess, "Popen", Tracked):
            w = make("hang_on_close")
            w.start(); w.stop()
        self.assertIsNone(instances[0]())

    def test_an_unreapable_child_keeps_its_popen_and_the_leak_is_visible(self) -> None:
        class Stuck:
            pid = 4242

            def poll(self):
                return None

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired("stuck", timeout)

            def terminate(self):
                pass

            def kill(self):
                pass

        before = abandoned_worker_count()
        with mock.patch.object(process_module.subprocess, "Popen", lambda *a, **k: Stuck()):
            w = make("ok", WorkerConfig(start_deadline_s=0.3, terminate_wait_s=0.1, kill_wait_s=0.1, close_graceful_s=0.1))
            with self.assertRaises(WorkerError):
                w.start()
        try:
            self.assertTrue(w.abandoned)
            # no peer holds the other pipe end in this fake, so the channel reads as lost; the first failure is the one reported
            self.assertIn(w.failure_category, {"worker_ipc_lost", "worker_handshake_timeout"})
            self.assertEqual(abandoned_worker_count(), before + 1)
            self.assertTrue(w.popen_released)                                    # this object no longer owns it
        finally:
            with process_module._ABANDONED_LOCK:
                del process_module._ABANDONED[before:]


@unittest.skipIf(open_handle_count() is None, "no handle counter on this platform")
class SteadyStateTests(unittest.TestCase):
    """The same property on both platforms: after the first cycle, every cycle ends at the same count (design: C11 of the spike)."""

    def cycle(self, fixture: str) -> None:
        w = make(fixture)
        try:
            try:
                w.start()
                w.ping()
            except WorkerError:
                pass
        finally:
            w.stop()

    def steady(self, fixture: str, cycles: int = 10) -> list:
        counts = []
        for _ in range(cycles):
            self.cycle(fixture)
            time.sleep(0.05)
            counts.append(open_handle_count())
        return counts

    def test_successful_cycles_do_not_leak(self) -> None:
        counts = self.steady("ok")
        self.assertEqual(set(counts[1:]), {counts[0]}, counts)

    def test_failed_start_cycles_do_not_leak(self) -> None:
        for fixture in ("garbage_hello", "exit_before_hello", "wrong_nonce"):
            counts = self.steady(fixture, 6)
            self.assertEqual(set(counts[1:]), {counts[0]}, (fixture, counts))

    def test_forced_containment_cycles_do_not_leak(self) -> None:
        w0 = make("hang_on_close")
        w0.start(); w0.stop()                                                    # warm-up of any one-time allocations
        counts = []
        for _ in range(5):
            w = make("hang_on_close")
            w.start(); w.stop()
            counts.append(open_handle_count())
        self.assertEqual(set(counts[1:]), {counts[0]}, counts)

    def test_the_count_returns_to_the_starting_level(self) -> None:
        self.cycle("ok")                                                         # one-time initialisation (imports, caches)
        base = open_handle_count()
        for _ in range(5):
            self.cycle("ok")
        self.assertLessEqual(open_handle_count() - base, 0)


if __name__ == "__main__":
    unittest.main()
