"""DB-05 Slice 3: the operator-run validation tool, offline. A stateful simulated Seestar, a manual clock and scripted consent;
no hardware, no network, no movement."""

from __future__ import annotations

import ast
import importlib.util
import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

from tsn_dss.engine.device_runtime import ManualClock, SequentialIdGenerator
from tsn_dss.engine.device_runtime.command_executor import ClearanceNotAuthorized
from tsn_dss.engine.seestar_provider.protocol import parse_reply
from tsn_dss.engine.seestar_control import (
    ARM_DEPLOY, ARM_PARK, COMMANDS, SCENERY_START, SCENERY_STOP, OperatorPermit, SeestarControl,
)

try:
    from seestar_support import HOST, make_config
    from test_seestar_control_runtime import Allow, POLL, SimSeestar
except ImportError:  # pragma: no cover
    from tests.seestar_support import HOST, make_config
    from tests.test_seestar_control_runtime import Allow, POLL, SimSeestar

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "seestar_command_validate.py"
spec = importlib.util.spec_from_file_location("seestar_command_validate", SCRIPT)
CV = importlib.util.module_from_spec(spec)
sys.modules["seestar_command_validate"] = CV
spec.loader.exec_module(CV)

SERIAL = "SN-SENTINEL-0042"
DEADLINE = timedelta(minutes=10)
GOOD_PREVIEW = {"overall": {"verdict": "PASS"}, "cameras": [{"camera": "main", "frames": 1, "format": "bgr", "width": 4, "height": 4, "verdict": "PASS"}]}


def answers(grant="DEPLOY", arm="ARM", log=None):
    def ask(prompt: str) -> str:
        if log is not None:
            log.append(prompt)
        return grant if "DEPLOY" in prompt else arm
    return ask


class Rig:
    """The tool's own composition against the simulated device, with the permit as the only authorizer."""

    def __init__(self, sim=None, *, validity=timedelta(hours=6), authorizer=None) -> None:
        self.sim = sim or SimSeestar()
        self.clock = ManualClock()

        def sleep(seconds: float) -> None:
            self.clock.advance(timedelta(seconds=seconds))
            self.sim.tick()

        self.permit = OperatorPermit(operator_id="op", valid_for=validity, clock=self.clock)
        self.control = SeestarControl(
            config=make_config(), read_transport=self.sim, control_transport=self.sim, freshness=CV_FRESH(),
            authorizer=authorizer or self.permit, clock=self.clock, id_generator=SequentialIdGenerator(), sleep=sleep)

    def run(self, *, physical=True, ask=None, preview=lambda n: GOOD_PREVIEW, **over):
        args = dict(operator="op", physical=physical, ask=ask or answers(), preview=preview, frames=1, deadline=DEADLINE,
                    poll_interval=POLL, clock=self.clock)
        args.update(over)
        return CV.run_validation(self.control, self.permit if physical else None, **args)

    def stage(self, report, name):
        return next(s for s in report["stages"] if s["name"] == name)


def CV_FRESH():
    from tsn_dss.engine.seestar_control import ControlFreshness

    return ControlFreshness(telemetry_max_age=timedelta(minutes=5), capability_max_age=timedelta(minutes=5))


class ReadOnlyTests(unittest.TestCase):
    def test_read_only_reports_state_and_sends_nothing(self) -> None:
        rig = Rig()
        report = rig.run(physical=False, ask=None)
        self.assertEqual(report["overall"], "READ_ONLY")
        self.assertEqual(report["mode"], "read_only")
        self.assertEqual(report["initial_state"], {"arm": "closed", "cameras": "stopped"})
        self.assertEqual(report["commands_sent"], [])
        self.assertEqual(rig.sim.control_calls, [])
        self.assertFalse(report["unsafe_or_unknown_final_state"])
        self.assertTrue(report["device_left_as_found"])
        self.assertEqual(rig.control.uncertainty_store.unresolved_devices(), frozenset())

    def test_the_report_carries_the_six_raw_state_items_and_the_read_duration(self) -> None:
        sim = SimSeestar()
        sim.serial = SERIAL
        rig = Rig(sim)
        report = rig.run(physical=False, ask=None)
        items = report["observed_items"]
        self.assertEqual(set(items), {"mount.move_type", "mount.arm_closed", "app.main.state", "app.main.rtsp_state",
                                      "app.wide.state", "app.wide.rtsp_state"})
        self.assertEqual(items["mount.move_type"], {"state": "known", "value": "none"})
        self.assertEqual(items["mount.arm_closed"], {"state": "known", "value": True})
        self.assertEqual(items["app.main.state"], {"state": "known", "value": "cancel"})
        self.assertGreaterEqual(report["read_seconds"], 0)
        self.assertNotIn(SERIAL, json.dumps(report))
        # a missing or unknown item keeps its distinction instead of becoming a guess
        sample = type("S", (), {"get": lambda self, name, source: None})()
        self.assertEqual(CV.observed_items(sample)["app.main.state"], {"state": "missing"})

    def test_main_without_the_flag_is_read_only_and_never_asks(self) -> None:
        rig = Rig()
        asked = []
        code, out, report = run_main(rig, ["--telemetry-max-age", "300", "--capability-max-age", "300"], ask=lambda p: asked.append(p) or "")
        self.assertEqual(code, 0)
        self.assertEqual(asked, [])
        self.assertEqual(rig.sim.control_calls, [])
        self.assertEqual(report["mode"], "read_only")


class CycleTests(unittest.TestCase):
    def test_the_four_command_cycle_runs_through_the_runtime(self) -> None:
        rig = Rig()
        report = rig.run()
        self.assertEqual(report["overall"], "PASS")
        self.assertEqual(report["commands_sent"], [ARM_DEPLOY, SCENERY_START, SCENERY_STOP, ARM_PARK])
        self.assertEqual(rig.sim.control_calls, [ARM_DEPLOY, SCENERY_START, SCENERY_STOP, ARM_PARK])
        self.assertEqual([o["classification"] for o in report["outcomes"]], ["succeeded"] * 4)
        self.assertEqual([s["status"] for s in report["stages"]], ["PASS"] * 7)
        self.assertEqual(report["final_state"], {"arm": "closed", "cameras": "stopped"})
        self.assertFalse(report["unsafe_or_unknown_final_state"])
        self.assertFalse(report["recovery_required"])
        self.assertEqual(report["preview"][0]["camera"], "main")

    def test_each_arm_movement_asks_again(self) -> None:
        log = []
        Rig().run(ask=answers(log=log))
        self.assertEqual(len(log), 3)  # one DEPLOY, then ARM before deploy and ARM before park
        self.assertIn("DEPLOY", log[0])
        self.assertTrue(all("ARM" in p for p in log[1:]))

    def test_the_preview_is_skippable_and_failure_still_cleans_up(self) -> None:
        rig = Rig()
        report = rig.run(preview=None)
        self.assertEqual(rig.stage(report, "preview")["detail"], "skipped_by_operator")
        rig = Rig()
        report = rig.run(preview=lambda n: {"overall": {"verdict": "FAIL", "fail_reasons": ["no_frames"]}, "cameras": []})
        self.assertEqual(rig.stage(report, "preview")["detail"], "preview_no_frames")
        self.assertEqual(rig.sim.control_calls, [ARM_DEPLOY, SCENERY_START, SCENERY_STOP, ARM_PARK])  # cleanup ran
        self.assertEqual(report["overall"], "FAIL")
        self.assertFalse(report["unsafe_or_unknown_final_state"])

    def test_an_unexpected_start_state_stops_before_any_command(self) -> None:
        for sim in (SimSeestar(arm_closed=False), SimSeestar(cameras="ready")):
            rig = Rig(sim)
            report = rig.run()
            self.assertEqual(rig.stage(report, "baseline")["detail"], "start_state_not_folded_and_stopped")
            self.assertEqual(sim.control_calls, [])
            self.assertTrue(report["device_left_as_found"])
            self.assertFalse(report["unsafe_or_unknown_final_state"])

    def test_the_report_is_stop_on_first_failure(self) -> None:
        sim = SimSeestar()
        sim.modes[ARM_DEPLOY] = "pre_send"
        rig = Rig(sim)
        report = rig.run()
        self.assertEqual(report["overall"], "FAIL")
        self.assertEqual(rig.stage(report, "start_scenery")["status"], "NOT_RUN")
        self.assertEqual(sim.control_calls, [ARM_DEPLOY])  # never retried
        self.assertEqual(report["final_state"], {"arm": "closed", "cameras": "stopped"})
        self.assertFalse(report["unsafe_or_unknown_final_state"])


class AuthorizationTests(unittest.TestCase):
    def test_no_control_command_before_authorization(self) -> None:
        for ask in (answers(grant="no"), answers(grant="")):
            rig = Rig()
            report = rig.run(ask=ask)
            self.assertEqual(rig.sim.control_calls, [])
            self.assertEqual(report["commands_sent"], [])
            self.assertIn("confirmation_not_given", rig.stage(report, "baseline")["detail"])
            self.assertEqual(rig.permit.remaining(), None)

    def test_an_ungranted_permit_blocks_the_driver_itself(self) -> None:
        rig = Rig()
        handle = rig.control.attach()
        handle.executor.establish_baseline_by_recovery(handle.connection)
        outcome = handle.driver.execute(handle.connection, SCENERY_STOP, "op", deadline=rig.clock() + DEADLINE, poll_interval=POLL)
        self.assertEqual(outcome.classification, "safety_blocked")
        self.assertEqual(rig.sim.control_calls, [])

    def test_a_wrong_arm_confirmation_sends_nothing_and_cleanup_has_nothing_to_do(self) -> None:
        rig = Rig()
        report = rig.run(ask=answers(arm="nope"))
        self.assertEqual(rig.sim.control_calls, [])
        self.assertEqual(rig.stage(report, "deploy_arm")["detail"], "arm_confirmation_not_given")
        self.assertTrue(report["device_left_as_found"])

    def test_the_permit_is_bound_to_the_identified_device_only(self) -> None:
        rig = Rig()
        other = Rig()
        other.permit.grant(other.control.attach().connection, allow_physical_motion=True, interactive=True, confirm=lambda: "DEPLOY")
        handle = rig.control.attach()
        handle.executor.establish_baseline_by_recovery(handle.connection)
        # `other` is a different permit, bound to a different (simulated) device attachment; this run's permit was never granted
        outcome = handle.driver.execute(handle.connection, SCENERY_STOP, "op", deadline=rig.clock() + DEADLINE, poll_interval=POLL)
        self.assertEqual(outcome.classification, "safety_blocked")

    def test_an_expired_permit_stops_the_run_and_cleanup_is_denied_too(self) -> None:
        rig = Rig(validity=timedelta(seconds=45))
        report = rig.run()
        self.assertEqual(rig.sim.control_calls[0], ARM_DEPLOY)
        self.assertNotIn(SCENERY_START, rig.sim.control_calls)
        self.assertEqual(rig.stage(report, "start_scenery")["detail"], "command_safety_blocked")
        self.assertEqual(report["overall"], "FAIL")
        # the arm was left open: the tool says so instead of claiming a safe cleanup
        self.assertTrue(report["unsafe_or_unknown_final_state"])
        self.assertEqual(report["final_state"]["arm"], "open")

    def test_the_tool_passes_no_clearance_authorizer(self) -> None:
        rig = Rig()
        handle = rig.control.attach()
        with self.assertRaises(ClearanceNotAuthorized):
            handle.executor.establish_baseline_by_operator(handle.connection.device.device_ref, "op", "reason")


class UncertaintyTests(unittest.TestCase):
    def uncertain_rig(self, kind_id, mode):
        sim = SimSeestar()
        sim.modes[kind_id] = mode
        return Rig(sim)

    def test_an_uncertain_outcome_requires_recovery_and_blocks_all_further_commands(self) -> None:
        rig = self.uncertain_rig(SCENERY_START, "ack_only")
        report = rig.run()
        self.assertTrue(report["recovery_required"])
        self.assertTrue(report["unsafe_or_unknown_final_state"])
        self.assertEqual(report["overall"], "FAIL")
        self.assertEqual(report["cleanup"], "not_attempted_recovery_required")
        self.assertEqual(rig.sim.control_calls, [ARM_DEPLOY, SCENERY_START])  # no stop, no park, no retry
        self.assertEqual(report["outcomes"][-1]["classification"], "unknown_result")
        self.assertTrue(report["outcomes"][-1]["uncertainty_open"])
        self.assertEqual(len(rig.control.uncertainty_store.unresolved_devices()), 1)  # not cleared, not recovered

    def test_the_tool_does_not_recover_an_uncertainty_on_its_own(self) -> None:
        rig = self.uncertain_rig(ARM_DEPLOY, "effect_then_drop")
        report = rig.run()
        self.assertTrue(report["recovery_required"])
        self.assertEqual(rig.sim.control_calls, [ARM_DEPLOY])
        self.assertEqual(len(rig.control.uncertainty_store.unresolved_devices()), 1)

    def test_a_failed_cleanup_is_reported_unsafe(self) -> None:
        sim = SimSeestar()
        sim.modes[SCENERY_STOP] = "pre_send"
        rig = Rig(sim)
        report = rig.run()
        self.assertEqual(rig.stage(report, "stop_scenery")["status"], "FAIL")
        self.assertEqual(rig.stage(report, "park_arm")["status"], "NOT_RUN")  # refuses to park with cameras running
        self.assertNotIn(ARM_PARK, sim.control_calls)
        self.assertEqual(report["final_state"], {"arm": "open", "cameras": "ready"})
        self.assertTrue(report["unsafe_or_unknown_final_state"])
        self.assertFalse(report["recovery_required"])
        self.assertEqual(sim.control_calls.count(SCENERY_STOP), 1)  # never retried

    def test_cleanup_uncertainty_is_not_a_safe_cleanup(self) -> None:
        rig = self.uncertain_rig(ARM_PARK, "ack_only")
        report = rig.run()
        self.assertTrue(report["recovery_required"])
        self.assertTrue(report["unsafe_or_unknown_final_state"])
        self.assertEqual(rig.sim.control_calls.count(ARM_PARK), 1)

    def test_an_open_uncertainty_is_unsafe_even_when_the_device_reads_as_folded(self) -> None:
        sim = SimSeestar()
        sim.modes[ARM_PARK] = "effect_then_drop"  # the arm does fold, but the command's own result stays unknown
        rig = Rig(sim)
        run = CV.Run(rig.control, rig.permit, operator="op", physical=True, ask=answers(), preview=lambda n: GOOD_PREVIEW, frames=1,
                     deadline=DEADLINE, poll_interval=POLL, clock=rig.clock)
        run.execute()
        while sim.pending:  # the device finishes folding after the tool gave up on the command
            sim.tick()
        report = run.report()
        self.assertEqual(report["final_state"], {"arm": "closed", "cameras": "stopped"})
        self.assertTrue(report["recovery_required"])
        self.assertTrue(report["unsafe_or_unknown_final_state"])
        self.assertEqual(report["overall"], "FAIL")

    def test_an_unreadable_state_is_unknown_not_safe(self) -> None:
        rig = Rig()
        original = rig.control.runtime.read_telemetry

        silent = []

        def flaky(connection):
            if silent:  # the device stops answering after the preview
                raise RuntimeError("secret detail 192.0.2.99")
            return original(connection)

        with mock.patch.object(rig.control.runtime, "read_telemetry", flaky):
            report = rig.run(preview=lambda n: silent.append(1) or GOOD_PREVIEW)
        self.assertEqual(rig.stage(report, "stop_scenery")["detail"], "state_unreadable")
        self.assertNotIn(SCENERY_STOP, rig.sim.control_calls)  # no blind commands
        self.assertEqual(report["final_state"], {"arm": "unknown", "cameras": "unknown"})
        self.assertTrue(report["unsafe_or_unknown_final_state"])
        self.assertNotIn("192.0.2.99", json.dumps(report))

    def test_an_exception_after_possible_submission_requires_recovery_and_stays_unsafe(self) -> None:
        rig = Rig()

        def explode(*args, **kwargs):
            rig.sim.control_calls.append("noted")
            raise RuntimeError("secret detail 192.0.2.99")

        run = CV.Run(rig.control, rig.permit, operator="op", physical=True, ask=answers(), preview=None, frames=0,
                     deadline=DEADLINE, poll_interval=POLL, clock=rig.clock)
        original = run.command

        def command(kind_id):
            if kind_id == SCENERY_START:
                run.handle.driver.execute = explode  # raised by the driver after the deploy command succeeded
            return original(kind_id)

        run.command = command
        run.execute()
        while rig.sim.pending:
            rig.sim.tick()
        report = run.report()  # the device now reads as folded and stopped, which must not downgrade the verdict
        self.assertTrue(report["recovery_required"])
        self.assertTrue(report["unsafe_or_unknown_final_state"])
        self.assertEqual(report["cleanup"], "not_attempted_recovery_required")
        self.assertEqual(rig.sim.control_calls, [ARM_DEPLOY, "noted"])  # no stop, no park
        self.assertEqual(rig.stage(report, "start_scenery")["detail"], "error_RuntimeError")
        self.assertEqual(report["outcomes"][-1], {"kind": SCENERY_START, "classification": "exception", "error": "RuntimeError"})
        self.assertNotIn("192.0.2.99", json.dumps(report))

    def test_an_interrupt_mid_command_is_recovery_required_without_cleanup(self) -> None:
        sim = SimSeestar()
        original = sim.send_command

        def interrupted(host, kind_id):
            if kind_id == SCENERY_START:
                raise KeyboardInterrupt
            return original(host, kind_id)

        sim.send_command = interrupted
        rig = Rig(sim)
        report = rig.run()
        self.assertTrue(report["interrupted"])
        self.assertTrue(report["recovery_required"])  # the command was in flight: its result is not known
        self.assertTrue(report["unsafe_or_unknown_final_state"])
        self.assertEqual(report["cleanup"], "not_attempted_recovery_required")
        self.assertEqual(sim.control_calls, [ARM_DEPLOY])


def scripted(log, *, decline=(), go="GO", nxt="NEXT"):
    """An operator who answers every prompt correctly except the ones whose text contains a ``decline`` marker."""
    def ask(prompt: str) -> str:
        log.append(prompt)
        if any(marker in prompt for marker in decline):
            return "no"
        for phrase, answer in (("DEPLOY", "DEPLOY"), ("ARM", "ARM"), ("GO", go), ("NEXT", nxt)):
            if f"Type {phrase} " in prompt:
                return answer
        return "no"
    return ask


class StepByStepTests(unittest.TestCase):
    def test_every_command_is_confirmed_before_and_after_and_nothing_advances_alone(self) -> None:
        log = []
        rig = Rig()
        with redirect_stdout(io.StringIO()) as out:
            report = rig.run(ask=scripted(log), step=True)
        self.assertEqual(report["overall"], "PASS")
        self.assertEqual(rig.sim.control_calls, [ARM_DEPLOY, SCENERY_START, SCENERY_STOP, ARM_PARK])
        kinds = [p.split()[1] for p in log]  # the phrase asked for: DEPLOY, GO, ARM, NEXT, ...
        self.assertEqual(kinds, ["DEPLOY", "GO", "ARM", "NEXT", "GO", "NEXT", "GO", "NEXT", "GO", "ARM"])
        shown = out.getvalue()
        self.assertIn("next command: seestar.arm.deploy; arm=closed cameras=stopped", shown)
        self.assertIn("seestar.scenery.start: succeeded", shown)
        self.assertIn("arm=open cameras=ready", shown)

    def test_declining_the_first_command_sends_nothing(self) -> None:
        log = []
        rig = Rig()
        with redirect_stdout(io.StringIO()):
            report = rig.run(ask=scripted(log, decline=("Type GO",)), step=True)
        self.assertEqual(rig.sim.control_calls, [])
        self.assertEqual(rig.stage(report, "deploy_arm")["detail"], "operator_declined_command")
        self.assertTrue(report["device_left_as_found"])
        self.assertFalse(any("Type ARM" in p for p in log))  # the arm confirmation is only asked once the operator said GO

    def test_cleanup_commands_are_confirmed_too(self) -> None:
        log = []
        rig = Rig()
        answer = scripted(log, decline=("Type NEXT",))
        with redirect_stdout(io.StringIO()):
            report = rig.run(ask=answer, step=True)
        # after the operator stopped, cleanup still needs GO (and ARM) per command; here they were given
        self.assertEqual(rig.stage(report, "start_scenery")["status"], "NOT_RUN")
        self.assertEqual(rig.stage(report, "deploy_arm")["detail"], "operator_stopped_after_command")
        self.assertEqual(rig.sim.control_calls, [ARM_DEPLOY, ARM_PARK])  # cameras were already stopped: only the park is needed
        self.assertEqual([p.split()[1] for p in log], ["DEPLOY", "GO", "ARM", "NEXT", "GO", "ARM"])
        self.assertFalse(report["unsafe_or_unknown_final_state"])

        rig = Rig()
        with redirect_stdout(io.StringIO()):
            report = rig.run(ask=scripted([], decline=("Type NEXT", "Type GO to send seestar.arm.park")), step=True)
        self.assertEqual(rig.sim.control_calls, [ARM_DEPLOY])
        self.assertTrue(report["unsafe_or_unknown_final_state"])  # the arm is open and the tool says so

    def test_an_unexpected_state_stops_progression(self) -> None:
        rig = Rig()
        wrong = dict(CV.EXPECTED_AFTER, **{ARM_DEPLOY: {"arm": "closed", "cameras": "stopped"}})
        with mock.patch.object(CV, "EXPECTED_AFTER", wrong), redirect_stdout(io.StringIO()):
            report = rig.run(ask=scripted([], decline=("Type GO to send seestar.arm.park",)), step=True)
        self.assertEqual(rig.stage(report, "deploy_arm")["detail"], "unexpected_state_after_command")
        self.assertNotIn(SCENERY_START, rig.sim.control_calls)

    def test_an_uncertain_result_stops_without_cleanup_or_further_prompts(self) -> None:
        log = []
        sim = SimSeestar()
        sim.modes[SCENERY_START] = "ack_only"
        rig = Rig(sim)
        with redirect_stdout(io.StringIO()):
            report = rig.run(ask=scripted(log), step=True)
        self.assertTrue(report["recovery_required"])
        self.assertEqual(report["cleanup"], "not_attempted_recovery_required")
        self.assertEqual(sim.control_calls, [ARM_DEPLOY, SCENERY_START])
        self.assertEqual(log[-1].split()[1], "GO")  # the last prompt was the one before the uncertain command; nothing was asked after it

    def test_a_failed_command_stops_without_asking_to_continue(self) -> None:
        log = []
        sim = SimSeestar()
        sim.modes[ARM_DEPLOY] = "pre_send"
        rig = Rig(sim)
        with redirect_stdout(io.StringIO()):
            report = rig.run(ask=scripted(log), step=True)
        self.assertEqual(sim.control_calls, [ARM_DEPLOY])
        self.assertFalse(any("Type NEXT" in p for p in log))
        self.assertEqual(rig.stage(report, "start_scenery")["status"], "NOT_RUN")

    def test_the_step_flag_needs_physical_mode_and_the_plain_mode_is_unchanged(self) -> None:
        rig = Rig()
        built = []
        code, out, _ = run_main(rig, ["--telemetry-max-age", "300", "--capability-max-age", "300", "--step-by-step"], built=built)
        self.assertEqual((code, built), (2, []))
        self.assertIn("--step-by-step needs --allow-physical-motion", out)
        log = []
        rig = Rig()
        rig.run(ask=answers(log=log))
        self.assertEqual(len(log), 3)  # DEPLOY + two ARM: no GO / NEXT prompts without the flag


class ColdSim(SimSeestar):
    """A cold-booted device: the app state carries no View and no SecondView."""

    def read_app_state(self, host):
        return parse_reply({"method": "iscope_get_app_state", "code": 0, "id": 1, "result": {"FocuserMove": {"state": "idle"}, "selected_cam": "View"}})


SINGLE_ARGS = ["--telemetry-max-age", "300", "--capability-max-age", "300", "--allow-physical-motion", "--command-deadline", "600",
               "--poll-interval", "30", "--permit-validity", "21600"]


class SingleCommandTests(unittest.TestCase):
    def one(self, command, sim, *, ask=None, **over):
        rig = Rig(sim)
        with redirect_stdout(io.StringIO()):
            return rig, rig.run(ask=ask or scripted([]), single=command, **over)

    def test_each_command_runs_alone_with_no_sequence_and_no_cleanup(self) -> None:
        cases = {
            "deploy": (SimSeestar(), ARM_DEPLOY, {"arm": "open", "cameras": "stopped"}),
            "start-scenery": (SimSeestar(arm_closed=False), SCENERY_START, {"arm": "open", "cameras": "ready"}),
            "stop-scenery": (SimSeestar(arm_closed=False, cameras="ready"), SCENERY_STOP, {"arm": "open", "cameras": "stopped"}),
            "park": (SimSeestar(arm_closed=False), ARM_PARK, {"arm": "closed", "cameras": "stopped"}),
        }
        for command, (sim, kind, expected) in cases.items():
            with self.subTest(command=command):
                rig, report = self.one(command, sim)
                self.assertEqual(rig.sim.control_calls, [kind])  # exactly one command; the cameras left running or the arm left open are NOT cleaned up
                self.assertEqual(report["overall"], "PASS")
                self.assertEqual((report["mode"], report["command"]), ("physical_single_command", kind))
                self.assertEqual(report["final_state"], expected)
                self.assertTrue(report["final_matches_expected_after_command"])
                self.assertFalse(report["unsafe_or_unknown_final_state"])
                self.assertEqual(report["cleanup"], "not_needed")
                self.assertEqual(report["outcomes"][0]["classification"], "succeeded")
                self.assertTrue(report["outcomes"][0]["last_evidence"])  # what the verifier saw
                self.assertEqual([s["status"] for s in report["stages"] if s["name"] not in ("identify", "baseline", SINGLE_COMMANDS_STAGE[command])],
                                 ["NOT_RUN"] * 4)

    def test_independent_invocations_each_take_a_fresh_baseline(self) -> None:
        sim = SimSeestar()
        _, first = self.one("deploy", sim)
        rig, second = self.one("start-scenery", sim)  # a new process: new executor, empty history
        self.assertEqual((first["overall"], second["overall"]), ("PASS", "PASS"))
        self.assertEqual(sim.control_calls, [ARM_DEPLOY, SCENERY_START])
        self.assertEqual(rig.stage(second, "baseline")["detail"], "established_by_recovery")

    def test_authorization_and_confirmation_are_required(self) -> None:
        for label, ask in (("no GO", scripted([], decline=("Type GO",))), ("no ARM", scripted([], decline=("Type ARM",))),
                           ("no DEPLOY", scripted([], decline=("Type DEPLOY",)))):
            with self.subTest(label):
                rig, report = self.one("deploy", SimSeestar(), ask=ask)
                self.assertEqual(rig.sim.control_calls, [])
                self.assertEqual(report["overall"], "FAIL")
                self.assertTrue(report["device_left_as_found"])
        log = []
        self.one("deploy", SimSeestar(), ask=scripted(log))
        self.assertEqual([p.split()[1] for p in log], ["DEPLOY", "GO", "ARM"])
        log = []
        self.one("stop-scenery", SimSeestar(arm_closed=False, cameras="ready"), ask=scripted(log))
        self.assertEqual([p.split()[1] for p in log], ["DEPLOY", "GO"])  # no arm movement, no ARM

    def test_the_gate_refuses_a_command_the_state_does_not_allow(self) -> None:
        for command, sim in (("deploy", SimSeestar(arm_closed=False)), ("park", SimSeestar(arm_closed=False, cameras="ready")),
                             ("start-scenery", SimSeestar()), ("park", SimSeestar())):
            with self.subTest(command=command):
                rig, report = self.one(command, sim)
                self.assertEqual(rig.sim.control_calls, [])  # nothing was sent
                self.assertEqual(rig.stage(report, SINGLE_COMMANDS_STAGE[command])["detail"], "command_safety_blocked")
                self.assertEqual(report["overall"], "FAIL")
                self.assertFalse(report["unsafe_or_unknown_final_state"])

    def test_a_cold_start_with_no_views_is_refused_and_never_read_as_stopped(self) -> None:
        for command in SINGLE_COMMANDS_STAGE:
            with self.subTest(command=command):
                sim = ColdSim()
                rig, report = self.one(command, sim)
                self.assertEqual(sim.control_calls, [])
                self.assertEqual(report["initial_state"], {"arm": "closed", "cameras": "unknown"})
                self.assertEqual(rig.stage(report, "baseline")["detail"], "baseline_refused_arm_closed_cameras_unknown")
                self.assertEqual(report["overall"], "FAIL")
                self.assertTrue(report["device_left_as_found"])
                self.assertEqual(report["observed_items"]["app.main.state"], {"state": "unavailable"})

    def test_unreadable_or_stale_telemetry_sends_nothing(self) -> None:
        sim = SimSeestar()
        sim.fail_app_reads = 10_000  # the app-state read keeps failing: no camera evidence at all
        rig, report = self.one("deploy", sim)
        self.assertEqual(sim.control_calls, [])
        self.assertNotEqual(report["overall"], "PASS")
        self.assertTrue(report["device_left_as_found"])

    def test_an_uncertain_result_is_reported_unsafe_and_nothing_else_is_sent(self) -> None:
        sim = SimSeestar()
        sim.modes[ARM_DEPLOY] = "ack_only"
        rig, report = self.one("deploy", sim)
        self.assertEqual(sim.control_calls, [ARM_DEPLOY])
        self.assertTrue(report["recovery_required"])
        self.assertTrue(report["unsafe_or_unknown_final_state"])
        self.assertEqual(report["overall"], "FAIL")
        self.assertEqual(len(rig.control.uncertainty_store.unresolved_devices()), 1)  # not cleared

    def test_a_failed_command_is_a_plain_failure_and_is_not_retried(self) -> None:
        sim = SimSeestar()
        sim.modes[ARM_DEPLOY] = "pre_send"
        rig, report = self.one("deploy", sim)
        self.assertEqual(sim.control_calls, [ARM_DEPLOY])
        self.assertEqual(report["final_state"], {"arm": "closed", "cameras": "stopped"})
        self.assertEqual(report["overall"], "FAIL")
        self.assertFalse(report["unsafe_or_unknown_final_state"])

    def test_the_command_line(self) -> None:
        for command, kind, sim in (("deploy", ARM_DEPLOY, SimSeestar()), ("park", ARM_PARK, SimSeestar(arm_closed=False))):
            rig = Rig(sim)
            code, out, report = run_main(rig, [*SINGLE_ARGS, "--command", command], ask=scripted([]))
            self.assertEqual((code, report["command"], sim.control_calls), (0, kind, [kind]), out)
        rig = Rig(ColdSim())
        code, out, report = run_main(rig, [*SINGLE_ARGS, "--command", "deploy"], ask=scripted([]))
        self.assertEqual((code, rig.sim.control_calls), (1, []))
        for extra, text in ((["--command", "deploy", "--step-by-step"], "cannot be combined"),):
            code, out, _ = run_main(Rig(), [*SINGLE_ARGS, *extra], ask=scripted([]))
            self.assertEqual(code, 2)
            self.assertIn(text, out)
        code, out, _ = run_main(Rig(), ["--telemetry-max-age", "300", "--capability-max-age", "300", "--command", "park"])
        self.assertEqual(code, 2)
        self.assertIn("--command needs --allow-physical-motion", out)
        argv = [a for a in SINGLE_ARGS if a not in ("--poll-interval", "30")]
        code, out, _ = run_main(Rig(), [*argv, "--command", "park"], ask=scripted([]))
        self.assertEqual(code, 2)
        self.assertIn("--poll-interval", out)  # timing is still required
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            CV.main(["--host", HOST, "--operator", "op", "--telemetry-max-age", "1", "--capability-max-age", "1", "--command", "sweep"])

    def test_uncertainty_exits_three(self) -> None:
        rig = Rig(SimSeestar())
        rig.sim.modes[ARM_DEPLOY] = "ack_only"
        code, out, _ = run_main(rig, [*SINGLE_ARGS, "--command", "deploy"], ask=scripted([]))
        self.assertEqual(code, 3)
        self.assertIn("CHECK THE TELESCOPE", out)

    def test_the_full_sequence_is_unchanged_by_the_new_option(self) -> None:
        rig = Rig()
        with redirect_stdout(io.StringIO()):
            report = rig.run(ask=answers())
        self.assertEqual(report["mode"], "physical")
        self.assertNotIn("command", report)
        self.assertEqual(rig.sim.control_calls, [ARM_DEPLOY, SCENERY_START, SCENERY_STOP, ARM_PARK])


SINGLE_COMMANDS_STAGE = {"deploy": "deploy_arm", "start-scenery": "start_scenery", "stop-scenery": "stop_scenery", "park": "park_arm"}


class MainTests(unittest.TestCase):
    PHYS = ["--telemetry-max-age", "300", "--capability-max-age", "300", "--allow-physical-motion", "--command-deadline", "600",
            "--poll-interval", "30", "--permit-validity", "21600", "--no-preview"]

    def test_a_full_physical_run_exits_zero_and_leaks_nothing(self) -> None:
        sim = SimSeestar()
        sim.serial = SERIAL
        rig = Rig(sim)
        code, out, report = run_main(rig, self.PHYS, ask=answers())
        self.assertEqual(code, 0)
        text = json.dumps(report) + out
        for secret in (HOST, SERIAL, "192.0.2.99", ".pem", "rtsp://"):
            self.assertNotIn(secret, text)

    def test_unsafe_exits_three(self) -> None:
        rig = Rig(SimSeestar())
        rig.sim.modes[SCENERY_START] = "ack_only"
        code, out, report = run_main(rig, self.PHYS, ask=answers())
        self.assertEqual(code, 3)
        self.assertIn("CHECK THE TELESCOPE", out)

    def test_a_missing_interactive_terminal_is_a_prerequisite_failure(self) -> None:
        rig = Rig()
        built = []
        with mock.patch.object(sys, "stdin", io.StringIO("")):
            code, out, report = run_main(rig, self.PHYS, ask=None, built=built)
        self.assertEqual(code, 2)
        self.assertIsNone(report)
        self.assertEqual(built, [])  # nothing was constructed, let alone contacted
        self.assertEqual(rig.sim.reads, 0)

    def test_every_timing_value_is_required_in_physical_mode(self) -> None:
        for flag in ("--command-deadline", "--poll-interval", "--permit-validity"):
            argv = list(self.PHYS)
            i = argv.index(flag)
            del argv[i:i + 2]
            rig = Rig()
            built = []
            code, out, report = run_main(rig, argv, ask=answers(), built=built)
            self.assertEqual((code, built, rig.sim.control_calls), (2, [], []), flag)
            self.assertIn(flag, out)
        argv = [a for a in self.PHYS if a != "--no-preview"]  # frames and preview time are required unless --no-preview
        code, out, _ = run_main(Rig(), argv, ask=answers(), built=[])
        self.assertEqual(code, 2)
        self.assertIn("--frames", out)

    def test_the_freshness_windows_are_required_even_read_only(self) -> None:
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            CV.main(["--host", HOST, "--operator", "op"])

    def test_an_existing_output_file_is_not_overwritten(self) -> None:
        rig = Rig()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "r.json"
            target.write_text("keep", encoding="utf-8")
            code, out, report = run_main(rig, ["--telemetry-max-age", "300", "--capability-max-age", "300"], out=target)
            self.assertEqual((code, target.read_text(encoding="utf-8")), (2, "keep"))


def run_main(rig, extra, *, ask=None, built=None, out=None):
    """Run ``CV.main`` against the rig. Returns (exit code, printed text, parsed report or None)."""
    with tempfile.TemporaryDirectory() as tmp:
        target = out or Path(tmp) / "report.json"

        def factory(freshness, permit):
            if built is not None:
                built.append(1)
            rig.permit = permit  # the tool's own permit is the authorizer
            sleep = rig.control._sleep
            return SeestarControl(config=make_config(), read_transport=rig.sim, control_transport=rig.sim, freshness=freshness,
                                  authorizer=permit, clock=rig.clock, id_generator=SequentialIdGenerator(), sleep=sleep)

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = CV.main(["--host", HOST, "--operator", "op", "--out", str(target), *extra], ask=ask, control_factory=factory,
                           preview_factory=lambda frames, seconds: GOOD_PREVIEW, clock=rig.clock)
        report = json.loads(target.read_text(encoding="utf-8")) if target.exists() and target.stat().st_size and code != 2 else None
        return code, buffer.getvalue(), report


class StaticTests(unittest.TestCase):
    source = SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source)

    def test_the_tool_never_touches_the_control_transport_or_the_provider(self) -> None:
        for forbidden in (".send_command(", ".submit_command(", ".poll_command(", ".cancel_command(", "SeestarCommandProvider", "ProviderRuntime("):
            self.assertNotIn(forbidden, self.source)
        constructed = [n for n in ast.walk(self.tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "SeestarControlTransport"]
        self.assertEqual(len(constructed), 1)  # built once and only handed to SeestarControl

    def test_the_tool_names_no_wire_method_and_supplies_no_parameter(self) -> None:
        for wire in ("scope_move_to_horizon", "scope_park", "iscope_start_view", "iscope_stop_view", "get_device_state", "socket", "sendall"):
            self.assertNotIn(wire, self.source)
        self.assertEqual(set(COMMANDS), {ARM_DEPLOY, ARM_PARK, SCENERY_START, SCENERY_STOP})
        for const in ("ARM_DEPLOY", "ARM_PARK", "SCENERY_START", "SCENERY_STOP"):
            self.assertIn(const, self.source)

    def test_only_the_existing_gpl_free_stack_is_imported_and_no_decoder_is_added(self) -> None:
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse({"seestarpy", "cv2", "numpy", "requests", "socket", "subprocess"} & imported)
        self.assertIn("seestar_preview_validate", self.source)
        self.assertIn("build_isolated_preview_manager", self.source)
        self.assertNotRegex(self.source, r"(?i)seestarpy")

    def test_no_timing_option_has_a_default(self) -> None:
        parsed = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_argument":
                name = node.args[0].value
                parsed[name] = {k.arg: k.value for k in node.keywords}
        for name in ("--telemetry-max-age", "--capability-max-age", "--command-deadline", "--poll-interval", "--permit-validity",
                     "--frames", "--preview-max-seconds"):
            default = parsed[name].get("default")
            self.assertTrue(default is None or (isinstance(default, ast.Constant) and default.value is None), name)
        self.assertIn("--allow-physical-motion", parsed)
        self.assertEqual(parsed["--allow-physical-motion"]["action"].value, "store_true")


if __name__ == "__main__":
    unittest.main()
