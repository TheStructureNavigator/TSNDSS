"""DB-05b v2: GoTo with native ScopeGoto events, offline. Physical GoTo stays BLOCKED by default."""

from __future__ import annotations

import contextlib
import dataclasses
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tsn_dss.engine.device_runtime import ManualClock, SequentialIdGenerator, TelemetrySource
from tsn_dss.engine.device_runtime.command_models import CommandKindPolicy, CommandPolicyError, CommandRequest, FreshnessRequirement
from tsn_dss.engine.seestar_control import (
    GOTO, ControlFreshness, ControlPostSendError, ControlPreSendError, GotoSafety, GotoTarget, OperatorPermit, SeestarControl,
    SeestarControlTransport,
)
from tsn_dss.engine.seestar_control import commands as control_commands
from tsn_dss.engine.seestar_control.commands import MountCoordinates, angular_separation_deg
from tsn_dss.engine.seestar_control.goto_watch import GotoCompletion, GotoWatch
from tsn_dss.engine.seestar_control.pointing import evaluate_pointing
from tsn_dss.engine.seestar_control.verification import build_goto_verifier
from tsn_dss.engine.seestar_provider.errors import SeestarUnreachable
from tsn_dss.engine.seestar_provider.protocol import RpcReply, parse_reply

try:
    from seestar_support import HOST, ScriptedDevice, StubAuthenticator, make_config
    from test_seestar_control_runtime import POLL, SimSeestar
except ImportError:  # pragma: no cover
    from tests.seestar_support import HOST, ScriptedDevice, StubAuthenticator, make_config
    from tests.test_seestar_control_runtime import POLL, SimSeestar

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("seestar_command_validate", ROOT / "tools" / "seestar_command_validate.py")
CV = importlib.util.module_from_spec(_spec)
sys.modules["seestar_command_validate"] = CV
_spec.loader.exec_module(CV)

TARGET = GotoTarget(5.5, -5.25)
DEADLINE = timedelta(minutes=10)
SITE = SimpleNamespace(latitude_deg=50.0, longitude_deg=20.0, elevation_m=200.0)
T_UTC = datetime(2026, 6, 21, 22, 0, tzinfo=timezone.utc)


def stand_in(altitude=40.0, sun=90.0):
    """An ephemeris stand-in; the production one is astropy-based and lives in tsn_dss.engine.pointing_geometry."""
    box = {"altitude": altitude, "sun": sun}
    return box, (lambda target, site, when: (box["altitude"], box["sun"]))


def safety(box_ephemeris=None, **kw):
    box, eph = box_ephemeris or stand_in()
    return GotoSafety(kw.get("site", SITE), kw.get("min_altitude", 20.0), kw.get("min_sun", 30.0), ephemeris=eph)


def enabled():
    return mock.patch.object(control_commands, "GOTO_PHYSICAL_ENABLED", True)


# --- a fake event socket for the real GotoWatch ------------------------------------------------------------------------------------


class EventSocket:
    """recv() pops the script: bytes are data, None is silence (timeout), b"" is a closed peer, an Exception is raised."""

    def __init__(self, *script) -> None:
        self.script = deque(script)
        self.sent: list[bytes] = []
        self.closed = False
        self.timeout = None

    def settimeout(self, value):
        self.timeout = value

    def sendall(self, data):
        if self.closed:
            raise OSError("closed")
        self.sent.append(data)

    def recv(self, size):
        if not self.script:
            raise TimeoutError("silence")
        item = self.script.popleft()
        if item is None:
            raise TimeoutError("silence")
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        self.closed = True


def frame(**fields) -> bytes:
    return json.dumps(fields).encode() + b"\r\n"


OUTER = dict(Event="ScopeGoto", route=["x"], lapse_ms=1234)


def make_watch(sock, *, keepalive=None, now=None, buffer=b""):
    ids = iter(range(5000, 6000))
    return GotoWatch(sock, buffer=buffer, clock=lambda: T_UTC, monotonic=now or (lambda: 0.0), next_id=lambda: next(ids), keepalive_s=keepalive)


class WatchTests(unittest.TestCase):
    def test_an_outer_complete_ends_the_watch_and_closes_the_connection(self) -> None:
        sock = EventSocket(frame(**OUTER, state="complete", cur_ra_dec=[5.5, -5.25], dist_deg=0.01))
        watch = make_watch(sock)
        self.assertEqual(watch.pump(), "complete")
        self.assertEqual((watch.completion.state, watch.completion.cur_ra_dec, watch.completion.dist_deg, watch.completion.lapse_ms),
                         ("complete", (5.5, -5.25), 0.01, 1234.0))
        self.assertTrue(sock.closed)

    def test_fail_and_cancel_are_terminal_and_keep_only_sanitized_detail(self) -> None:
        for state in ("fail", "cancel"):
            sock = EventSocket(frame(**OUTER, state=state, error="fail to operate", code=207), frame(**OUTER, state=state, error="no_target", code=7))
            watch = make_watch(sock)
            self.assertEqual(watch.pump(), state)
            self.assertIsNone(watch.completion.error)  # free text with spaces is not copied
        sock = EventSocket(frame(**OUTER, state="fail", error="no_target", code=7))
        watch = make_watch(sock)
        watch.pump()
        self.assertEqual((watch.completion.error, watch.completion.code), ("no_target", 7))

    def test_inner_frames_are_never_an_end(self) -> None:
        for extra in (dict(page=1, lapse_ms=5), dict(), dict(page=2, route=[1])):
            sock = EventSocket(frame(Event="ScopeGoto", state="complete", **extra), None)
            watch = make_watch(sock)
            self.assertEqual(watch.pump(), "waiting", extra)
            self.assertIsNone(watch.completion)
            self.assertEqual(watch.provisional_state, "complete")
            self.assertFalse(sock.closed)

    def test_irrelevant_or_malformed_input_is_ignored(self) -> None:
        lines = [b"not json\r\n", b"[1, 2]\r\n", frame(Event="PiStatus", state="complete", **{k: v for k, v in OUTER.items() if k != "Event"}),
                 frame(Event="ScopeGoto", state=3, **{k: v for k, v in OUTER.items() if k != "Event"}),
                 frame(Event="ScopeGoto", state="finished", **{k: v for k, v in OUTER.items() if k != "Event"}),
                 frame(Event="ScopeGoto", **{k: v for k, v in OUTER.items() if k != "Event"}),
                 frame(Event="ScopeHome", state="complete", **{k: v for k, v in OUTER.items() if k != "Event"}),
                 frame(id=7, code=0, result="ok"), None]
        watch = make_watch(EventSocket(*lines))
        self.assertEqual(watch.pump(), "waiting")
        self.assertIsNone(watch.completion)

    def test_progress_is_noted_and_the_lowercase_key_is_accepted(self) -> None:
        sock = EventSocket(frame(Event="ScopeGoto", state="working"), None)
        watch = make_watch(sock)
        self.assertEqual(watch.pump(), "waiting")
        self.assertTrue(watch.progress_seen)
        sock2 = EventSocket(frame(event="ScopeGoto", state="complete", route=1))
        self.assertEqual(make_watch(sock2).pump(), "complete")

    def test_fragmented_frames_and_leftover_buffer(self) -> None:
        raw = frame(**OUTER, state="complete")
        watch = make_watch(EventSocket(raw[:10], raw[10:25], raw[25:]))
        self.assertEqual(watch.pump(), "complete")
        leftover = make_watch(EventSocket(), buffer=raw)  # bytes already read after the command reply
        self.assertEqual(leftover.pump(), "complete")

    def test_a_dropped_or_failing_connection_is_lost_not_inferred(self) -> None:
        for script in ((b"",), (OSError("reset"),), (b"x" * 1_100_000,)):
            sock = EventSocket(*script)
            watch = make_watch(sock)
            self.assertEqual(watch.pump(), "lost", script[0] if not isinstance(script[0], bytes) else len(script[0]))
            self.assertIsNone(watch.completion)
            self.assertTrue(sock.closed)
            self.assertEqual(watch.pump(), "lost")  # stays lost

    def test_the_first_end_wins(self) -> None:
        watch = make_watch(EventSocket(frame(**OUTER, state="complete") + frame(**OUTER, state="fail")))
        self.assertEqual(watch.pump(), "complete")

    def test_keepalive_is_the_allow_listed_read_and_only_when_due(self) -> None:
        clock = {"t": 0.0}
        sock = EventSocket(None, None, None)
        watch = make_watch(sock, keepalive=5.0, now=lambda: clock["t"])
        watch.pump()
        self.assertEqual(sock.sent, [])
        clock["t"] = 5.0
        watch.pump()
        self.assertEqual(len(sock.sent), 1)
        sent = json.loads(sock.sent[0])
        self.assertEqual((sent["method"], sent["verify"]), ("test_connection", True))
        self.assertGreaterEqual(sent["id"], 5000)
        clock["t"] = 7.0
        watch.pump()
        self.assertEqual(len(sock.sent), 1)
        no_keepalive = make_watch(EventSocket(None), keepalive=None, now=lambda: 100.0)
        no_keepalive.pump()
        self.assertEqual(no_keepalive._sock.sent, [])

    def test_a_failing_keepalive_loses_the_watch(self) -> None:
        sock = EventSocket(None)
        sock.closed = True  # sendall raises OSError
        watch = make_watch(sock, keepalive=1.0, now=iter([0.0, 5.0, 5.0, 5.0]).__next__)
        self.assertEqual(watch.pump(), "lost")


# --- the real control transport -----------------------------------------------------------------------------------------------------


class TransportTests(unittest.TestCase):
    def transport(self, device):
        return SeestarControlTransport(make_config(), StubAuthenticator(), connect_factory=device.connect, goto_keepalive_s=None)

    def test_the_issuing_connection_stays_open_until_the_end_event(self) -> None:
        device = ScriptedDevice(auth="required")
        reply, watch = self.transport(device).open_goto(HOST, TARGET)
        (sock,) = device.sockets
        self.assertEqual(reply.code, 0)
        self.assertEqual(device.goto_calls, [[5.5, -5.25]])
        self.assertEqual(device.methods, ["get_verify_str", "verify_client", "pi_is_verified", "scope_goto"])
        self.assertFalse(sock.closed)  # kept open, authenticated, for the ScopeGoto event
        self.assertEqual(watch.pump(), "waiting")
        sock._incoming += frame(**OUTER, state="complete")
        self.assertEqual(watch.pump(), "complete")
        self.assertTrue(sock.closed)

    def test_events_that_arrive_with_the_reply_are_not_lost(self) -> None:
        device = ScriptedDevice()
        device.goto_events = [dict(OUTER, state="complete", cur_ra_dec=[5.5, -5.25])]
        _, watch = self.transport(device).open_goto(HOST, TARGET)
        self.assertEqual(watch.pump(), "complete")

    def test_each_watch_reads_only_its_own_connection(self) -> None:
        device = ScriptedDevice()
        transport = self.transport(device)
        _, first = transport.open_goto(HOST, TARGET)
        _, second = transport.open_goto(HOST, GotoTarget(1.0, 2.0))
        device.sockets[0]._incoming += frame(**OUTER, state="complete")
        self.assertEqual((first.pump(), second.pump()), ("complete", "waiting"))

    def test_failures_before_and_after_the_frame_and_no_leaked_connection(self) -> None:
        refused = ScriptedDevice()
        refused.connect_errors.append(OSError("down"))
        with self.assertRaises(ControlPreSendError):
            self.transport(refused).open_goto(HOST, TARGET)
        self.assertEqual(refused.sockets, [])
        silent = ScriptedDevice(auth="required")
        silent.accept_signature = False
        with self.assertRaises(ControlPreSendError):
            self.transport(silent).open_goto(HOST, TARGET)  # the handshake failed: nothing was sent
        self.assertEqual(silent.goto_calls, [])
        self.assertTrue(all(s.closed for s in silent.sockets))
        dropping = ScriptedDevice()
        dropping.close_when_empty = True
        dropping.fail_send = None
        original = dropping.handle

        def no_reply(message):
            return [] if message.get("method") == "scope_goto" else original(message)

        dropping.handle = no_reply
        with self.assertRaises(ControlPostSendError):
            self.transport(dropping).open_goto(HOST, TARGET)
        self.assertTrue(all(s.closed for s in dropping.sockets))

    def test_only_a_validated_target_can_be_sent(self) -> None:
        device = ScriptedDevice()
        for bad in ((5.5, -5.25), {"ra": 1, "dec": 2}, None, "5.5"):
            with self.assertRaises(ControlPreSendError):
                self.transport(device).open_goto(HOST, bad)
        self.assertEqual(device.connect_addresses, [])

    def test_the_read_only_allow_list_still_refuses_goto(self) -> None:
        from tsn_dss.engine.seestar_provider.errors import SeestarMethodNotAllowed
        from tsn_dss.engine.seestar_provider.protocol import encode_read_request

        with self.assertRaises(SeestarMethodNotAllowed):
            encode_read_request(1, "scope_goto", {"ra": 1})


# --- the provider and executor, against a simulated mount with a scripted watch ---------------------------------------------------


class SimWatch:
    def __init__(self, sim) -> None:
        self.sim, self.state, self.completion, self.progress_seen, self.provisional_state = sim, "waiting", None, False, None
        self.closed = False

    def pump(self):
        return self.state

    def close(self):
        self.closed = True

    def end(self, state, **fields):
        self.state = state
        self.completion = GotoCompletion(state, self.sim.clock(), **fields)


class GotoSim(SimSeestar):
    """A mount that slews in a few ticks and ends the GoTo with a scripted event. ``mode`` picks the ending."""

    def __init__(self, **kw) -> None:
        super().__init__(arm_closed=False, **kw)
        self.ra, self.dec = 1.0, 20.0
        self.goto_calls: list = []
        self.watches: list = []
        self.offset_deg = 0.0
        self.fail_equ = False
        self.fail_device = False
        self.clock = None

    def open_goto(self, host, target):
        with self.lock:
            self.goto_calls.append((target.ra_hours, target.dec_deg))
            mode = self.modes.get(GOTO, "complete")
            if mode == "pre_send":
                raise ControlPreSendError("connect_failed")
            if mode == "post_send_drop":
                raise ControlPostSendError("connection_closed")
            if mode == "error_reply":
                return RpcReply("", 1, None, None), SimWatch(self)
            watch = SimWatch(self)
            self.watches.append(watch)
            if mode != "never":
                def moving():
                    self.moving = True
                    watch.progress_seen = True

                def arrive():
                    self.moving, self.ra, self.dec = False, target.ra_hours, target.dec_deg + self.offset_deg
                    if mode == "lost":
                        watch.state = "lost"
                    else:
                        watch.end(mode if mode in ("fail", "cancel") else "complete")

                self.pending.extend([moving, arrive])
            return RpcReply("", 0, 0, None), watch

    def read_equ_coord(self, host):
        if self.fail_equ:
            raise SeestarUnreachable("connect_failed")
        return parse_reply({"method": "scope_get_equ_coord", "code": 0, "id": 1, "result": {"ra": self.ra, "dec": self.dec}})

    def read_device_state(self, host, keys):
        if self.fail_device:
            raise SeestarUnreachable("connect_failed")
        return super().read_device_state(host, keys)


class Rig:
    def __init__(self, sim=None, *, tolerance=None, goto_safety="default", authorizer=None, grant=True) -> None:
        self.sim = sim or GotoSim()
        self.clock = ManualClock()
        self.sim.clock = self.clock

        def sleep(seconds):
            self.clock.advance(timedelta(seconds=seconds))
            self.sim.tick()

        self.permit = OperatorPermit(operator_id="op", valid_for=timedelta(hours=6), clock=self.clock)
        self.box, eph = stand_in()
        self.safety = GotoSafety(SITE, 20.0, 30.0, ephemeris=eph) if goto_safety == "default" else goto_safety
        self.freshness = ControlFreshness(timedelta(minutes=5), timedelta(minutes=5), goto_tolerance_deg=tolerance)
        self.control = SeestarControl(config=make_config(), read_transport=self.sim, control_transport=self.sim, freshness=self.freshness,
                                      authorizer=authorizer or self.permit, goto_safety=self.safety, clock=self.clock,
                                      id_generator=SequentialIdGenerator(), sleep=sleep)
        self.handle = self.control.attach()
        self.handle.executor.establish_baseline_by_recovery(self.handle.connection)
        if grant and authorizer is None:
            self.permit.grant(self.handle.connection, allow_physical_motion=True, interactive=True, confirm=lambda: "DEPLOY")

    def goto(self, target=TARGET, *, confirm=True, parameters="default"):
        if confirm and self.permit._bound is not None:
            self.permit.confirm_arm_motion(GOTO, lambda: "ARM")
        params = target if parameters == "default" else parameters
        return self.handle.driver.execute(self.handle.connection, GOTO, "op", deadline=self.clock() + DEADLINE, poll_interval=POLL, parameters=params)


class TargetTests(unittest.TestCase):
    def test_valid_bounds_and_floats(self) -> None:
        for ra, dec in ((0, -90), (24, 90), (12.5, 0), (5, 10)):
            target = GotoTarget(ra, dec)
            self.assertEqual((target.ra_hours, target.dec_deg), (float(ra), float(dec)))

    def test_invalid_targets_are_refused(self) -> None:
        nan, inf = float("nan"), float("inf")
        for ra, dec in ((nan, 0), (0, nan), (inf, 0), (0, -inf), (-0.01, 0), (24.01, 0), (0, 90.01), (0, -90.01), (True, 0), (0, False),
                        ("5", 0), (0, None), (None, None), ([5], 0)):
            with self.assertRaises(ValueError, msg=repr((ra, dec))):
                GotoTarget(ra, dec)

    def test_immutable_and_hashable(self) -> None:
        with self.assertRaises(dataclasses.FrozenInstanceError):
            TARGET.ra_hours = 1.0
        self.assertEqual(hash(TARGET), hash(GotoTarget(5.5, -5.25)))

    def test_angular_separation(self) -> None:
        self.assertAlmostEqual(angular_separation_deg(0, 0, 6, 0), 90.0)
        self.assertAlmostEqual(angular_separation_deg(23.9, 0, 0.1, 0), 3.0, places=6)
        self.assertAlmostEqual(angular_separation_deg(1, 90, 13, 90), 0.0)


class CorePlumbingTests(unittest.TestCase):
    def test_parameters_are_immutable_and_policy_decides_acceptance(self) -> None:
        from tsn_dss.engine.device_runtime.models import CommandRef

        ref, now = CommandRef("c", "p", "conn"), datetime(2026, 1, 1, tzinfo=timezone.utc)
        for bad in ({"ra": 1}, [1, 2]):
            with self.assertRaises(CommandPolicyError):
                CommandRequest(ref, "k", "op", now, parameters=bad)
        fixed = CommandKindPolicy("k", state_changing=True, physical=True, idempotent=False, safety_sensitive=True,
                                  freshness=(FreshnessRequirement("x", timedelta(seconds=1)),))
        with_params = dataclasses.replace(fixed, takes_parameters=True)
        self.assertEqual(CommandRequest(ref, "k", "op", now, parameters=TARGET).violates_policy(fixed), "parameters_not_accepted")
        self.assertEqual(CommandRequest(ref, "k", "op", now).violates_policy(with_params), "parameters_required")
        with self.assertRaises(CommandPolicyError):
            dataclasses.replace(fixed, parameter_gate=lambda p, n: None)  # a gate needs a kind that takes parameters

    def test_the_four_fixed_commands_never_accept_parameters(self) -> None:
        with enabled():
            rig = Rig()
            rig.permit.confirm_arm_motion("seestar.arm.deploy", lambda: "ARM")
            for kind in ("seestar.arm.deploy", "seestar.scenery.stop"):
                outcome = rig.handle.driver.execute(rig.handle.connection, kind, "op", deadline=rig.clock() + DEADLINE, poll_interval=POLL,
                                                    parameters=TARGET)
                self.assertEqual(outcome.classification, "rejected", kind)
            self.assertEqual(rig.sim.control_calls, [])

    def test_a_goto_without_or_with_a_wrong_typed_target_never_reaches_the_wire(self) -> None:
        with enabled():
            rig = Rig()
            self.assertEqual(rig.goto(parameters=None).classification, "rejected")
            outcome = rig.goto(parameters=(5.5, -5.25))
            self.assertNotEqual(outcome.classification, "succeeded")
            self.assertEqual(rig.sim.goto_calls, [])


class BlockedByDefaultTests(unittest.TestCase):
    def test_physical_goto_is_blocked_even_when_everything_else_allows_it(self) -> None:
        self.assertIs(control_commands.GOTO_PHYSICAL_ENABLED, False)
        rig = Rig()
        outcome = rig.goto()
        self.assertEqual(rig.sim.goto_calls, [])
        self.assertEqual((outcome.classification, outcome.uncertainty_open), ("failed", False))

    def test_the_cli_refuses_before_building_anything(self) -> None:
        built = []
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = CV.main(["--host", HOST, "--operator", "op", "--telemetry-max-age", "10", "--capability-max-age", "10",
                            "--allow-physical-motion", "--command-deadline", "90", "--poll-interval", "2", "--permit-validity", "600",
                            "--command", "goto", "--ra-hours", "5.5", "--dec-deg", "-5.25", "--frame", "of-date",
                            "--site-lat", "50", "--site-lon", "20", "--site-elev-m", "200", "--min-altitude-deg", "20",
                            "--min-sun-separation-deg", "30", "--out", str(Path(tempfile.gettempdir()) / "never_written_goto.json")],
                           ask=lambda p: "x", control_factory=lambda *a: built.append(1), geometry=SimpleNamespace())
        self.assertEqual((code, built), (2, []))
        self.assertIn("BLOCKED", out.getvalue())


class EventDrivenOutcomeTests(unittest.TestCase):
    def test_complete_is_firmware_reported_completion_not_centering(self) -> None:
        with enabled():
            rig = Rig()
            outcome = rig.goto()
            self.assertEqual(outcome.classification, "succeeded")
            self.assertEqual(rig.sim.goto_calls, [(5.5, -5.25)])
            self.assertEqual(rig.sim.control_calls, [])  # no deploy, park, camera or tracking command
            self.assertIn("firmware-reported ScopeGoto complete", outcome.last_evidence)
            self.assertIn("NOT confirmed", outcome.last_evidence)
            self.assertIn("not cross-checked", outcome.last_evidence)

    def test_fail_and_cancel_never_verify_and_are_unknown_results(self) -> None:
        with enabled():
            for ending in ("fail", "cancel"):
                sim = GotoSim()
                sim.modes[GOTO] = ending
                rig = Rig(sim, tolerance=0.5)
                outcome = rig.goto()
                self.assertEqual((outcome.classification, outcome.uncertainty_open), ("unknown_result", True), ending)
                self.assertEqual(rig.goto().classification, "safety_blocked")  # the uncertainty blocks the next one
                self.assertEqual(len(sim.goto_calls), 1)  # never retried

    def test_the_provider_reports_the_event_it_saw_and_nothing_more(self) -> None:
        from tsn_dss.engine.device_runtime.command_executor import CommandIntent
        from tsn_dss.engine.device_runtime.provider import ProviderCommandStatus as PS

        with enabled():
            for ending, expected in (("complete", PS.REPORTED_COMPLETE), ("fail", PS.REPORTED_FAILED), ("cancel", PS.REPORTED_FAILED),
                                     ("never", PS.ACKNOWLEDGED)):
                sim = GotoSim()
                sim.modes[GOTO] = ending
                rig = Rig(sim)
                rig.permit.confirm_arm_motion(GOTO, lambda: "ARM")
                record = rig.handle.executor.admit(CommandIntent(rig.handle.connection, GOTO, "op", deadline=rig.clock() + DEADLINE, parameters=TARGET))
                rig.handle.executor.submit(record.command_id)
                provider = rig.control._provider
                self.assertEqual(provider.poll_command(rig.handle.connection.connection_id, record.command_id).status, PS.ACKNOWLEDGED)
                sim.tick()
                sim.tick()
                report = provider.poll_command(rig.handle.connection.connection_id, record.command_id)
                self.assertEqual(report.status, expected, ending)
                if ending in ("fail", "cancel"):
                    self.assertTrue(report.effect_possible)  # a failure report is never "no effect" (amendment A1)

    def test_a_lost_connection_is_unknown_not_a_guess(self) -> None:
        with enabled():
            sim = GotoSim()
            sim.modes[GOTO] = "lost"
            outcome = Rig(sim).goto()
            self.assertEqual((outcome.classification, outcome.uncertainty_open), ("unknown_result", True))

    def test_no_end_event_means_unknown_at_the_deadline(self) -> None:
        with enabled():
            sim = GotoSim()
            sim.modes[GOTO] = "never"
            rig = Rig(sim)
            outcome = rig.goto()
            self.assertEqual((outcome.classification, outcome.uncertainty_open), ("unknown_result", True))
            self.assertEqual(len(sim.goto_calls), 1)

    def test_a_coordinate_cross_check_that_disagrees_blocks_verification(self) -> None:
        with enabled():
            sim = GotoSim()
            sim.offset_deg = 5.0
            self.assertEqual(Rig(sim, tolerance=0.5).goto().classification, "unknown_result")
            near = Rig(GotoSim(), tolerance=0.5)
            outcome = near.goto()
            self.assertEqual(outcome.classification, "succeeded")
            self.assertIn("within 0.5 deg", outcome.last_evidence)

    def test_an_unreadable_cross_check_is_not_a_veto_but_is_said(self) -> None:
        with enabled():
            sim = GotoSim()
            sim.fail_equ = True
            outcome = Rig(sim, tolerance=0.5).goto()
            self.assertEqual(outcome.classification, "succeeded")
            self.assertIn("not cross-checked", outcome.last_evidence)

    def test_a_mount_that_is_not_known_stationary_does_not_verify(self) -> None:
        with enabled():
            sim = GotoSim()
            rig = Rig(sim)
            original = sim.read_device_state

            def moving_mount(host, keys):
                sim.moving = True
                return original(host, keys)

            sim.read_device_state = moving_mount
            self.assertEqual(rig.goto().classification, "safety_blocked")  # the gate needs a known stationary mount

    def test_other_failure_modes(self) -> None:
        with enabled():
            for mode, expected in (("pre_send", ("failed", False)), ("post_send_drop", ("unknown_result", True)),
                                   ("error_reply", ("unknown_result", True))):
                sim = GotoSim()
                sim.modes[GOTO] = mode
                outcome = Rig(sim).goto()
                self.assertEqual((outcome.classification, outcome.uncertainty_open), expected, mode)
                self.assertEqual(len(sim.goto_calls), 1, mode)

    def test_denied_states_and_authorization(self) -> None:
        with enabled():
            closed = Rig(GotoSim())
            closed.sim.arm_closed = True
            self.assertEqual(closed.goto().classification, "safety_blocked")
            self.assertEqual(closed.sim.goto_calls, [])
            unreadable = Rig()
            unreadable.sim.fail_device = True
            self.assertEqual(unreadable.goto().classification, "safety_blocked")
            self.assertEqual(Rig(grant=False).goto(confirm=False).classification, "safety_blocked")
            once = Rig()
            self.assertEqual(once.goto().classification, "succeeded")
            self.assertEqual(once.goto(confirm=False).classification, "safety_blocked")  # the confirmation was single use

    def test_the_provider_closes_open_goto_connections_on_disconnect(self) -> None:
        with enabled():
            sim = GotoSim()
            sim.modes[GOTO] = "never"
            rig = Rig(sim)
            rig.goto()
            self.assertTrue(sim.watches)
            provider = rig.control._provider
            provider.disconnect(rig.handle.connection.connection_id)
            self.assertEqual(provider._goto_watches, {})


def broken_site(eph, **changes):
    site = SimpleNamespace(latitude_deg=50.0, longitude_deg=20.0, elevation_m=0)
    safety_ = GotoSafety(site, 20, 30, ephemeris=eph)
    for name, value in changes.items():
        setattr(site, name, value)
    return safety_


class PointingGateTests(unittest.TestCase):
    def test_the_pure_check(self) -> None:
        box, eph = stand_in(40.0, 90.0)
        s = GotoSafety(SITE, 20.0, 30.0, ephemeris=eph)
        self.assertIsNone(evaluate_pointing(TARGET, s, T_UTC))
        box["altitude"] = 19.9
        self.assertEqual(evaluate_pointing(TARGET, s, T_UTC), "target_below_minimum_altitude")
        box.update(altitude=40.0, sun=29.9)
        self.assertEqual(evaluate_pointing(TARGET, s, T_UTC), "target_too_close_to_sun")
        box.update(sun=30.0)
        self.assertIsNone(evaluate_pointing(TARGET, s, T_UTC))  # the limit itself is allowed

    def test_everything_unevaluable_fails_closed(self) -> None:
        box, eph = stand_in()
        naive = datetime(2026, 1, 1)
        nan = float("nan")
        cases = [
            (TARGET, None, T_UTC, "pointing_policy_not_configured"),
            ((5.5, -5.25), GotoSafety(SITE, 20, 30, ephemeris=eph), T_UTC, "pointing_evaluation_failed"),
            (TARGET, GotoSafety(SITE, 20, 30, ephemeris=eph), naive, "pointing_evaluation_failed"),
            (TARGET, GotoSafety(SITE, 20, 30, ephemeris=eph), "now", "pointing_evaluation_failed"),
            (TARGET, broken_site(eph, latitude_deg=None), T_UTC, "pointing_evaluation_failed"),  # a Site changed after construction
            (TARGET, broken_site(eph, longitude_deg=float("nan")), T_UTC, "pointing_evaluation_failed"),
            (TARGET, broken_site(eph, latitude_deg=95.0), T_UTC, "pointing_evaluation_failed"),
            (TARGET, GotoSafety(SITE, 20, 30, ephemeris=lambda *a: (nan, 90.0)), T_UTC, "pointing_evaluation_failed"),
            (TARGET, GotoSafety(SITE, 20, 30, ephemeris=lambda *a: (_ for _ in ()).throw(RuntimeError("no ephemeris"))), T_UTC, "pointing_evaluation_failed"),
        ]
        for target, s, when, token in cases:
            self.assertEqual(evaluate_pointing(target, s, when), token)

    def test_limits_have_no_defaults_and_must_be_sane(self) -> None:
        _, eph = stand_in()
        with self.assertRaises(TypeError):
            GotoSafety(SITE, 20.0, ephemeris=eph)  # type: ignore[call-arg]
        for alt, sun in ((0, 30), (20, 0), (-5, 30), (float("nan"), 30), (True, 30), (95, 30), (20, 200)):
            with self.assertRaises(ValueError, msg=(alt, sun)):
                GotoSafety(SITE, alt, sun, ephemeris=eph)
        for lat, lon in ((None, 1.0), (95.0, 1.0), (1.0, float("nan")), (1.0, 400.0), ("50", 1.0)):
            with self.assertRaises(ValueError, msg=(lat, lon)):
                GotoSafety(SimpleNamespace(latitude_deg=lat, longitude_deg=lon, elevation_m=0), 20, 30, ephemeris=eph)

    def test_the_executor_gate_blocks_before_anything_is_sent_and_is_rechecked_at_submission(self) -> None:
        with enabled():
            rig = Rig()
            rig.box["altitude"] = 5.0
            outcome = rig.goto()
            self.assertEqual(outcome.classification, "safety_blocked")
            self.assertIn("parameter_gate:target_below_minimum_altitude", outcome.last_evidence)
            self.assertEqual(rig.sim.goto_calls, [])
            rig = Rig()
            rig.permit.confirm_arm_motion(GOTO, lambda: "ARM")
            from tsn_dss.engine.device_runtime.command_executor import CommandIntent

            record = rig.handle.executor.admit(CommandIntent(rig.handle.connection, GOTO, "op", deadline=rig.clock() + DEADLINE, parameters=TARGET))
            self.assertEqual(record.state.value, "validated")
            rig.box["sun"] = 1.0  # the Sun is now too close
            self.assertEqual(rig.handle.executor.submit(record.command_id).state.value, "safety_blocked")
            self.assertEqual(rig.sim.goto_calls, [])

    def test_without_a_configured_policy_goto_is_always_blocked(self) -> None:
        with enabled():
            rig = Rig(goto_safety=None)
            outcome = rig.goto()
            self.assertEqual(outcome.classification, "safety_blocked")
            self.assertIn("pointing_policy_not_configured", outcome.last_evidence)

    def test_a_gate_that_raises_blocks(self) -> None:
        with enabled():
            rig = Rig(goto_safety=GotoSafety(SITE, 20.0, 30.0, ephemeris=lambda *a: 1 / 0))
            self.assertEqual(rig.goto().classification, "safety_blocked")
            self.assertEqual(rig.sim.goto_calls, [])


try:
    import astropy  # noqa: F401

    HAVE_ASTROPY = True
except ImportError:  # pragma: no cover - the production geometry needs astropy
    HAVE_ASTROPY = False


@unittest.skipUnless(HAVE_ASTROPY, "astropy is not installed in this interpreter")
class GeometryTests(unittest.TestCase):
    """Sanity checks of the real geometry module (orders of magnitude and geometry, not an astrometry validation)."""

    def setUp(self) -> None:
        from tsn_dss.engine import pointing_geometry

        self.g = pointing_geometry

    def test_j2000_to_of_date_moves_the_point_by_the_expected_precession(self) -> None:
        ra, dec = self.g.j2000_to_of_date(0.0, 0.0, T_UTC)
        self.assertTrue(0.015 < ra < 0.030, ra)  # about 3 s of RA per year over ~26 years
        self.assertTrue(0.10 < dec < 0.20, dec)

    def test_sun_separation_and_altitude(self) -> None:
        near_sun = self.g.target_altitude_and_sun_separation(6.0, 23.4, 0.0, 0.0, 0.0, datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc))
        opposite = self.g.target_altitude_and_sun_separation(18.0, -23.4, 0.0, 0.0, 0.0, datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc))
        self.assertLess(near_sun[1], 2.0)
        self.assertGreater(near_sun[0], 60.0)
        self.assertGreater(opposite[1], 177.0)
        self.assertLess(opposite[0], -50.0)
        with self.assertRaises(ValueError):
            self.g.target_altitude_and_sun_separation(6.0, 0.0, 0.0, 0.0, 0.0, datetime(2026, 6, 21, 12, 0))


class VerifierUnitTests(unittest.TestCase):
    T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def verdict(self, *, completion="complete", completion_at=None, moving=False, coords=None, tol=None, sample_at=None):
        from tsn_dss.engine.device_runtime import TelemetryItem, TelemetrySample, ValueState
        from tsn_dss.engine.device_runtime.command_effects import EffectVerdictKind

        boundary = self.T0
        now = boundary + timedelta(seconds=30)
        pr = TelemetrySource.PROVIDER_REPORTED
        sample = TelemetrySample("seestar", "conn-1", sample_at or boundary + timedelta(seconds=20),
                                 (TelemetryItem("mount.move_type", pr, ValueState.KNOWN, "move" if moving else "none"),), False)
        runtime = SimpleNamespace(read_telemetry=lambda c: sample)
        record = SimpleNamespace(parameters=TARGET, provider_id="seestar", command_id="c1", policy=SimpleNamespace(kind_id=GOTO),
                                 history=(SimpleNamespace(to_state="submitted", at=boundary),))
        done = None if completion is None else GotoCompletion(completion, completion_at or boundary + timedelta(seconds=10))
        reading = None if coords is None else MountCoordinates(coords[0], coords[1], boundary + timedelta(seconds=20))
        freshness = ControlFreshness(timedelta(minutes=1), timedelta(minutes=1), goto_tolerance_deg=tol)
        got = build_goto_verifier(runtime, lambda cid: done, lambda c: reading, freshness, lambda: now)(SimpleNamespace(connection_id="conn-1"), record)
        self.assertNotEqual(got.kind, EffectVerdictKind.FAILED)
        return got.kind == EffectVerdictKind.VERIFIED

    def test_only_a_complete_event_with_a_stationary_mount_verifies(self) -> None:
        self.assertTrue(self.verdict())
        for completion in ("fail", "cancel", None):
            self.assertFalse(self.verdict(completion=completion), completion)
        self.assertFalse(self.verdict(completion_at=self.T0))  # not after the boundary
        self.assertFalse(self.verdict(moving=True))
        self.assertFalse(self.verdict(sample_at=self.T0))
        self.assertFalse(self.verdict(sample_at=self.T0 - timedelta(minutes=5)))

    def test_the_cross_check_is_optional_but_a_disagreement_is_contradictory(self) -> None:
        self.assertTrue(self.verdict(tol=0.5, coords=(5.5, -5.25)))
        self.assertTrue(self.verdict(tol=0.5, coords=None))  # unreadable: not a veto
        self.assertTrue(self.verdict(tol=None, coords=(1.0, 1.0)))  # no tolerance: not consulted
        self.assertFalse(self.verdict(tol=0.5, coords=(5.5, -3.0)))


class CliTests(unittest.TestCase):
    GEOMETRY = SimpleNamespace(
        j2000_to_of_date=lambda ra, dec, when: (ra + 0.02, dec + 0.15),
        target_altitude_and_sun_separation=lambda ra, dec, lat, lon, elev, when: (45.0, 120.0))
    ARGS = ["--telemetry-max-age", "10", "--capability-max-age", "10", "--allow-physical-motion", "--command-deadline", "90",
            "--poll-interval", "2", "--permit-validity", "600", "--command", "goto", "--ra-hours", "5.5", "--dec-deg", "-5.25",
            "--frame", "of-date", "--site-lat", "50", "--site-lon", "20", "--site-elev-m", "200", "--min-altitude-deg", "20",
            "--min-sun-separation-deg", "30"]

    def main(self, sim, args, ask=None, geometry=None):
        log, seen = [], {}

        def default_ask(prompt):
            log.append(prompt)
            return "DEPLOY" if "DEPLOY" in prompt else "ARM" if "Type ARM" in prompt else "GO"

        clock = ManualClock()
        sim.clock = clock

        def factory(freshness, permit, safety):
            seen["safety"] = safety
            return SeestarControl(config=make_config(), read_transport=sim, control_transport=sim, freshness=freshness, authorizer=permit,
                                  goto_safety=safety, clock=clock, id_generator=SequentialIdGenerator(),
                                  sleep=lambda s: (clock.advance(timedelta(seconds=s)), sim.tick()))

        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(out):
            target = Path(tmp) / "r.json"
            code = CV.main(["--host", HOST, "--operator", "op", "--out", str(target), *args], ask=ask or default_ask, control_factory=factory,
                           clock=clock, coordinates_reader=lambda: (sim.ra, sim.dec), geometry=geometry or self.GEOMETRY)
            report = json.loads(target.read_text(encoding="utf-8")) if target.exists() else None
        return code, out.getvalue(), report, log, seen

    def test_an_enabled_goto_runs_alone_and_reports_honestly(self) -> None:
        with enabled():
            sim = GotoSim()
            code, out, report, log, seen = self.main(sim, self.ARGS)
        self.assertEqual(code, 0, out)
        self.assertEqual((sim.goto_calls, sim.control_calls), ([(5.5, -5.25)], []))
        self.assertEqual(report["target"], {"ra_hours": 5.5, "dec_deg": -5.25})
        self.assertEqual(report["target_input"], {"ra_hours": 5.5, "dec_deg": -5.25, "frame": "of-date"})
        self.assertIn("provisional", report["coordinate_frame"])
        self.assertIn("NOT verified", report["coordinate_frame"])
        self.assertIn("not confirmed", report["completion"])
        self.assertEqual(report["final_reported_coordinates"], {"ra_hours": 5.5, "dec_deg": -5.25})
        self.assertIn("firmware-reported", report["outcomes"][0]["last_evidence"])
        self.assertEqual([p.split()[1] for p in log], ["DEPLOY", "GO", "ARM"])
        self.assertIsNotNone(seen["safety"])
        self.assertNotIn(HOST, json.dumps(report))

    def test_j2000_is_converted_only_when_asked_and_both_values_are_reported(self) -> None:
        with enabled():
            sim = GotoSim()
            args = [("j2000" if a == "of-date" else a) for a in self.ARGS]
            code, out, report, _, _ = self.main(sim, args)
        self.assertEqual(code, 0, out)
        self.assertEqual(report["target_input"]["frame"], "j2000")
        self.assertAlmostEqual(report["target"]["ra_hours"], 5.52)
        self.assertAlmostEqual(report["target_input"]["converted_to_of_date"]["dec_deg"], -5.10)
        self.assertEqual(sim.goto_calls[0][0], 5.52)

    def test_a_missing_frame_or_safety_input_is_refused_before_anything_is_built(self) -> None:
        with enabled():
            for flag, text in (("--frame", "--frame"), ("--site-lat", "--site-lat"), ("--site-lon", "--site-lon"), ("--site-elev-m", "--site-elev-m"),
                               ("--min-altitude-deg", "--min-altitude-deg"), ("--min-sun-separation-deg", "--min-sun-separation-deg"),
                               ("--ra-hours", "valid"), ("--dec-deg", "valid")):
                argv = list(self.ARGS)
                i = argv.index(flag)
                del argv[i:i + 2]
                sim = GotoSim()
                code, out, _, _, seen = self.main(sim, argv)
                self.assertEqual((code, sim.goto_calls, seen), (2, [], {}), flag)
                self.assertIn(text, out)

    def test_invalid_values_are_refused(self) -> None:
        with enabled():
            for flag, value in (("--ra-hours", "25"), ("--dec-deg", "91"), ("--ra-hours", "nan"), ("--min-altitude-deg", "0"),
                                ("--min-sun-separation-deg", "-1"), ("--site-lat", "95"), ("--goto-tolerance-deg", "0")):
                argv = list(self.ARGS)
                if flag in argv:
                    argv[argv.index(flag) + 1] = value
                else:
                    argv += [flag, value]
                sim = GotoSim()
                code, out, _, _, _ = self.main(sim, argv)
                self.assertEqual((code, sim.goto_calls), (2, []), (flag, value, out))

    def test_goto_options_are_refused_for_other_commands(self) -> None:
        with enabled():
            argv = ["park" if a == "goto" else a for a in self.ARGS]
            code, out, _, _, _ = self.main(GotoSim(), argv)
            self.assertEqual(code, 2)
            self.assertIn("only for --command goto", out)

    def test_pointing_policy_blocks_through_the_cli(self) -> None:
        with enabled():
            sim = GotoSim()
            low = SimpleNamespace(j2000_to_of_date=self.GEOMETRY.j2000_to_of_date,
                                  target_altitude_and_sun_separation=lambda *a: (5.0, 120.0))
            code, out, report, _, _ = self.main(sim, self.ARGS, geometry=low)
        self.assertEqual((code, sim.goto_calls), (1, []))
        self.assertIn("parameter_gate:target_below_minimum_altitude", report["outcomes"][0]["last_evidence"])

    def test_uncertain_and_declined(self) -> None:
        with enabled():
            sim = GotoSim()
            sim.modes[GOTO] = "fail"
            code, _, report, _, _ = self.main(sim, self.ARGS)
            self.assertEqual((code, report["recovery_required"], len(sim.goto_calls)), (3, True, 1))
            sim = GotoSim()
            code, _, _, _, _ = self.main(sim, self.ARGS, ask=lambda p: "DEPLOY" if "DEPLOY" in p else "no")
            self.assertEqual((code, sim.goto_calls), (1, []))


if __name__ == "__main__":
    unittest.main()
