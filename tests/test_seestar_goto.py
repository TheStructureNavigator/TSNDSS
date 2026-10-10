"""DB-05b: the parameterized GoTo command, offline against a stateful simulated device. Physical GoTo is BLOCKED by default."""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import sys
import tempfile
import unittest
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tsn_dss.engine.device_runtime import ManualClock, SequentialIdGenerator, TelemetrySource
from tsn_dss.engine.device_runtime.command_executor import CommandIntent
from tsn_dss.engine.device_runtime.command_models import CommandKindPolicy, CommandPolicyError, CommandRequest, FreshnessRequirement
from tsn_dss.engine.seestar_control import (
    GOTO, ControlFreshness, ControlPostSendError, ControlPreSendError, GotoTarget, OperatorPermit, SeestarControl, SeestarControlTransport,
)
from tsn_dss.engine.seestar_control import commands as control_commands
from tsn_dss.engine.seestar_control.commands import MountCoordinates, angular_separation_deg
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
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("seestar_command_validate", ROOT / "tools" / "seestar_command_validate.py")
CV = importlib.util.module_from_spec(_spec)
sys.modules["seestar_command_validate"] = CV
_spec.loader.exec_module(CV)

TARGET = GotoTarget(5.5, -5.25)
TOL = 0.5
DEADLINE = timedelta(minutes=10)


class GotoSim(SimSeestar):
    """Mount that slews to a GoTo target in a few ticks. ``offset_deg`` makes it stop short; the other modes mirror SimSeestar."""

    def __init__(self, **kw) -> None:
        super().__init__(arm_closed=False, **kw)
        self.ra, self.dec = 1.0, 20.0
        self.goto_calls: list = []
        self.offset_deg = 0.0
        self.fail_equ = False
        self.fail_device = False

    def send_goto(self, host, target):
        with self.lock:
            self.goto_calls.append((target.ra_hours, target.dec_deg))
            mode = self.modes.get(GOTO, "normal")
            if mode == "pre_send":
                raise ControlPreSendError("connect_failed")
            if mode == "drop":
                raise ControlPostSendError("connection_closed")
            if mode == "error_reply":
                return RpcReply("", 1, None, None)
            if mode != "ack_only":
                def moving(): self.moving = True
                def arrive(): self.moving, self.ra, self.dec = False, target.ra_hours, target.dec_deg + self.offset_deg
                self.pending.extend([moving, arrive])
            if mode == "effect_then_drop":
                raise ControlPostSendError("connection_closed")
            return RpcReply("", 0, 0, None)

    def read_equ_coord(self, host):
        if self.fail_equ:
            raise SeestarUnreachable("connect_failed")
        return parse_reply({"method": "scope_get_equ_coord", "code": 0, "id": 1, "result": {"ra": self.ra, "dec": self.dec}})

    def read_device_state(self, host, keys):
        if self.fail_device:
            raise SeestarUnreachable("connect_failed")
        return super().read_device_state(host, keys)


class Rig:
    def __init__(self, sim=None, *, tolerance=TOL, authorizer=None, grant=True) -> None:
        self.sim = sim or GotoSim()
        self.clock = ManualClock()

        def sleep(seconds):
            self.clock.advance(timedelta(seconds=seconds))
            self.sim.tick()

        self.permit = OperatorPermit(operator_id="op", valid_for=timedelta(hours=6), clock=self.clock)
        self.freshness = ControlFreshness(timedelta(minutes=5), timedelta(minutes=5), goto_tolerance_deg=tolerance)
        self.control = SeestarControl(config=make_config(), read_transport=self.sim, control_transport=self.sim, freshness=self.freshness,
                                      authorizer=authorizer or self.permit, clock=self.clock, id_generator=SequentialIdGenerator(), sleep=sleep)
        self.handle = self.control.attach()
        self.handle.executor.establish_baseline_by_recovery(self.handle.connection)
        if grant and authorizer is None:
            self.permit.grant(self.handle.connection, allow_physical_motion=True, interactive=True, confirm=lambda: "DEPLOY")

    def goto(self, target=TARGET, *, confirm=True, parameters="default"):
        if confirm and authorizer_is_permit(self):
            self.permit.confirm_arm_motion(GOTO, lambda: "ARM")
        params = target if parameters == "default" else parameters
        return self.handle.driver.execute(self.handle.connection, GOTO, "op", deadline=self.clock() + DEADLINE, poll_interval=POLL, parameters=params)


def authorizer_is_permit(rig) -> bool:
    return rig.permit._bound is not None


def enabled():
    return mock.patch.object(control_commands, "GOTO_PHYSICAL_ENABLED", True)


class TargetTests(unittest.TestCase):
    def test_valid_bounds_are_accepted_and_stored_as_floats(self) -> None:
        for ra, dec in ((0, -90), (24, 90), (12.5, 0), (5, 10)):
            target = GotoTarget(ra, dec)
            self.assertEqual((target.ra_hours, target.dec_deg), (float(ra), float(dec)))
            self.assertIsInstance(target.ra_hours, float)

    def test_invalid_targets_are_refused(self) -> None:
        nan, inf = float("nan"), float("inf")
        for ra, dec in ((nan, 0), (0, nan), (inf, 0), (0, -inf), (-0.01, 0), (24.01, 0), (0, 90.01), (0, -90.01),
                        (True, 0), (0, False), ("5", 0), (0, None), (None, None), ([5], 0)):
            with self.assertRaises(ValueError, msg=repr((ra, dec))):
                GotoTarget(ra, dec)

    def test_a_target_is_immutable_and_hashable(self) -> None:
        with self.assertRaises(dataclasses.FrozenInstanceError):
            TARGET.ra_hours = 1.0
        self.assertEqual(hash(TARGET), hash(GotoTarget(5.5, -5.25)))

    def test_angular_separation(self) -> None:
        self.assertAlmostEqual(angular_separation_deg(0, 0, 0, 0), 0.0)
        self.assertAlmostEqual(angular_separation_deg(0, 0, 6, 0), 90.0)
        self.assertAlmostEqual(angular_separation_deg(23.9, 0, 0.1, 0), 3.0, places=6)  # wraps across 24 h
        self.assertAlmostEqual(angular_separation_deg(1, 90, 13, 90), 0.0)  # at the pole RA does not matter


class CorePlumbingTests(unittest.TestCase):
    def test_a_request_keeps_immutable_parameters_and_rejects_mutable_ones(self) -> None:
        from tsn_dss.engine.device_runtime.models import CommandRef

        ref = CommandRef("c", "p", "conn")
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(CommandRequest(ref, "k", "op", now, parameters=TARGET).parameters, TARGET)
        for bad in ({"ra": 1}, [1, 2]):
            with self.assertRaises(CommandPolicyError):
                CommandRequest(ref, "k", "op", now, parameters=bad)

    def test_the_policy_decides_whether_parameters_are_accepted(self) -> None:
        from tsn_dss.engine.device_runtime.models import CommandRef

        ref, now = CommandRef("c", "p", "conn"), datetime(2026, 1, 1, tzinfo=timezone.utc)
        fixed = CommandKindPolicy("k", state_changing=True, physical=True, idempotent=False, safety_sensitive=True,
                                  freshness=(FreshnessRequirement("x", timedelta(seconds=1)),))
        with_params = dataclasses.replace(fixed, takes_parameters=True)
        self.assertEqual(CommandRequest(ref, "k", "op", now, parameters=TARGET).violates_policy(fixed), "parameters_not_accepted")
        self.assertEqual(CommandRequest(ref, "k", "op", now).violates_policy(with_params), "parameters_required")
        self.assertIsNone(CommandRequest(ref, "k", "op", now, parameters=TARGET).violates_policy(with_params))
        self.assertIsNone(CommandRequest(ref, "k", "op", now).violates_policy(fixed))

    def test_the_four_fixed_commands_never_accept_parameters(self) -> None:
        with enabled():
            rig = Rig(GotoSim())
            for kind in ("seestar.arm.deploy", "seestar.scenery.stop"):
                rig.permit.confirm_arm_motion("seestar.arm.deploy", lambda: "ARM") if kind.endswith("deploy") else None
                outcome = rig.handle.driver.execute(rig.handle.connection, kind, "op", deadline=rig.clock() + DEADLINE,
                                                    poll_interval=POLL, parameters=TARGET)
                self.assertEqual(outcome.classification, "rejected", kind)
            self.assertEqual(rig.sim.control_calls, [])

    def test_a_goto_without_a_target_is_rejected_before_anything_is_sent(self) -> None:
        with enabled():
            rig = Rig()
            outcome = rig.goto(parameters=None)
            self.assertEqual(outcome.classification, "rejected")
            self.assertEqual(rig.sim.goto_calls, [])

    def test_a_wrong_typed_parameter_never_reaches_the_wire(self) -> None:
        with enabled():
            rig = Rig()
            outcome = rig.goto(parameters=(5.5, -5.25))  # hashable, but not a validated GotoTarget
            self.assertNotEqual(outcome.classification, "succeeded")
            self.assertEqual(rig.sim.goto_calls, [])
            self.assertFalse(outcome.uncertainty_open)


class BlockedByDefaultTests(unittest.TestCase):
    def test_physical_goto_is_blocked_even_when_authorized_and_every_gate_passes(self) -> None:
        self.assertIs(control_commands.GOTO_PHYSICAL_ENABLED, False)
        rig = Rig()
        outcome = rig.goto()
        self.assertEqual(rig.sim.goto_calls, [])  # nothing left the process
        self.assertEqual(outcome.classification, "failed")
        self.assertFalse(outcome.uncertainty_open)

    def test_the_cli_refuses_before_building_anything(self) -> None:
        built = []
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = CV.main(["--host", HOST, "--operator", "op", "--telemetry-max-age", "10", "--capability-max-age", "10",
                            "--allow-physical-motion", "--command-deadline", "90", "--poll-interval", "2", "--permit-validity", "600",
                            "--command", "goto", "--ra-hours", "5.5", "--dec-deg", "-5.25", "--goto-tolerance-deg", "0.5",
                            "--out", str(Path(tempfile.gettempdir()) / "never_written_goto.json")],
                           ask=lambda p: "x", control_factory=lambda f, p: built.append(1))
        self.assertEqual((code, built), (2, []))
        self.assertIn("BLOCKED", out.getvalue())


class GotoCycleTests(unittest.TestCase):
    def test_a_goto_is_verified_only_by_fresh_stationary_coordinates_near_the_target(self) -> None:
        with enabled():
            rig = Rig()
            outcome = rig.goto()
            self.assertEqual(outcome.classification, "succeeded")
            self.assertEqual(rig.sim.goto_calls, [(5.5, -5.25)])
            self.assertEqual(rig.sim.control_calls, [])  # no deploy, park, camera or tracking command
            self.assertIn("within", outcome.last_evidence)

    def test_stopping_short_is_never_success(self) -> None:
        with enabled():
            sim = GotoSim()
            sim.offset_deg = 5.0
            outcome = Rig(sim).goto()
            self.assertEqual(outcome.classification, "unknown_result")
            self.assertTrue(outcome.uncertainty_open)

    def test_an_acknowledgement_that_never_moves_is_unknown_not_success(self) -> None:
        with enabled():
            sim = GotoSim()
            sim.modes[GOTO] = "ack_only"
            rig = Rig(sim)
            outcome = rig.goto()
            self.assertEqual((outcome.classification, outcome.uncertainty_open), ("unknown_result", True))
            self.assertEqual(sim.goto_calls, [(5.5, -5.25)])  # exactly one send: no retry

    def test_without_a_configured_tolerance_nothing_can_be_verified(self) -> None:
        with enabled():
            self.assertEqual(Rig(tolerance=None).goto().classification, "unknown_result")

    def test_unreadable_coordinates_leave_the_result_unknown(self) -> None:
        with enabled():
            sim = GotoSim()
            sim.fail_equ = True
            self.assertEqual(Rig(sim).goto().classification, "unknown_result")

    def test_a_dropped_connection_is_unknown_and_blocks_the_next_goto(self) -> None:
        with enabled():
            for mode in ("effect_then_drop", "drop", "error_reply"):
                sim = GotoSim()
                sim.modes[GOTO] = mode
                rig = Rig(sim)
                first = rig.goto()
                self.assertEqual((first.classification, first.uncertainty_open), ("unknown_result", True), mode)
                second = rig.goto()
                self.assertEqual(second.classification, "safety_blocked", mode)  # unresolved uncertainty blocks it
                self.assertEqual(len(sim.goto_calls), 1, mode)  # never retried, never resent

    def test_a_failure_before_the_frame_is_a_plain_failure(self) -> None:
        with enabled():
            sim = GotoSim()
            sim.modes[GOTO] = "pre_send"
            outcome = Rig(sim).goto()
            self.assertEqual((outcome.classification, outcome.uncertainty_open), ("failed", False))

    def test_denied_states_send_nothing(self) -> None:
        with enabled():
            closed = Rig(GotoSim())
            closed.sim.arm_closed = True
            moving = Rig(GotoSim())
            moving.sim.moving = True
            for label, rig in (("arm closed", closed), ("mount moving", moving)):
                outcome = rig.goto()
                self.assertEqual(outcome.classification, "safety_blocked", label)
                self.assertEqual(rig.sim.goto_calls, [], label)

    def test_unreadable_telemetry_at_the_gate_fails_closed(self) -> None:
        with enabled():
            rig = Rig()
            rig.sim.fail_device = True
            outcome = rig.goto()
            self.assertEqual(outcome.classification, "safety_blocked")
            self.assertEqual(rig.sim.goto_calls, [])

    def test_authorization_is_required(self) -> None:
        with enabled():
            ungranted = Rig(grant=False)
            self.assertEqual(ungranted.goto(confirm=False).classification, "safety_blocked")
            no_confirmation = Rig()
            self.assertEqual(no_confirmation.goto(confirm=False).classification, "safety_blocked")  # the single-use ARM confirmation is missing
            self.assertEqual(no_confirmation.sim.goto_calls, [])
            once = Rig()
            self.assertEqual(once.goto().classification, "succeeded")
            self.assertEqual(once.goto(confirm=False).classification, "safety_blocked")  # the confirmation was consumed

    def test_goto_does_not_depend_on_or_use_the_camera_state(self) -> None:
        package = ROOT / "tsn_dss" / "engine" / "seestar_control"
        text = "".join(p.read_text(encoding="utf-8") for p in package.glob("*.py"))
        for name in ("get_camera_state", "read_camera_state"):
            self.assertNotIn(name, text)
        policy_items = {r.item for r in __import__("tsn_dss.engine.seestar_control.kinds", fromlist=["x"]).build_goto_policy(
            ControlFreshness(timedelta(seconds=1), timedelta(seconds=1))).freshness}
        self.assertFalse([i for i in policy_items if i.startswith("app.")])


class VerifierUnitTests(unittest.TestCase):
    """The verdict logic with scripted evidence: stale, early or doubtful evidence is PENDING, never failed or verified."""

    T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def run_case(self, *, moving=False, coords=(5.5, -5.25), coord_at=None, sample_at=None, boundary=None, tol=TOL):
        from tsn_dss.engine.device_runtime import TelemetryItem, TelemetrySample, ValueState
        from tsn_dss.engine.device_runtime.command_effects import EffectVerdictKind

        boundary = boundary or self.T0
        now = boundary + timedelta(seconds=30)
        pr = TelemetrySource.PROVIDER_REPORTED
        sample = TelemetrySample("seestar", "conn-1", sample_at or boundary + timedelta(seconds=20),
                                 (TelemetryItem("mount.move_type", pr, ValueState.KNOWN, "move" if moving else "none"),), False)

        class Runtime:
            def read_telemetry(self, connection):
                return sample

        record = type("R", (), {})()
        record.parameters, record.provider_id = TARGET, "seestar"
        record.policy = type("P", (), {"kind_id": GOTO})()
        record.history = (type("H", (), {"to_state": "submitted", "at": boundary})(),)
        connection = type("C", (), {"connection_id": "conn-1"})()
        reading = None if coords is None else MountCoordinates(coords[0], coords[1], coord_at or boundary + timedelta(seconds=20))
        freshness = ControlFreshness(timedelta(minutes=1), timedelta(minutes=1), goto_tolerance_deg=tol)
        verdict = build_goto_verifier(Runtime(), lambda c: reading, freshness, lambda: now)(connection, record)
        self.assertNotEqual(verdict.kind, EffectVerdictKind.FAILED)
        return verdict.kind == EffectVerdictKind.VERIFIED

    def test_only_fresh_stationary_close_evidence_verifies(self) -> None:
        self.assertTrue(self.run_case())
        self.assertFalse(self.run_case(moving=True))
        self.assertFalse(self.run_case(coords=(5.5, -3.0)))
        self.assertFalse(self.run_case(coords=None))
        self.assertFalse(self.run_case(tol=None))
        self.assertFalse(self.run_case(coord_at=self.T0))  # not strictly after the boundary
        self.assertFalse(self.run_case(sample_at=self.T0))
        self.assertFalse(self.run_case(coord_at=self.T0 + timedelta(minutes=3), sample_at=self.T0 + timedelta(seconds=20)))  # stale
        self.assertTrue(self.run_case(coords=(5.5, -5.25 + TOL * 0.99)))
        self.assertFalse(self.run_case(coords=(5.5, -5.25 + TOL * 1.01)))


class WireTests(unittest.TestCase):
    def test_the_real_transport_sends_a_positional_scope_goto_after_the_handshake(self) -> None:
        device = ScriptedDevice(auth="required")
        transport = SeestarControlTransport(make_config(), StubAuthenticator(), connect_factory=device.connect)
        reply = transport.send_goto(HOST, TARGET)
        self.assertEqual(reply.code, 0)
        self.assertEqual(device.goto_calls, [[5.5, -5.25]])
        self.assertEqual(device.methods, ["get_verify_str", "verify_client", "pi_is_verified", "scope_goto"])
        self.assertTrue(all(s.closed for s in device.sockets))

    def test_only_a_validated_target_can_be_sent(self) -> None:
        device = ScriptedDevice()
        transport = SeestarControlTransport(make_config(), StubAuthenticator(), connect_factory=device.connect)
        for bad in ((5.5, -5.25), {"ra": 1, "dec": 2}, None, "5.5"):
            with self.assertRaises(ControlPreSendError):
                transport.send_goto(HOST, bad)
        self.assertEqual(device.connect_addresses, [])  # refused before any socket

    def test_the_read_only_allow_list_still_refuses_goto(self) -> None:
        from tsn_dss.engine.seestar_provider.errors import SeestarMethodNotAllowed
        from tsn_dss.engine.seestar_provider.protocol import encode_read_request

        with self.assertRaises(SeestarMethodNotAllowed):
            encode_read_request(1, "scope_goto", {"ra": 1})


class CliTests(unittest.TestCase):
    ARGS = ["--telemetry-max-age", "10", "--capability-max-age", "10", "--allow-physical-motion", "--command-deadline", "90",
            "--poll-interval", "2", "--permit-validity", "600", "--command", "goto", "--ra-hours", "5.5", "--dec-deg", "-5.25",
            "--goto-tolerance-deg", "0.5"]

    def main(self, sim, args, ask=None):
        log = []

        def default_ask(prompt):
            log.append(prompt)
            return "DEPLOY" if "DEPLOY" in prompt else "ARM" if "Type ARM" in prompt else "GO"

        clock = ManualClock()

        def factory(freshness, permit):
            return SeestarControl(config=make_config(), read_transport=sim, control_transport=sim, freshness=freshness, authorizer=permit,
                                  clock=clock, id_generator=SequentialIdGenerator(),
                                  sleep=lambda s: (clock.advance(timedelta(seconds=s)), sim.tick()))

        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(out):
            target = Path(tmp) / "r.json"
            code = CV.main(["--host", HOST, "--operator", "op", "--out", str(target), *args], ask=ask or default_ask,
                           control_factory=factory, clock=clock, coordinates_reader=lambda: (sim.ra, sim.dec))
            report = json.loads(target.read_text(encoding="utf-8")) if target.exists() else None
        return code, out.getvalue(), report, log

    def test_an_enabled_goto_runs_alone_and_reports_the_target_without_assuming_a_frame(self) -> None:
        with enabled():
            sim = GotoSim()
            code, out, report, log = self.main(sim, self.ARGS)
        self.assertEqual(code, 0, out)
        self.assertEqual(sim.goto_calls, [(5.5, -5.25)])
        self.assertEqual(sim.control_calls, [])
        self.assertEqual((report["mode"], report["command"]), ("physical_single_command", GOTO))
        self.assertEqual(report["target"], {"ra_hours": 5.5, "dec_deg": -5.25})
        self.assertIn("unknown", report["coordinate_frame"])
        self.assertEqual(report["final_reported_coordinates"], {"ra_hours": 5.5, "dec_deg": -5.25})
        self.assertEqual(report["cleanup"], "not_needed")
        self.assertEqual([p.split()[1] for p in log], ["DEPLOY", "GO", "ARM"])
        self.assertIn("(frame unknown)", out)
        self.assertNotIn(HOST, json.dumps(report))

    def test_an_uncertain_goto_exits_three(self) -> None:
        with enabled():
            sim = GotoSim()
            sim.modes[GOTO] = "ack_only"
            code, out, report, _ = self.main(sim, self.ARGS)
        self.assertEqual(code, 3)
        self.assertTrue(report["recovery_required"])
        self.assertEqual(sim.goto_calls, [(5.5, -5.25)])

    def test_a_declined_confirmation_sends_nothing(self) -> None:
        with enabled():
            sim = GotoSim()
            code, _, report, _ = self.main(sim, self.ARGS, ask=lambda p: "DEPLOY" if "DEPLOY" in p else "no")
        self.assertEqual((code, sim.goto_calls), (1, []))

    def test_argument_validation(self) -> None:
        with enabled():
            base = [a for a in self.ARGS]
            cases = [
                (["--ra-hours", "25"], "valid"), (["--dec-deg", "91"], "valid"), (["--ra-hours", "nan"], "valid"),
                (["--goto-tolerance-deg", "0"], "goto-tolerance-deg"),
            ]
            for override, text in cases:
                argv = list(base)
                i = argv.index(override[0])
                argv[i + 1] = override[1]
                code, out, _, _ = self.main(GotoSim(), argv)
                self.assertEqual(code, 2, override)
                self.assertIn(text, out)
            for missing in ("--ra-hours", "--dec-deg", "--goto-tolerance-deg"):
                argv = list(base)
                i = argv.index(missing)
                del argv[i:i + 2]
                code, out, _, _ = self.main(GotoSim(), argv)
                self.assertEqual(code, 2, missing)
            code, out, _, _ = self.main(GotoSim(), ["park" if a == "goto" else a for a in base])
            self.assertEqual(code, 2)
            self.assertIn("only for --command goto", out)


if __name__ == "__main__":
    unittest.main()
