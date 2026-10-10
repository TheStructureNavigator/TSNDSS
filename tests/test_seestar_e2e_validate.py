"""DB-03 Wave 5: the PHYSICAL end-to-end tool, offline. A fake device state machine, a fake clock and a fake socket; nothing is moved,
started or contacted, and no hardware or network is used."""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "seestar_e2e_validate.py"
spec = importlib.util.spec_from_file_location("seestar_e2e_validate", SCRIPT)
E = importlib.util.module_from_spec(spec)
sys.modules["seestar_e2e_validate"] = E
spec.loader.exec_module(E)

HOST = "192.0.2.77"                     # documentation range (RFC 5737) used as a sentinel: it must never appear in a report


class FakeDevice:
    """Mount and cameras as a small state machine on a fake clock. Effects of a command arrive ``delay`` seconds later."""

    def __init__(self, *, close=True, move="none", cameras="idle", delay=4.0, stuck=()) -> None:
        self.close, self.move, self.cameras, self.delay, self.t = close, move, cameras, delay, 0.0
        self.pending: list = []
        self.commands: list = []
        self.reject: set = set()
        self.stuck: set = set(stuck)      # commands accepted but whose effect never happens
        self.start_result = 0
        self.unreadable_after: int | None = None
        self.reads = 0

    # fake clock
    def clock(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds
        for item in [p for p in self.pending if p[0] <= self.t]:
            self.pending.remove(item)
            item[1]()

    def _later(self, method: str, effect) -> None:
        if method not in self.stuck:
            self.pending.append((self.t + self.delay, effect))

    # reader
    def _read_check(self) -> None:
        self.reads += 1
        if self.unreadable_after is not None and self.reads > self.unreadable_after:
            raise E.E2EError("mount_unreadable")

    def mount(self) -> dict:
        self._read_check()
        return {"close": self.close, "move_type": self.move}

    def app(self) -> dict:
        self._read_check()
        word = self.cameras
        ready = word == "working"
        return {label: {"mode": "scenery" if ready else "none", "stage": "RTSP" if ready else "Idle", "state": word,
                        "rtsp_state": word, "port": port}
                for label, _, port in E.CAMERAS}

    # control channel
    def send(self, method: str):
        self.commands.append(method)
        if method in self.reject:
            raise E.E2EError(f"rpc_error_{method}")
        if method == "scope_move_to_horizon":
            self.move = "moving"
            self._later(method, lambda: setattr(self, "close", False) or setattr(self, "move", "none"))
        elif method == "iscope_start_view":
            self._later(method, lambda: setattr(self, "cameras", "working"))
            return self.start_result
        elif method == "iscope_stop_view":
            self._later(method, lambda: setattr(self, "cameras", "idle"))
        elif method == "scope_park":
            self.move = "moving"
            self._later(method, lambda: setattr(self, "close", True) or setattr(self, "move", "none"))
        return 0


def good_preview(device: FakeDevice, calls: list):
    def preview(frames: int) -> dict:
        calls.append((device.close, device.move, device.cameras, frames))
        return {"overall": {"verdict": "PASS"}, "cameras": [
            {"camera": c, "frames": frames, "format": "bgr8", "width": 4, "height": 2, "verdict": "PASS", "fail_reasons": []} for c in ("main", "wide")]}
    return preview


def run(device, preview=None, calls=None, **kw):
    calls = calls if calls is not None else []
    return E.run_sequence(device, device, preview or good_preview(device, calls), clock=device.clock, sleep=device.sleep, **kw)


def status(report):
    return {s["name"]: s["status"] for s in report["stages"]}


class SequenceTests(unittest.TestCase):
    def test_the_full_sequence_from_a_parked_telescope(self) -> None:
        device, calls = FakeDevice(), []
        report = run(device, calls=calls)
        self.assertEqual(report["overall"], "PASS")
        self.assertEqual(device.commands, ["scope_move_to_horizon", "iscope_start_view", "iscope_stop_view", "scope_park"])
        self.assertEqual(calls, [(False, "none", "working", 3)])                      # the preview ran with the arm open and both cameras working
        self.assertEqual(set(status(report).values()), {"PASS"})
        self.assertEqual(report["final_state"], {"arm": "parked", "cameras_stopped": True})
        self.assertEqual((report["unsafe_or_unknown_final_state"], report["device_left_as_found"]), (False, False))
        self.assertEqual([s["name"] for s in report["stages"]], list(E.STAGES))
        self.assertEqual(report["preview"][0]["frames"], 3)

    def test_an_arm_that_is_already_open_and_scenery_already_running_is_not_commanded_again(self) -> None:
        device = FakeDevice(close=False, cameras="working")
        report = run(device)
        self.assertEqual(report["overall"], "PASS")
        self.assertEqual(device.commands, ["iscope_stop_view", "scope_park"])
        self.assertEqual(report["arm_open_at_start"], True)

    def test_every_wait_is_bounded_by_the_timeout(self) -> None:
        device = FakeDevice(delay=10 ** 6)
        report = run(device, timeout=20.0, poll=2.0)
        self.assertEqual(status(report)["deploy_arm"], "FAIL")
        self.assertLessEqual(device.t, 20.0 + 2.0 + 2 * 20.0 + 2.0)                     # deploy wait + cleanup waits, nothing unbounded


class FailClosedTests(unittest.TestCase):
    def test_a_moving_mount_gets_no_command_and_the_device_is_left_as_found(self) -> None:
        device = FakeDevice(close=False, move="moving")
        report = run(device)
        self.assertEqual(device.commands, [])
        self.assertEqual((report["overall"], report["device_left_as_found"], report["unsafe_or_unknown_final_state"]), ("FAIL", True, False))
        self.assertEqual((status(report)["deploy_arm"], report["stages"][1]["detail"]), ("FAIL", "mount_moving"))
        self.assertEqual(status(report)["stop_scenery"], "NOT_RUN")

    def test_an_unknown_arm_position_gets_no_command(self) -> None:
        device = FakeDevice(close=None)
        report = run(device)
        self.assertEqual((device.commands, report["overall"]), ([], "FAIL"))
        self.assertEqual(report["stages"][1]["detail"], "arm_state_unknown")

    def test_an_arm_that_never_finishes_opening_is_never_parked_or_started(self) -> None:
        device = FakeDevice(stuck={"scope_move_to_horizon"})
        report = run(device, timeout=10.0)
        self.assertEqual(device.commands, ["scope_move_to_horizon"])                    # no start, no stop, no park while the mount moves
        self.assertEqual((report["overall"], report["unsafe_or_unknown_final_state"], report["final_state"]["arm"]), ("FAIL", True, "moving_or_unknown"))
        self.assertEqual(report["stages"][6]["detail"], "refused_mount_moving")

    def test_scenery_is_never_started_unless_the_arm_is_open_and_stationary(self) -> None:
        for close, move in ((True, "none"), (False, "moving"), (None, "none")):
            device = FakeDevice(close=close, move=move)
            step = E.Run(device, device, good_preview(device, []), timeout=10.0, max_total=60.0, poll=2.0, frames=1, clock=device.clock, sleep=device.sleep)
            with self.assertRaises(E.E2EError) as ctx:
                step.start()
            self.assertEqual((ctx.exception.token, device.commands), ("arm_not_open_and_stationary", []), (close, move))

    def test_a_rejected_start_leaves_the_arm_parked_again_and_fails(self) -> None:
        device = FakeDevice()
        device.reject.add("iscope_start_view")
        report = run(device)
        self.assertEqual(device.commands, ["scope_move_to_horizon", "iscope_start_view", "scope_park"])      # stop needed no command: cameras never ran
        self.assertEqual((report["overall"], report["unsafe_or_unknown_final_state"], report["final_state"]["arm"]), ("FAIL", False, "parked"))
        self.assertEqual(report["stages"][2]["detail"], "rpc_error_iscope_start_view")

    def test_a_start_with_a_nonzero_result_is_not_accepted(self) -> None:
        device = FakeDevice()
        device.start_result = 5
        report = run(device)
        self.assertEqual(report["stages"][2]["detail"], "start_view_not_accepted")
        self.assertEqual(report["overall"], "FAIL")

    def test_cameras_that_never_become_ready_fail_and_are_cleaned_up(self) -> None:
        device = FakeDevice(stuck={"iscope_start_view"})
        report = run(device, timeout=10.0)
        self.assertEqual(report["stages"][3]["detail"], "cameras_not_ready")
        self.assertEqual((report["overall"], report["final_state"]), ("FAIL", {"arm": "parked", "cameras_stopped": True}))

    def test_cameras_that_do_not_confirm_stopped_block_parking_and_raise_the_alarm(self) -> None:
        device = FakeDevice(stuck={"iscope_stop_view"})
        report = run(device, timeout=10.0)
        self.assertNotIn("scope_park", device.commands)                                  # never parked with cameras still active
        self.assertEqual(report["stages"][5]["detail"], "cameras_did_not_confirm_stopped")
        self.assertEqual(report["stages"][6]["detail"], "refused_cameras_not_confirmed_stopped")
        self.assertEqual((report["overall"], report["unsafe_or_unknown_final_state"], report["final_state"]["arm"]), ("FAIL", True, "open"))

    def test_a_park_that_never_completes_is_unsafe(self) -> None:
        device = FakeDevice(stuck={"scope_park"})
        report = run(device, timeout=10.0)
        self.assertEqual((report["stages"][6]["detail"], report["overall"], report["unsafe_or_unknown_final_state"]), ("arm_did_not_park", "FAIL", True))

    def test_a_failed_preview_still_stops_and_parks_and_never_passes(self) -> None:
        device = FakeDevice()
        bad = lambda frames: {"overall": {"verdict": "FAIL", "fail_reasons": ["gate_denied"]}, "cameras": []}      # noqa: E731
        report = run(device, preview=bad)
        self.assertEqual(report["stages"][4]["detail"], "preview_gate_denied")
        self.assertEqual((report["overall"], report["final_state"], report["unsafe_or_unknown_final_state"]), ("FAIL", {"arm": "parked", "cameras_stopped": True}, False))
        self.assertEqual(device.commands[-2:], ["iscope_stop_view", "scope_park"])

    def test_an_interrupt_during_the_preview_still_runs_the_cleanup(self) -> None:
        device = FakeDevice()

        def interrupted(frames):
            raise KeyboardInterrupt

        report = run(device, preview=interrupted)
        self.assertEqual((report["overall"], report["interrupted"]), ("FAIL", True))
        self.assertEqual(device.commands[-2:], ["iscope_stop_view", "scope_park"])
        self.assertEqual(report["final_state"]["arm"], "parked")

    def test_an_interrupt_during_the_cleanup_is_reported_as_unsafe(self) -> None:
        device = FakeDevice()
        original = device.sleep
        state = {"n": 0}

        def sleeping(seconds):
            state["n"] += 1
            if device.commands.count("iscope_stop_view"):
                raise KeyboardInterrupt
            original(seconds)

        report = E.run_sequence(device, device, good_preview(device, []), clock=device.clock, sleep=sleeping)
        self.assertEqual((report["overall"], report["unsafe_or_unknown_final_state"], report["interrupted"]), ("FAIL", True, True))

    def test_an_unreadable_device_during_cleanup_is_an_unknown_state_never_a_pass(self) -> None:
        device = FakeDevice()
        device.unreadable_after = 14                                                    # the reads work for the main flow, then fail
        report = run(device)
        self.assertEqual(report["overall"], "FAIL")
        self.assertTrue(report["unsafe_or_unknown_final_state"])
        self.assertEqual(report["final_state"]["arm"], "UNKNOWN")

    def test_an_unreadable_device_at_the_start_sends_nothing(self) -> None:
        device = FakeDevice()
        device.unreadable_after = 0
        report = run(device)
        self.assertEqual((device.commands, report["overall"], report["unsafe_or_unknown_final_state"]), ([], "FAIL", False))
        self.assertEqual(report["final_state"]["arm"], "never_read")


class PermissionAndChannelTests(unittest.TestCase):
    def test_a_permit_cannot_be_made_without_the_opt_in_and_the_typed_confirmation(self) -> None:
        with self.assertRaises(E.E2EError):
            E.PhysicalPermit(object())
        with self.assertRaises(E.E2EError):
            E.grant_permit(False, lambda: "DEPLOY")
        with self.assertRaises(E.E2EError):
            E.grant_permit(True, lambda: "yes")
        self.assertIsInstance(E.grant_permit(True, lambda: " DEPLOY "), E.PhysicalPermit)

    def test_the_channel_refuses_every_command_without_a_permit_before_any_socket(self) -> None:
        connect = mock.Mock(side_effect=AssertionError("a socket was opened"))
        channel = E.ControlChannel(HOST, mock.Mock(), None, connect=connect)
        for method in E.CONTROL:
            with self.assertRaises(E.E2EError) as ctx:
                channel.send(method)
            self.assertEqual(ctx.exception.token, "no_physical_permit")
        connect.assert_not_called()

    def test_only_the_four_reference_commands_exist_with_fixed_parameters(self) -> None:
        self.assertEqual(set(E.CONTROL), {"scope_move_to_horizon", "iscope_start_view", "iscope_stop_view", "scope_park"})
        self.assertEqual(E.CONTROL["iscope_start_view"], {"mode": "scenery", "target_ra_dec": [None, None], "target_name": "Unknown",
                                                          "lp_filter": False, "cam_id": 1})
        channel = E.ControlChannel(HOST, mock.Mock(), E.grant_permit(True, lambda: "DEPLOY"), connect=mock.Mock(side_effect=AssertionError("socket")))
        for method in ("iscope_stop_all", "set_setting", "scope_goto", "get_device_state"):
            with self.assertRaises(E.E2EError) as ctx:
                channel.send(method)
            self.assertEqual(ctx.exception.token, "command_not_allowed")

    def test_a_command_goes_over_an_authenticated_short_lived_connection(self) -> None:
        frames = []

        class Sock:
            def __init__(self):
                self.out = b""
                self.closed = False

            def settimeout(self, t):
                pass

            def sendall(self, data):
                frames.append(json.loads(data.decode().strip()))
                self.out += (json.dumps(self.reply(frames[-1])) + "\r\n").encode()

            def reply(self, frame):
                if frame["method"] == "get_verify_str":
                    return {"id": frame["id"], "code": 0, "result": {"str": "challenge"}}
                if frame["method"] == "verify_client":
                    return {"id": frame["id"], "code": 0, "result": 0}
                return {"id": frame["id"], "code": 0, "result": 0, "method": frame["method"]}

            def recv(self, n):
                if not hasattr(self, "noised"):                              # an unsolicited event frame before the first reply
                    self.noised = True
                    self.out = b'{"Event":"noise"}\r\n' + self.out
                data, self.out = self.out[:n], self.out[n:]
                return data

            def close(self):
                self.closed = True

        sock = Sock()
        auth = mock.Mock()
        auth.sign.return_value = "sig"
        channel = E.ControlChannel(HOST, auth, E.grant_permit(True, lambda: "DEPLOY"), connect=lambda addr, timeout: sock)
        self.assertEqual(channel.send("scope_park"), 0)
        self.assertEqual([f["method"] for f in frames], ["get_verify_str", "verify_client", "pi_is_verified", "scope_park"])
        self.assertTrue(frames[-1]["verify"])
        self.assertTrue(sock.closed)

    def test_an_rpc_error_and_a_lost_connection_become_fixed_tokens(self) -> None:
        class Bad:
            def settimeout(self, t): pass
            def sendall(self, data): raise OSError("secret " + HOST)
            def close(self): pass

        channel = E.ControlChannel(HOST, mock.Mock(), E.grant_permit(True, lambda: "DEPLOY"), connect=lambda a, t: Bad())
        with self.assertRaises(E.E2EError) as ctx:
            channel.send("scope_park")
        self.assertEqual(ctx.exception.token, "connection_lost")
        self.assertNotIn(HOST, repr(ctx.exception) + str(ctx.exception))

    def test_cli_without_the_opt_in_or_a_terminal_exits_2_and_contacts_nothing(self) -> None:
        with mock.patch.object(E, "run_sequence", side_effect=AssertionError("ran")), mock.patch("socket.create_connection", side_effect=AssertionError("net")):
            for argv in (["--host", HOST], ["--host", HOST, "--allow-physical-motion"]):
                with mock.patch("sys.stdout"):
                    self.assertEqual(E.main(argv), 2, argv)

    def test_the_wrong_confirmation_text_exits_2_and_contacts_nothing(self) -> None:
        env = {"TSNDSS_SEESTAR_KEY_PATH": str(SCRIPT)}                                   # any existing file: the key is never read here
        with mock.patch.dict("os.environ", env), mock.patch.object(E.importlib.util, "find_spec", return_value=object()), \
                mock.patch.object(E, "run_sequence", side_effect=AssertionError("ran")), mock.patch("sys.stdout"):
            self.assertEqual(E.main(["--host", HOST, "--allow-physical-motion", "--out", "/tmp/never_written_e2e.json"], confirm=lambda: "no"), 2)


class ReportAndStaticTests(unittest.TestCase):
    def test_the_report_has_no_host_key_address_or_raw_payload(self) -> None:
        text = json.dumps(run(FakeDevice()))
        self.assertNotIn(HOST, text)
        self.assertNotIn("rtsp", text.lower())
        self.assertIsNone(re.search(r"[0-9a-f]{32,}|BEGIN|\.pem", text))
        self.assertEqual(set(json.loads(text)), {"overall", "unsafe_or_unknown_final_state", "device_left_as_found", "interrupted", "stages",
                                                 "commands_sent", "arm_open_at_start", "scenery_active_at_start", "final_state", "preview", "elapsed_s"})

    def test_importing_the_tool_does_nothing_to_a_device(self) -> None:
        code = ("import socket\nsocket.create_connection = lambda *a, **k: (_ for _ in ()).throw(AssertionError('net'))\n"
                "import importlib.util\ns = importlib.util.spec_from_file_location('x', %r)\nm = importlib.util.module_from_spec(s)\ns.loader.exec_module(m)\nprint('ok')" % str(SCRIPT))
        import subprocess
        result = subprocess.run([sys.executable, "-P", "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60, env={"PYTHONPATH": str(ROOT)})
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "ok"), result.stderr)

    def test_control_code_lives_only_in_this_tool_and_the_provider_stays_read_only(self) -> None:
        for path in list((ROOT / "tsn_dss").rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            for command in ("scope_move_to_horizon", "scope_park", "iscope_start_view", "iscope_stop_view"):
                self.assertNotIn(command, text, f"{path.name}: {command}")
        code = SCRIPT.read_text(encoding="utf-8")
        self.assertEqual(sorted(set(re.findall(r'"((?:scope|iscope)_[a-z_]+)"', code)) - {"iscope_get_app_state"}),
                         ["iscope_start_view", "iscope_stop_view", "scope_move_to_horizon", "scope_park"])
        tree = ast.parse(code)
        self.assertEqual([n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr in {"write_bytes", "imwrite"}], [])


if __name__ == "__main__":
    unittest.main()
