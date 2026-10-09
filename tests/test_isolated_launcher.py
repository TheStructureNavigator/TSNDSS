"""DB-03 Wave 4B-1: launcher, readiness handshake and bounded containment against REAL worker processes.

The workers are test fixtures (tests/process_fixtures.py) started through the launcher's private test seam. There is no
OpenCV, no network, no device and no real address anywhere; every deadline is short and every wait is bounded.
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

from tsn_dss.engine.device_runtime.errors import InvalidTransition
from tsn_dss.engine.opencv_isolated_decoder import (
    IsolationUnsupported, WorkerConfig, WorkerError, WorkerEvent as V, WorkerProcess, WorkerState as S, abandoned_worker_count,
)

ROOT = Path(__file__).resolve().parents[1]
POSIX = sys.platform != "win32"
FAST = WorkerConfig(start_deadline_s=1.5, ping_deadline_s=0.6, close_graceful_s=0.4, terminate_wait_s=0.8, kill_wait_s=1.0)
SLACK = 3.0


def worker(fixture: str, config: WorkerConfig = FAST, *extra: str) -> WorkerProcess:
    return WorkerProcess(config, _entry_module="tests.process_fixtures", _extra_args=("--fixture", fixture, *extra))


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD(0)
            return bool(k.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            k.CloseHandle(handle)
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except FileNotFoundError:
        return False
    except OSError:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False


class Cleanup(unittest.TestCase):
    def make(self, fixture: str, config: WorkerConfig = FAST, *extra: str) -> WorkerProcess:
        w = worker(fixture, config, *extra)
        self.addCleanup(w.stop)
        return w


class LifecycleTests(Cleanup):
    def test_start_ping_stop(self) -> None:
        w = self.make("ok", WorkerConfig())
        self.assertIs(w.state, S.CREATED)
        self.assertIsNone(w.pid)
        w.start()
        self.assertIs(w.state, S.READY)
        self.assertIsNotNone(w.pid)
        self.assertFalse(w.decoder_open)
        w.ping(); w.ping()
        report = w.stop()
        self.assertIs(w.state, S.TERMINATED)
        self.assertEqual((report.steps, report.exit_code, report.containment_failed), (("close",), 0, False))
        self.assertTrue(w.popen_released)
        self.assertFalse(pid_alive(w.pid))
        self.assertEqual([(t.from_state, t.event, t.to_state) for t in w.transitions], [
            ("created", "start_requested", "starting"), ("starting", "handshake_complete", "ready"),
            ("ready", "stop_requested", "stopping"), ("stopping", "stopped", "terminated")])

    def test_stop_is_idempotent_and_returns_the_same_report(self) -> None:
        w = self.make("ok")
        w.start()
        first = w.stop()
        self.assertIs(w.stop(), first)
        self.assertEqual(len(w.transitions), 4)

    def test_stop_before_start_creates_nothing(self) -> None:
        w = self.make("ok")
        report = w.stop()
        self.assertIs(w.state, S.TERMINATED)
        self.assertEqual((report.steps, report.exit_code), ((), None))
        self.assertIsNone(w.pid)
        with self.assertRaises(InvalidTransition):
            w.start()

    def test_a_worker_is_single_use(self) -> None:
        w = self.make("ok")
        w.start()
        with self.assertRaises(InvalidTransition):
            w.start()
        w.stop()
        with self.assertRaises(InvalidTransition):
            w.start()

    def test_ping_needs_a_ready_worker(self) -> None:
        w = self.make("ok")
        with self.assertRaises(InvalidTransition):
            w.ping()
        w.start()
        w.stop()
        with self.assertRaises(InvalidTransition):
            w.ping()

    def test_context_manager_stops(self) -> None:
        with worker("ok") as w:
            w.start()
            pid = w.pid
        self.assertIs(w.state, S.TERMINATED)
        self.assertFalse(pid_alive(pid))

    def test_python_version_gate_is_at_creation_not_at_import(self) -> None:
        for old in ((3, 12), (3, 0), (2, 7), (3, 12, 9)):
            with self.assertRaises(IsolationUnsupported) as ctx:
                WorkerProcess(python_version=old)
            self.assertEqual(ctx.exception.category, "python_unsupported")
        WorkerProcess(python_version=(3, 13)).stop()
        WorkerProcess(python_version=(3, 14, 1)).stop()
        self.assertEqual(abandoned_worker_count(), 0)

    def test_construction_starts_nothing(self) -> None:
        with mock.patch("subprocess.Popen", side_effect=AssertionError("must not spawn")):
            w = WorkerProcess()
            self.assertIsNone(w.pid)
            w.stop()


class HygieneTests(Cleanup):
    def test_parent_process_state_is_not_touched(self) -> None:
        main = sys.modules["__main__"]
        before = (id(main), getattr(main, "__spec__", None), getattr(main, "__file__", None), dict(os.environ), list(sys.path),
                  sorted(sys.modules["__main__"].__dict__))
        handlers = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)} if POSIX else {}
        with tempfile.TemporaryDirectory() as tmp:
            w = self.make("report", WorkerConfig(), "--report", os.path.join(tmp, "r.json"))
            w.start(); w.stop()
        after = (id(main), getattr(main, "__spec__", None), getattr(main, "__file__", None), dict(os.environ), list(sys.path),
                 sorted(sys.modules["__main__"].__dict__))
        self.assertEqual(before, after)
        self.assertEqual(handlers, {s: signal.getsignal(s) for s in handlers})

    def test_the_child_sees_an_explicit_environment_and_a_numeric_only_command_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"TSN_SPIKE_PROBE_SECRET": "not-a-real-secret"}):
            path = os.path.join(tmp, "r.json")
            w = self.make("report", WorkerConfig(), "--report", path)
            w.start(); w.stop()
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertFalse(data["probe_visible"])
        self.assertNotIn("TSN_SPIKE_PROBE_SECRET", data["env_names"])
        argv = data["argv"]
        self.assertEqual(argv[0], "--ctl")
        self.assertTrue(argv[1].isdigit())
        self.assertEqual(argv[2:4], ["--proto", "1"])
        for item in argv:
            self.assertNotIn("rtsp", item)
            self.assertNotIn("://", item)
        self.assertEqual(data["main_spec"], "tests.process_fixtures")
        self.assertTrue(data["safe_path"])                         # -P: the working directory is not on sys.path
        self.assertFalse(data["stdin_is_tty"])

    def test_opencv_and_numpy_are_never_loaded_by_the_handshake(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "r.json")
            w = self.make("report", WorkerConfig(), "--report", path)
            w.start(); w.stop()
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertEqual(data["heavy_modules"], [])               # recorded by the worker while it was answering INIT
        probe = subprocess.run([sys.executable, "-P", "-c",
                                "import sys, tsn_dss.engine.opencv_isolated_decoder.worker_main as w; "
                                "print(sorted(m for m in sys.modules if m.split('.')[0] in {'cv2','numpy','PIL'}))"],
                               cwd=ROOT, capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(ROOT)})
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertEqual(probe.stdout.strip(), "[]")

    def test_the_parent_main_module_is_not_executed_again_in_the_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            marker, report = os.path.join(tmp, "marker.txt"), os.path.join(tmp, "r.json")
            result = subprocess.run([sys.executable, str(ROOT / "tests" / "isolated_main_probe.py"), marker, report], cwd=ROOT,
                                    capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONPATH": str(ROOT)})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("report-ready", result.stdout)
            lines = Path(marker).read_text(encoding="ascii").splitlines()
        self.assertEqual(len(lines), 1, lines)                        # the parent only; a re-executing child would add a line
        self.assertTrue(lines[0].endswith("name=__main__"))

    def test_the_address_vocabulary_does_not_exist_in_the_launcher(self) -> None:
        text = (ROOT / "tsn_dss" / "engine" / "opencv_isolated_decoder" / "process.py").read_text(encoding="utf-8")
        spawn = text[text.index("def _spawn"):text.index("def _channel_lost")]
        self.assertNotIn("address", spawn)
        self.assertNotIn("host", spawn.lower())


class StartupFailureTests(Cleanup):
    CASES = {
        "no_hello": "worker_handshake_timeout",
        "no_pong": "worker_handshake_timeout",
        "exit_before_hello": "worker_crashed",
        "crash_before_hello": "worker_crashed",
        "spin_before_hello": "worker_handshake_timeout",
        "python_unsupported": "python_unsupported",
        "known_error_status": "worker_internal",
        "unknown_status": "worker_protocol_error",
        "wrong_nonce": "worker_protocol_error",
        "wrong_version": "worker_protocol_error",
        "wrong_table_version": "worker_protocol_error",
        "garbage_hello": "worker_protocol_error",
        "oversize_hello": "worker_protocol_error",
        "truncated_hello": "worker_protocol_error",
        "trailing_hello": "worker_protocol_error",
        "pong_instead_of_hello": "worker_protocol_error",
        "reserved_bits_hello": "worker_protocol_error",
        "wrong_pong_op": "worker_protocol_error",
    }

    def test_every_startup_failure_has_one_category_and_leaves_nothing_behind(self) -> None:
        for fixture, category in self.CASES.items():
            with self.subTest(fixture=fixture):
                w = self.make(fixture)
                began = time.monotonic()
                with self.assertRaises(WorkerError) as ctx:
                    w.start()
                elapsed = time.monotonic() - began
                self.assertEqual(ctx.exception.category, category)
                self.assertEqual(w.failure_category, category)
                self.assertLess(elapsed, FAST.start_deadline_s + FAST.terminate_wait_s + FAST.kill_wait_s + SLACK, fixture)
                self.assertIs(w.state, S.TERMINATED)
                self.assertTrue(w.popen_released)
                self.assertFalse(w.abandoned)
                self.assertFalse(pid_alive(w.pid))
                events = [t.event for t in w.transitions]
                self.assertEqual(events[:3], ["start_requested", "failure", "stop_requested"])
                with self.assertRaises(InvalidTransition):
                    w.ping()

    def test_error_text_carries_only_the_category(self) -> None:
        w = self.make("garbage_hello")
        with self.assertRaises(WorkerError) as ctx:
            w.start()
        self.assertEqual(str(ctx.exception), "worker_protocol_error")
        self.assertIsNone(ctx.exception.__cause__)

    def test_unknown_status_is_never_mapped_to_a_nearby_category(self) -> None:
        w = self.make("unknown_status")
        with self.assertRaises(WorkerError) as ctx:
            w.start()
        self.assertEqual(ctx.exception.category, "worker_protocol_error")
        self.assertNotIn(ctx.exception.category, {"worker_internal", "python_unsupported"})

    def test_a_missing_entry_module_is_a_start_failure_not_a_hang(self) -> None:
        w = WorkerProcess(FAST, _entry_module="tests.no_such_worker_module")
        self.addCleanup(w.stop)
        began = time.monotonic()
        with self.assertRaises(WorkerError) as ctx:
            w.start()
        self.assertIn(ctx.exception.category, {"worker_crashed", "worker_handshake_timeout"})
        self.assertLess(time.monotonic() - began, FAST.start_deadline_s + SLACK + 2)

    def test_spawn_failure_is_reported_and_releases_the_pipe(self) -> None:
        w = self.make("ok")
        with mock.patch("subprocess.Popen", side_effect=OSError("cannot execute")):
            with self.assertRaises(WorkerError) as ctx:
                w.start()
        self.assertEqual(ctx.exception.category, "worker_start_failed")
        self.assertIs(w.state, S.TERMINATED)
        self.assertIsNone(w.pid)


class ContainmentTests(Cleanup):
    def test_graceful_close_ends_the_worker_without_force(self) -> None:
        w = self.make("ok")
        w.start()
        report = w.stop()
        self.assertEqual(report.steps, ("close",))

    def test_a_worker_that_hangs_on_close_is_terminated_within_the_bound(self) -> None:
        w = self.make("hang_on_close")
        w.start()
        began = time.monotonic()
        report = w.stop()
        elapsed = time.monotonic() - began
        self.assertIn("terminate", report.steps)
        self.assertFalse(report.containment_failed)
        self.assertLess(elapsed, FAST.close_graceful_s + FAST.terminate_wait_s + SLACK)
        self.assertFalse(pid_alive(w.pid))

    def test_a_worker_spinning_with_the_gil_held_is_still_ended(self) -> None:
        w = self.make("spin_on_close")
        w.start()
        report = w.stop()
        self.assertFalse(report.containment_failed)
        self.assertFalse(pid_alive(w.pid))

    @unittest.skipUnless(POSIX, "SIGTERM can be ignored only on POSIX; on Windows terminate and kill are the same call")
    def test_a_worker_that_ignores_terminate_is_killed(self) -> None:
        w = self.make("ignore_sigterm_hang_on_close")
        w.start()
        began = time.monotonic()
        report = w.stop()
        self.assertEqual(report.steps, ("close", "terminate", "kill"))
        self.assertEqual(report.exit_code, -signal.SIGKILL)
        self.assertLess(time.monotonic() - began, FAST.close_graceful_s + FAST.terminate_wait_s + FAST.kill_wait_s + SLACK)
        self.assertFalse(pid_alive(w.pid))

    def test_a_worker_stuck_after_ready_is_ended_by_stop(self) -> None:
        w = self.make("stuck_after_ready")
        w.start()
        report = w.stop()
        self.assertIn("terminate", report.steps)
        self.assertFalse(pid_alive(w.pid))

    def test_a_worker_that_exits_without_answering_close_is_handled(self) -> None:
        w = self.make("exit_without_reply_on_close")
        w.start()
        report = w.stop()
        self.assertFalse(report.containment_failed)
        self.assertFalse(pid_alive(w.pid))

    def test_a_worker_that_dies_after_ready_is_reported_by_the_next_ping(self) -> None:
        w = self.make("die_on_second_ping")
        w.start()
        with self.assertRaises(WorkerError) as ctx:
            w.ping()
        self.assertEqual(ctx.exception.category, "worker_crashed")
        self.assertIs(w.state, S.TERMINATED)
        self.assertEqual(w.failure_category, "worker_crashed")

    def test_a_worker_that_stops_answering_after_ready_is_ended_by_the_ping_deadline(self) -> None:
        w = self.make("hang_on_second_ping")
        w.start()
        began = time.monotonic()
        with self.assertRaises(WorkerError) as ctx:
            w.ping()
        self.assertEqual(ctx.exception.category, "worker_ipc_lost")
        self.assertLess(time.monotonic() - began, FAST.ping_deadline_s + FAST.close_graceful_s + FAST.terminate_wait_s + SLACK)
        self.assertFalse(pid_alive(w.pid))

    def test_stop_from_another_thread_interrupts_a_stuck_start(self) -> None:
        w = self.make("no_hello", WorkerConfig(start_deadline_s=30, terminate_wait_s=0.8, kill_wait_s=1.0, close_graceful_s=0.3))
        outcome: dict = {}

        def starter() -> None:
            try:
                w.start()
            except WorkerError as exc:
                outcome["category"] = exc.category

        thread = threading.Thread(target=starter)
        thread.start()
        deadline = time.monotonic() + 10
        while w.pid is None and time.monotonic() < deadline:
            time.sleep(0.02)
        began = time.monotonic()
        report = w.stop()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertLess(time.monotonic() - began, 6)
        self.assertIn("category", outcome)
        self.assertIs(w.state, S.TERMINATED)
        self.assertFalse(report.containment_failed)
        self.assertFalse(pid_alive(w.pid))

    def test_concurrent_stops_share_one_containment(self) -> None:
        w = self.make("hang_on_close")
        w.start()
        reports: list = []
        threads = [threading.Thread(target=lambda: reports.append(w.stop())) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        self.assertEqual(len(reports), 3)
        self.assertEqual(len({id(r) for r in reports}), 1)
        self.assertEqual(sum(1 for t in w.transitions if t.event == "stopped"), 1)


class ParentDeathTests(unittest.TestCase):
    def test_a_worker_with_a_stuck_main_thread_leaves_when_the_parent_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = os.path.join(tmp, "pid.txt")
            result = subprocess.run([sys.executable, str(ROOT / "tests" / "isolated_parent_death_probe.py"), pid_file], cwd=ROOT,
                                    capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONPATH": str(ROOT)})
            self.assertEqual(result.returncode, 9, result.stderr)
            pid = int(Path(pid_file).read_text(encoding="ascii"))
        deadline = time.monotonic() + 8
        while pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        alive = pid_alive(pid)
        if alive:                                                     # never leave an orphan behind, even when failing
            os.kill(pid, signal.SIGKILL) if POSIX else subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        self.assertFalse(alive, "the worker survived its parent (lifeline watchdog failed)")


if __name__ == "__main__":
    unittest.main()
