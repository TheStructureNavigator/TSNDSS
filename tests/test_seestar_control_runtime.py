"""DB-05 Slice 2: the Seestar adapter composed with the DB-04 Safe Command Runtime, against a stateful simulated device only."""

from __future__ import annotations

import copy
import inspect
import re
import threading
import unittest
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tsn_dss.engine.device_runtime import ConnectionState as C, ManualClock, SequentialIdGenerator, TelemetryItem, TelemetrySample
from tsn_dss.engine.device_runtime import TelemetrySource, ValueState
from tsn_dss.engine.device_runtime.command_effects import EffectVerdictKind as V
from tsn_dss.engine.device_runtime.command_executor import ClearanceNotAuthorized, CommandIntent, RecoveryNotEstablished
from tsn_dss.engine.device_runtime.errors import ProviderConnectionError
from tsn_dss.engine.device_runtime.models import CommandState as S
from tsn_dss.engine.device_runtime.provider import ProviderCommandReport, ProviderCommandStatus as PS
from tsn_dss.engine.device_runtime.safety_gates import EvidenceSnapshot, UncertaintyState
from tsn_dss.engine.device_runtime.uncertainty import UncertaintyStore
from tsn_dss.engine.seestar_control import (
    ARM_DEPLOY,
    ARM_PARK,
    COMMANDS,
    SCENERY_START,
    SCENERY_STOP,
    CommandDriver,
    ControlAttachError,
    ControlFreshness,
    ControlPostSendError,
    ControlPreSendError,
    SeestarCommandProvider,
    SeestarControl,
    SeestarControlTransport,
)
from tsn_dss.engine.seestar_control.recovery import observed_state_assessor, seestar_baseline_recovery
from tsn_dss.engine.seestar_control.verification import build_verifiers, submission_boundary
from tsn_dss.engine.seestar_provider.errors import SeestarUnreachable
from tsn_dss.engine.seestar_provider.protocol import RpcReply, parse_reply

try:
    from seestar_support import HOST, load_fixture, make_config
except ImportError:  # pragma: no cover
    from tests.seestar_support import HOST, load_fixture, make_config

PACKAGE = Path(__file__).resolve().parents[1] / "tsn_dss" / "engine" / "seestar_control"
FIVE_MINUTES = timedelta(minutes=5)
FRESH = ControlFreshness(telemetry_max_age=FIVE_MINUTES, capability_max_age=FIVE_MINUTES)  # test values, not module defaults
POLL = timedelta(seconds=30)
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


# --- a stateful simulated device: read transport and control transport in one ------------------------------------------------------


class SimSeestar:
    def __init__(self, *, arm_closed=True, cameras="stopped") -> None:
        self.arm_closed, self.moving, self.cameras = arm_closed, False, cameras  # cameras: stopped | starting | ready
        self.modes: dict[str, str] = {}  # kind -> normal | ack_only | effect_then_drop | drop | pre_send | error_reply
        self.control_calls: list[str] = []
        self.pending: deque = deque()
        self.fail_app_reads = 0
        self.lock = threading.Lock()

    # control side
    def send_command(self, host, kind_id):
        with self.lock:
            self.control_calls.append(kind_id)
            mode = self.modes.get(kind_id, "normal")
            if mode == "pre_send":
                raise ControlPreSendError("connect_failed")
            if mode == "drop":
                raise ControlPostSendError("connection_closed")
            if mode == "error_reply":
                return RpcReply("", 1, None, None)
            if mode != "ack_only":
                self.pending.extend(self._steps(kind_id))
            if mode == "effect_then_drop":
                raise ControlPostSendError("connection_closed")
            return RpcReply("", 0, 0, None)

    def _steps(self, kind_id):
        def moving(): self.moving = True
        def open_arm(): self.moving, self.arm_closed = False, False
        def close_arm(): self.moving, self.arm_closed = False, True
        def starting(): self.cameras = "starting"
        def ready(): self.cameras = "ready"
        def stopped(): self.cameras = "stopped"
        return {ARM_DEPLOY: [moving, open_arm], ARM_PARK: [moving, close_arm],
                SCENERY_START: [starting, ready], SCENERY_STOP: [stopped]}[kind_id]

    def tick(self) -> None:
        with self.lock:
            if self.pending:
                self.pending.popleft()()

    # read side
    def _mount(self):
        return {"move_type": "move" if self.moving else "none", "close": self.arm_closed, "tracking": False, "equ_mode": False}

    def read_device_state(self, host, keys):
        frame = copy.deepcopy(load_fixture("device_state_full_unfiltered.json"))
        frame["result"]["mount"] = self._mount()
        return parse_reply(frame)

    def read_app_state(self, host):
        if self.fail_app_reads:
            self.fail_app_reads -= 1
            raise SeestarUnreachable("connect_failed")
        frame = copy.deepcopy(load_fixture("app_state_scenery_ready.json" if self.cameras == "ready" else "app_state_idle.json"))
        if self.cameras == "starting":
            frame["result"]["View"]["state"] = "working"
        return parse_reply(frame)

    def test_connection(self, host):
        return parse_reply({"method": "test_connection", "result": "ok", "code": 0, "id": 1})

    def discover_via_udp(self):
        return ()


class Allow:
    def is_authorized(self, requested_by, kind, ref) -> bool:
        return True


class Deny:
    def is_authorized(self, requested_by, kind, ref) -> bool:
        return False


class Operators:
    def __init__(self, allowed=True) -> None:
        self.allowed = allowed

    def may_clear(self, operator_id, provider_id, device_ref) -> bool:
        return self.allowed


def build(sim=None, *, authorizer=None, clearance=None, store=None, baseline=True, attach=True):
    sim = sim or SimSeestar()
    clock = ManualClock()

    def sleep(seconds: float) -> None:
        clock.advance(timedelta(seconds=seconds))
        sim.tick()

    control = SeestarControl(
        config=make_config(), read_transport=sim, control_transport=sim, freshness=FRESH,
        authorizer=authorizer if authorizer is not None else Allow(), clearance_authorizer=clearance,
        uncertainty_store=store, clock=clock, id_generator=SequentialIdGenerator(), sleep=sleep,
    )
    if not attach:
        return sim, control, None, clock
    handle = control.attach()
    if baseline == "store":  # test setup for states the real baseline refuses (a moving arm): an operator-cleared entry
        from tsn_dss.engine.device_runtime.uncertainty import Resolution, ResolutionKind

        handle.executor.uncertainty_store.establish_baseline(
            handle.executor._runtime.provider_id, handle.connection.device.device_ref,
            Resolution(ResolutionKind.OPERATOR_CLEARANCE, T0, "test setup", resolved_by="setup"))
    elif baseline:
        handle.executor.establish_baseline_by_recovery(handle.connection)
    return sim, control, handle, clock


def run(handle, clock, kind, *, minutes=10, by="operator"):
    return handle.driver.execute(handle.connection, kind, by, deadline=clock() + timedelta(minutes=minutes), poll_interval=POLL)


# --- sample builders for the unit tests of the verifiers and the assessor ------------------------------------------------------------------


def sample(*, closed=False, move="none", cameras="ready", connection="conn-1", provider="seestar", at=T0, state=ValueState.KNOWN):
    pr = TelemetrySource.PROVIDER_REPORTED
    items = [TelemetryItem("mount.move_type", pr, state, move if state is ValueState.KNOWN else None),
             TelemetryItem("mount.arm_closed", pr, state, closed if state is ValueState.KNOWN else None)]
    ports = {"main": 4554, "wide": 4555}
    for cam in ("main", "wide"):
        if cameras == "ready":
            values = {"mode": "scenery", "stage": "RTSP", "state": "working", "rtsp_state": "working", "rtsp_port": ports[cam]}
        elif cameras == "stopped":
            values = {"mode": "none", "stage": "Sleep", "state": "idle", "rtsp_state": "idle", "rtsp_port": None}
        else:
            values = cameras[cam]
        for name, value in values.items():
            known = value is not None and state is ValueState.KNOWN
            items.append(TelemetryItem(f"app.{cam}.{name}", pr, ValueState.KNOWN if known else ValueState.UNKNOWN, value if known else None))
    return TelemetrySample(provider, connection, at, tuple(items), False)


class FakeRuntime:
    def __init__(self, result) -> None:
        self.result = result

    def read_telemetry(self, connection):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeRecord:
    def __init__(self, kind_id, boundary=T0, provider="seestar") -> None:
        from tsn_dss.engine.device_runtime.command_models import CommandKindPolicy

        from datetime import timedelta as _td
        from tsn_dss.engine.device_runtime.command_models import FreshnessRequirement

        self.policy = CommandKindPolicy(kind_id, state_changing=True, physical=True, idempotent=False, safety_sensitive=True,
                                        freshness=(FreshnessRequirement("x", _td(minutes=1)),))
        self.provider_id = provider
        entry = type("E", (), {"to_state": "submitted", "at": boundary})()
        self.history = (entry,) if boundary is not None else ()


class FakeConnection:
    connection_id = "conn-1"


def verdict(kind_id, telemetry, *, now=T0 + timedelta(seconds=30), boundary=T0, runtime=None):
    verifiers = build_verifiers(runtime or FakeRuntime(telemetry), FRESH, lambda: now)
    return verifiers[kind_id](FakeConnection(), FakeRecord(kind_id, boundary))


AFTER = T0 + timedelta(seconds=10)


# =====================================================================================================================


class CompositionTests(unittest.TestCase):
    def test_the_command_authorizer_is_mandatory_and_nothing_is_permitted_by_default(self) -> None:
        sim = SimSeestar()
        common = dict(config=make_config(), read_transport=sim, control_transport=sim, freshness=FRESH)
        with self.assertRaises(TypeError):
            SeestarControl(**common)  # type: ignore[call-arg]
        for bad in (None, object(), lambda *a: True):
            with self.assertRaises(ControlAttachError):
                SeestarControl(**common, authorizer=bad)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            SeestarControl(config=make_config(), read_transport=sim, control_transport=sim, authorizer=Allow())  # type: ignore[call-arg]
        signature = inspect.signature(SeestarControl.__init__).parameters
        self.assertIs(signature["clearance_authorizer"].default, None)  # no clearance unless injected
        self.assertIs(signature["authorizer"].default, inspect.Parameter.empty)
        self.assertIs(signature["freshness"].default, inspect.Parameter.empty)

    def test_a_denying_or_failing_authorizer_means_no_physical_call_at_all(self) -> None:
        class Boom:
            def is_authorized(self, *a):
                raise RuntimeError("down")

        for authorizer in (Deny(), Boom()):
            sim, control, handle, clock = build(authorizer=authorizer)
            for kind in COMMANDS:
                outcome = run(handle, clock, kind)
                self.assertEqual(outcome.classification, "safety_blocked", kind)
                self.assertFalse(outcome.submitted)
            self.assertEqual(sim.control_calls, [])
            self.assertEqual(handle.connection.state, C.READY)

    def test_capability_declarations_are_not_permission_freshness_safety_or_success(self) -> None:
        sim, control, handle, clock = build(authorizer=Deny())
        report = control.runtime.capability_report(handle.connection)
        declared = {e.name: e for e in report.entries if e.name in COMMANDS}
        self.assertEqual(len(declared), 4)
        self.assertTrue(all(e.confirmation.value == "implemented_untested" and e.available_now for e in declared.values()))
        self.assertEqual(run(handle, clock, ARM_DEPLOY).classification, "safety_blocked")  # declared, available, still not authorized
        sim2, control2, handle2, clock2 = build(baseline=False)
        blocked = run(handle2, clock2, ARM_DEPLOY)  # authorized, declared, but the device history is unknown
        self.assertEqual(blocked.classification, "safety_blocked")
        self.assertIn("uncertainty_unknown", blocked.last_evidence)
        sim3, control3, handle3, clock3 = build(SimSeestar(arm_closed=False))
        self.assertIn("state_unsafe", run(handle3, clock3, ARM_DEPLOY).last_evidence)  # fresh and known, unsafe value
        sim4, control4, handle4, clock4 = build()
        sim4.modes[ARM_DEPLOY] = "ack_only"
        self.assertFalse(run(handle4, clock4, ARM_DEPLOY).succeeded)  # accepted by the device, never verified

    def test_one_executor_per_connection_and_a_single_live_attachment(self) -> None:
        sim, control, handle, clock = build()
        with self.assertRaises(ControlAttachError) as ctx:
            control.attach()
        self.assertEqual(str(ctx.exception), "executor_already_attached")
        control.runtime.disconnect(handle.connection)
        second = control.attach()
        self.assertIsNot(second.executor, handle.executor)
        self.assertIsNot(second.connection, handle.connection)
        self.assertIs(control.uncertainty_store, control.uncertainty_store)

    def test_the_store_and_the_command_ids_are_shared_across_attachments(self) -> None:
        sim, control, handle, clock = build()
        first = run(handle, clock, ARM_DEPLOY)
        control.runtime.disconnect(handle.connection)
        second = control.attach()
        outcome = run(second, clock, SCENERY_START)
        self.assertNotEqual(first.command_id, outcome.command_id)
        self.assertEqual(len(set(sim.control_calls)), len(sim.control_calls))

    def test_exactly_one_device_is_required(self) -> None:
        sim, control, _, clock = build(attach=False)
        sim.read_device_state = lambda host, keys: (_ for _ in ()).throw(SeestarUnreachable("connect_failed"))  # type: ignore[method-assign]
        with self.assertRaises(Exception):
            control.attach()

    def test_the_command_provider_and_the_control_transport_are_not_reachable_from_the_public_surface(self) -> None:
        sim, control, handle, clock = build()
        for owner in (control, handle):
            for name in dir(owner):
                if name.startswith("_"):
                    continue
                value = getattr(owner, name)
                self.assertNotIsInstance(value, (SeestarCommandProvider, SeestarControlTransport), name)
        self.assertEqual({n for n in dir(handle) if not n.startswith("_")}, {"connection", "driver", "executor"})
        self.assertEqual({n for n in dir(control) if not n.startswith("_")}, {"attach", "runtime", "uncertainty_store"})
        runtime_names = {n.lower() for n in dir(control.runtime) if not n.startswith("_")}
        self.assertFalse([n for n in runtime_names if "submit" in n or "command" in n and "capab" not in n])


class SequenceTests(unittest.TestCase):
    def test_the_four_commands_run_in_order_and_are_each_verified(self) -> None:
        sim, control, handle, clock = build()
        outcomes = [run(handle, clock, kind) for kind in (ARM_DEPLOY, SCENERY_START, SCENERY_STOP, ARM_PARK)]
        for outcome in outcomes:
            self.assertTrue(outcome.succeeded, (outcome.kind_id, outcome.last_evidence))
            self.assertTrue(outcome.submitted)
            self.assertGreaterEqual(outcome.polls, 1)
            self.assertFalse(outcome.uncertainty_open)
            self.assertEqual(outcome.connection_state, "ready")
            self.assertIn("effect_verified", [h[1] for h in outcome.history])
            self.assertIn("provider-reported telemetry observed after submission", outcome.last_evidence)
        self.assertEqual(sim.control_calls, [ARM_DEPLOY, SCENERY_START, SCENERY_STOP, ARM_PARK])
        self.assertEqual((sim.arm_closed, sim.cameras, sim.moving), (True, "stopped", False))
        self.assertEqual(len({o.command_id for o in outcomes}), 4)

    def test_progress_is_visible_before_the_effect_is_verified(self) -> None:
        sim, control, handle, clock = build()
        outcome = run(handle, clock, ARM_DEPLOY)
        events = [h[1] for h in outcome.history]
        self.assertEqual(events[:4], ["request_valid", "gates_passed", "provider_acknowledged", "monitoring_started"])
        self.assertEqual(events[-1], "effect_verified")

    def test_the_outcome_carries_no_address_or_payload(self) -> None:
        sim, control, handle, clock = build()
        text = repr(run(handle, clock, ARM_DEPLOY))
        self.assertNotIn(HOST, text)
        self.assertNotIn("operator-supplied.pem", text)


class UnsafeStateTests(unittest.TestCase):
    def test_fresh_but_unsafe_state_is_blocked_at_admission_with_no_physical_call(self) -> None:
        cases = {
            ARM_DEPLOY: SimSeestar(arm_closed=False),
            SCENERY_START: SimSeestar(arm_closed=True),
            ARM_PARK: SimSeestar(arm_closed=True),
        }
        for kind, sim in cases.items():
            sim, control, handle, clock = build(sim)
            outcome = run(handle, clock, kind)
            self.assertEqual((outcome.classification, outcome.submitted), ("safety_blocked", False), kind)
            self.assertIn("state_unsafe", outcome.last_evidence)
            self.assertEqual(sim.control_calls, [], kind)
            self.assertEqual(handle.connection.state, C.READY)
        moving = SimSeestar(arm_closed=False)
        moving.moving = True
        sim, control, handle, clock = build(moving, baseline="store")
        for kind in (ARM_DEPLOY, SCENERY_START, ARM_PARK):
            self.assertIn("state_unsafe:mount.move_type", run(handle, clock, kind).last_evidence)
        running = SimSeestar(arm_closed=False, cameras="ready")
        sim, control, handle, clock = build(running, baseline="store")
        for kind in (SCENERY_START, ARM_PARK):
            self.assertIn("state_unsafe", run(handle, clock, kind).last_evidence)
        self.assertEqual(sim.control_calls, [])

    def test_the_state_is_checked_again_right_before_submission(self) -> None:
        sim, control, handle, clock = build()
        record = handle.executor.admit(CommandIntent(handle.connection, ARM_DEPLOY, "operator"))
        self.assertIs(record.state, S.VALIDATED)
        sim.arm_closed = False  # opened by other means after admission
        handle.executor.submit(record.command_id)
        self.assertIs(record.state, S.SAFETY_BLOCKED)
        self.assertEqual(sim.control_calls, [])
        self.assertEqual(handle.connection.state, C.READY)

    def test_stale_or_unreadable_evidence_blocks(self) -> None:
        sim, control, handle, clock = build()
        sim.fail_app_reads = 10  # app-state reads fail: cameras are retained as STALE, never accepted
        outcome = run(handle, clock, ARM_DEPLOY)  # deploy needs only the mount and the capability: still fine
        self.assertTrue(outcome.submitted)
        sim2, control2, handle2, clock2 = build()
        sim2.fail_app_reads = 10
        self.assertIn("state_stale", run(handle2, clock2, SCENERY_STOP).last_evidence)
        self.assertEqual(sim2.control_calls, [])


class AcknowledgementTests(unittest.TestCase):
    def test_an_accepted_command_without_a_physical_effect_is_never_success(self) -> None:
        sim, control, handle, clock = build()
        sim.modes[ARM_DEPLOY] = "ack_only"
        outcome = run(handle, clock, ARM_DEPLOY, minutes=10)
        self.assertEqual(outcome.classification, "unknown_result")
        self.assertTrue(outcome.uncertainty_open)
        self.assertEqual(outcome.connection_state, "degraded")
        self.assertGreater(outcome.polls, 5)
        self.assertEqual(sim.control_calls, [ARM_DEPLOY])  # one submission, no retry
        self.assertEqual([h[1] for h in outcome.history][-1], "deadline_effect_undetermined")

    def test_provider_reported_completion_alone_is_never_success(self) -> None:
        sim, control, handle, clock = build()
        sim.modes[ARM_DEPLOY] = "ack_only"
        control._provider.poll_command = lambda connection_id, command_id: ProviderCommandReport(PS.REPORTED_COMPLETE)  # type: ignore[method-assign]
        outcome = run(handle, clock, ARM_DEPLOY, minutes=5)
        self.assertNotEqual(outcome.classification, "succeeded")
        self.assertEqual(outcome.classification, "unknown_result")
        self.assertNotIn("effect_verified", [h[1] for h in outcome.history])


class FailureTests(unittest.TestCase):
    def test_pre_send_failure_is_failed_and_leaves_no_uncertainty(self) -> None:
        sim, control, handle, clock = build()
        sim.modes[ARM_DEPLOY] = "pre_send"
        outcome = run(handle, clock, ARM_DEPLOY)
        self.assertEqual((outcome.classification, outcome.uncertainty_open), ("failed", False))
        self.assertEqual(outcome.connection_state, "ready")

    def test_every_other_failure_preserves_uncertainty_and_is_never_retried(self) -> None:
        for mode in ("drop", "effect_then_drop", "error_reply", "ack_only"):
            sim, control, handle, clock = build()
            sim.modes[ARM_DEPLOY] = mode
            outcome = run(handle, clock, ARM_DEPLOY, minutes=5)
            self.assertEqual(outcome.classification, "unknown_result", mode)
            self.assertTrue(outcome.uncertainty_open, mode)
            self.assertEqual(sim.control_calls, [ARM_DEPLOY], mode)
            self.assertEqual(outcome.connection_state, "degraded", mode)
            again = run(handle, clock, ARM_DEPLOY)  # a new request is a new decision, and it is blocked
            self.assertEqual(again.classification, "safety_blocked", mode)
            self.assertEqual(sim.control_calls, [ARM_DEPLOY], mode)

    def test_the_driver_never_admits_or_submits_twice(self) -> None:
        sim, control, handle, clock = build()
        calls = {"admit": 0, "submit": 0}
        executor = handle.executor
        admit, submit = executor.admit, executor.submit
        executor.admit = lambda intent: (calls.__setitem__("admit", calls["admit"] + 1), admit(intent))[1]  # type: ignore[method-assign]
        executor.submit = lambda cid: (calls.__setitem__("submit", calls["submit"] + 1), submit(cid))[1]  # type: ignore[method-assign]
        sim.modes[ARM_DEPLOY] = "effect_then_drop"
        run(handle, clock, ARM_DEPLOY)
        self.assertEqual(calls, {"admit": 1, "submit": 1})

    def test_the_driver_requires_a_deadline_and_a_poll_interval(self) -> None:
        sim, control, handle, clock = build()
        parameters = inspect.signature(CommandDriver.execute).parameters
        self.assertIs(parameters["deadline"].default, inspect.Parameter.empty)
        self.assertIs(parameters["poll_interval"].default, inspect.Parameter.empty)
        with self.assertRaises(TypeError):
            handle.driver.execute(handle.connection, ARM_DEPLOY, "operator")  # type: ignore[call-arg]
        with self.assertRaises(ValueError):
            handle.driver.execute(handle.connection, ARM_DEPLOY, "op", deadline=datetime(2026, 1, 1), poll_interval=POLL)
        for bad in (timedelta(0), timedelta(seconds=-1), 30):
            with self.assertRaises(ValueError):
                handle.driver.execute(handle.connection, ARM_DEPLOY, "op", deadline=clock() + POLL, poll_interval=bad)  # type: ignore[arg-type]
        self.assertEqual(sim.control_calls, [])

    def test_an_already_elapsed_deadline_times_out_without_a_physical_call(self) -> None:
        sim, control, handle, clock = build()
        outcome = handle.driver.execute(handle.connection, ARM_DEPLOY, "op", deadline=clock() - POLL, poll_interval=POLL)
        self.assertEqual((outcome.classification, outcome.submitted), ("timed_out", False))
        self.assertEqual(sim.control_calls, [])


class RestartBaselineTests(unittest.TestCase):
    def test_a_restarted_process_knows_nothing_and_must_establish_the_device_state(self) -> None:
        sim = SimSeestar(arm_closed=False, cameras="ready")  # left running by the previous process
        sim, control, handle, clock = build(sim, baseline=False)
        blocked = run(handle, clock, SCENERY_STOP)
        self.assertEqual(blocked.classification, "safety_blocked")
        self.assertEqual(sim.control_calls, [])
        resolution = handle.executor.establish_baseline_by_recovery(handle.connection)
        self.assertIn("says nothing about whether any earlier command took effect", resolution.evidence)
        self.assertEqual({o for _, o in resolution.basis}, {"provider_reported"})
        stopped = run(handle, clock, SCENERY_STOP)
        self.assertTrue(stopped.succeeded)

    def test_a_baseline_authorizes_nothing(self) -> None:
        sim, control, handle, clock = build(authorizer=Deny())
        self.assertEqual(run(handle, clock, ARM_DEPLOY).classification, "safety_blocked")
        self.assertEqual(sim.control_calls, [])

    def test_ambiguous_moving_stale_or_inconsistent_states_are_refused(self) -> None:
        def refused(sim, fragment, setup=lambda s: None):
            sim, control, handle, clock = build(sim, baseline=False)
            setup(sim)
            with self.assertRaises(RecoveryNotEstablished) as ctx:
                handle.executor.establish_baseline_by_recovery(handle.connection)
            self.assertIn(fragment, str(ctx.exception))
            self.assertEqual(run(handle, clock, SCENERY_STOP).classification, "safety_blocked")

        moving = SimSeestar(arm_closed=False)
        moving.moving = True
        refused(moving, "state_unsafe")
        refused(SimSeestar(), "state_stale", lambda s: setattr(s, "fail_app_reads", 10))
        refused(SimSeestar(arm_closed=False, cameras="starting"), "recovery_not_established")
        refused(SimSeestar(arm_closed=True, cameras="ready"), "recovery_not_established")  # cameras running with the arm folded

    def test_a_new_process_does_not_inherit_the_old_stores_uncertainty(self) -> None:
        sim, control, handle, clock = build()
        sim.modes[ARM_DEPLOY] = "drop"
        self.assertTrue(run(handle, clock, ARM_DEPLOY).uncertainty_open)
        sim2, control2, handle2, clock2 = build(sim, baseline=False)  # same device, new process, empty store
        self.assertEqual(control2.uncertainty_store.unresolved_devices(), frozenset())
        self.assertEqual(run(handle2, clock2, ARM_DEPLOY).classification, "safety_blocked")  # still blocked: history unknown


class RecoveryTests(unittest.TestCase):
    def uncertain(self, kind=SCENERY_START, **kw):
        sim, control, handle, clock = build(SimSeestar(arm_closed=False) if kind == SCENERY_START else SimSeestar(), **kw)
        sim.modes[kind] = "effect_then_drop"  # the effect happened but the reply was lost
        outcome = run(handle, clock, kind)
        sim.tick(); sim.tick()  # the device settles
        self.assertEqual(outcome.classification, "unknown_result")
        return sim, control, handle, clock, outcome

    def test_recovery_resolves_with_the_current_state_and_never_rewrites_the_command(self) -> None:
        sim, control, handle, clock, outcome = self.uncertain()
        record = handle.executor.get(outcome.command_id)
        history = record.history
        entry = handle.executor.resolve_uncertainty_by_recovery(handle.connection, outcome.command_id)
        self.assertFalse(entry.unresolved)
        self.assertEqual(entry.resolution.kind.value, "recovery_evidence")
        self.assertFalse(entry.resolution.proves_physical_effect)
        self.assertIn("arm open and stationary", entry.resolution.evidence)
        self.assertIs(record.state, S.UNKNOWN_RESULT)
        self.assertEqual(record.history, history)
        self.assertEqual(sim.control_calls, [SCENERY_START])  # recovery sent nothing

    def test_recovery_works_even_when_the_state_differs_from_the_kinds_preconditions(self) -> None:
        sim, control, handle, clock, outcome = self.uncertain(ARM_DEPLOY)  # deploy requires a folded arm; it is now open
        self.assertFalse(sim.arm_closed)
        entry = handle.executor.resolve_uncertainty_by_recovery(handle.connection, outcome.command_id)
        self.assertFalse(entry.unresolved)

    def test_recovery_refuses_moving_stale_or_inconsistent_states(self) -> None:
        sim, control, handle, clock, outcome = self.uncertain()
        for label, mutate in {
            "moving": lambda: setattr(sim, "moving", True),
            "cameras_mixed": lambda: setattr(sim, "cameras", "starting"),
            "stale": lambda: setattr(sim, "fail_app_reads", 10),
        }.items():
            before = (sim.moving, sim.cameras, sim.fail_app_reads)
            mutate()
            with self.assertRaises(RecoveryNotEstablished, msg=label):
                handle.executor.resolve_uncertainty_by_recovery(handle.connection, outcome.command_id)
            sim.moving, sim.cameras, sim.fail_app_reads = before
        self.assertEqual(len(handle.executor.unresolved_devices()), 1)

    def test_resolving_does_not_authorize_new_physical_commands(self) -> None:
        sim, control, handle, clock, outcome = self.uncertain()
        handle.executor.resolve_uncertainty_by_recovery(handle.connection, outcome.command_id)
        control.runtime.refresh_evidence(handle.connection)
        calls = list(sim.control_calls)
        outcome2 = run(handle, clock, SCENERY_STOP)
        self.assertTrue(outcome2.succeeded)  # still needed the (allowing) authorizer, fresh evidence and a safe state
        self.assertEqual(sim.control_calls, calls + [SCENERY_STOP])

    def test_operator_clearance_is_separate_and_must_be_explicitly_authorized(self) -> None:
        sim, control, handle, clock, outcome = self.uncertain()
        ref = handle.connection.device.device_ref
        with self.assertRaises(ClearanceNotAuthorized):  # no clearance authorizer injected
            handle.executor.clear_uncertainty_by_operator(ref, outcome.command_id, "alice", "inspected")
        sim2, control2, handle2, clock2, outcome2 = self.uncertain(clearance=Operators(False))
        with self.assertRaises(ClearanceNotAuthorized):
            handle2.executor.clear_uncertainty_by_operator(handle2.connection.device.device_ref, outcome2.command_id, "mallory", "r")
        sim3, control3, handle3, clock3, outcome3 = self.uncertain(clearance=Operators(True))
        entry = handle3.executor.clear_uncertainty_by_operator(handle3.connection.device.device_ref, outcome3.command_id, "alice", "inspected")
        self.assertEqual((entry.resolution.kind.value, entry.resolution.resolved_by), ("operator_clearance", "alice"))
        self.assertFalse(entry.resolution.proves_physical_effect)
        self.assertIs(handle3.executor.get(outcome3.command_id).state, S.UNKNOWN_RESULT)

    def test_cleanup_is_blocked_until_the_uncertainty_is_resolved(self) -> None:
        sim, control, handle, clock = build()
        self.assertTrue(run(handle, clock, ARM_DEPLOY).succeeded)
        sim.modes[SCENERY_START] = "effect_then_drop"
        started = run(handle, clock, SCENERY_START)
        sim.tick(); sim.tick()
        self.assertEqual((started.classification, sim.cameras), ("unknown_result", "ready"))  # cameras are running
        # the degraded Connection is not ready: stop is refused on that ground alone
        self.assertEqual(run(handle, clock, SCENERY_STOP).classification, "safety_blocked")
        # a fresh Connection is ready, and the uncertainty (kept in the shared store) is what blocks cleanup
        control.runtime.disconnect(handle.connection)
        again = control.attach()
        self.assertEqual(again.connection.state, C.READY)
        calls = list(sim.control_calls)
        for kind in (SCENERY_STOP, ARM_PARK):
            blocked = run(again, clock, kind)
            self.assertEqual(blocked.classification, "safety_blocked", kind)
            self.assertIn("uncertainty_unresolved", blocked.last_evidence)
        self.assertEqual(sim.control_calls, calls)  # nothing was sent while the uncertainty was open
        # the executor that recorded the command resolves it, using the new Connection's fresh evidence
        handle.executor.resolve_uncertainty_by_recovery(again.connection, started.command_id)
        self.assertTrue(run(again, clock, SCENERY_STOP).succeeded)
        self.assertTrue(run(again, clock, ARM_PARK).succeeded)
        self.assertEqual((sim.arm_closed, sim.cameras), (True, "stopped"))

    def test_cleanup_after_an_operator_clearance_still_needs_fresh_safe_evidence(self) -> None:
        sim, control, handle, clock, outcome = self.uncertain(clearance=Operators(True))
        handle.executor.clear_uncertainty_by_operator(handle.connection.device.device_ref, outcome.command_id, "alice", "looked")
        control.runtime.refresh_evidence(handle.connection)
        sim.moving = True  # the arm is moving: clearance does not make that safe
        self.assertEqual(run(handle, clock, ARM_PARK).classification, "safety_blocked")
        sim.moving = False
        self.assertTrue(run(handle, clock, SCENERY_STOP).succeeded)


class ConcurrencyTests(unittest.TestCase):
    def test_simultaneous_requests_on_one_connection_admit_exactly_one_and_the_device_is_reached_once(self) -> None:
        sim, control, handle, clock = build()
        count = 6
        barrier = threading.Barrier(count)
        records = []

        def worker(i):
            barrier.wait(10)
            records.append(handle.executor.admit(CommandIntent(handle.connection, ARM_DEPLOY, f"op{i}")))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(sorted(r.state.value for r in records), ["rejected"] * (count - 1) + ["validated"])
        winner = next(r for r in records if r.state is S.VALIDATED)
        handle.executor.submit(winner.command_id)
        self.assertEqual(sim.control_calls, [ARM_DEPLOY])

    def test_simultaneous_submission_of_one_admitted_command_sends_once(self) -> None:
        sim, control, handle, clock = build()
        record = handle.executor.admit(CommandIntent(handle.connection, ARM_DEPLOY, "operator"))
        barrier = threading.Barrier(5)
        results = []

        def worker():
            barrier.wait(10)
            try:
                handle.executor.submit(record.command_id)
                results.append("ok")
            except Exception:
                results.append("refused")

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(sorted(results), ["ok"] + ["refused"] * 4)
        self.assertEqual(sim.control_calls, [ARM_DEPLOY])


class VerifierUnitTests(unittest.TestCase):
    def check(self, kind, telemetry, expected, **kw):
        result = verdict(kind, telemetry, **kw)
        self.assertIs(result.kind, expected)
        return result

    def test_each_target_state_is_verified_from_fresh_post_boundary_telemetry(self) -> None:
        self.check(ARM_DEPLOY, sample(closed=False, at=AFTER, cameras="stopped"), V.VERIFIED)
        self.check(ARM_PARK, sample(closed=True, at=AFTER, cameras="stopped"), V.VERIFIED)
        self.check(SCENERY_START, sample(closed=False, at=AFTER, cameras="ready"), V.VERIFIED)
        self.check(SCENERY_STOP, sample(closed=False, at=AFTER, cameras="stopped"), V.VERIFIED)

    def test_every_other_state_is_pending_and_never_failed(self) -> None:
        ok = {"closed": False, "at": AFTER}
        for kind, bad in {
            ARM_DEPLOY: [sample(closed=True, at=AFTER), sample(move="move", **ok), sample(state=ValueState.UNKNOWN, at=AFTER)],
            ARM_PARK: [sample(closed=False, at=AFTER), sample(closed=True, move="move", at=AFTER)],
            SCENERY_START: [sample(closed=True, cameras="ready", at=AFTER), sample(cameras="stopped", **ok),
                            sample(move="move", cameras="ready", **ok)],
            SCENERY_STOP: [sample(cameras="ready", **ok)],
        }.items():
            for telemetry in bad:
                self.assertIs(verdict(kind, telemetry).kind, V.PENDING, kind)

    def test_scenery_start_needs_both_cameras_on_their_own_ports(self) -> None:
        for broken in ({"main": {"mode": "scenery", "stage": "RTSP", "state": "working", "rtsp_state": "working", "rtsp_port": 4555},
                        "wide": {"mode": "scenery", "stage": "RTSP", "state": "working", "rtsp_state": "working", "rtsp_port": 4555}},
                       {"main": {"mode": "scenery", "stage": "RTSP", "state": "working", "rtsp_state": "working", "rtsp_port": 4554},
                        "wide": {"mode": "scenery", "stage": "Sleep", "state": "working", "rtsp_state": "working", "rtsp_port": 4555}},
                       {"main": {"mode": "scenery", "stage": "RTSP", "state": "working", "rtsp_state": "idle", "rtsp_port": 4554},
                        "wide": {"mode": "scenery", "stage": "RTSP", "state": "working", "rtsp_state": "working", "rtsp_port": 4555}}):
            self.assertIs(verdict(SCENERY_START, sample(closed=False, cameras=broken, at=AFTER)).kind, V.PENDING)

    def test_scenery_stop_needs_all_four_camera_items_stopped(self) -> None:
        partial = {"main": {"mode": "none", "stage": "Sleep", "state": "idle", "rtsp_state": "idle", "rtsp_port": None},
                   "wide": {"mode": "none", "stage": "Sleep", "state": "idle", "rtsp_state": "working", "rtsp_port": 4555}}
        self.assertIs(verdict(SCENERY_STOP, sample(cameras=partial, at=AFTER)).kind, V.PENDING)

    def test_evidence_must_be_after_the_submission_boundary_fresh_and_about_this_connection(self) -> None:
        good = sample(closed=False, at=AFTER)
        self.assertIs(verdict(ARM_DEPLOY, good).kind, V.VERIFIED)
        for label, (telemetry, kw) in {
            "equal_to_boundary": (sample(closed=False, at=T0), {}),
            "before_boundary": (sample(closed=False, at=T0 - timedelta(seconds=5)), {}),
            "too_old": (good, {"now": AFTER + FIVE_MINUTES + timedelta(seconds=1)}),
            "future": (sample(closed=False, at=AFTER), {"now": AFTER - timedelta(seconds=1)}),
            "other_connection": (sample(closed=False, at=AFTER, connection="conn-9"), {}),
            "other_provider": (sample(closed=False, at=AFTER, provider="other"), {}),
            "never_submitted": (good, {"boundary": None}),
        }.items():
            self.assertIs(verdict(ARM_DEPLOY, telemetry, **kw).kind, V.PENDING, label)
        self.assertIs(verdict(ARM_DEPLOY, good, now=AFTER + FIVE_MINUTES).kind, V.VERIFIED)  # the window is inclusive

    def test_an_unreadable_device_or_a_mismatched_kind_is_pending(self) -> None:
        self.assertIs(verdict(ARM_DEPLOY, None, runtime=FakeRuntime(ProviderConnectionError("x"))).kind, V.PENDING)
        verifiers = build_verifiers(FakeRuntime(sample(closed=False, at=AFTER)), FRESH, lambda: AFTER)
        self.assertIs(verifiers[ARM_PARK](FakeConnection(), FakeRecord(ARM_DEPLOY)).kind, V.PENDING)

    def test_the_window_is_the_integrators_and_there_is_one_verifier_per_command(self) -> None:
        self.assertEqual(set(build_verifiers(FakeRuntime(None), FRESH, lambda: T0)), set(COMMANDS))
        short = ControlFreshness(timedelta(seconds=5), timedelta(seconds=5))
        verifiers = build_verifiers(FakeRuntime(sample(closed=False, at=AFTER)), short, lambda: AFTER + timedelta(seconds=6))
        self.assertIs(verifiers[ARM_DEPLOY](FakeConnection(), FakeRecord(ARM_DEPLOY)).kind, V.PENDING)

    def test_submission_boundary_is_the_last_submitted_entry(self) -> None:
        self.assertEqual(submission_boundary(FakeRecord(ARM_DEPLOY, boundary=T0)), T0)
        self.assertIsNone(submission_boundary(FakeRecord(ARM_DEPLOY, boundary=None)))

    def test_the_evidence_text_names_the_state_and_its_provider_reported_nature(self) -> None:
        text = verdict(ARM_PARK, sample(closed=True, at=AFTER)).evidence
        self.assertIn("provider-reported", text)
        self.assertIn("arm closed and stationary", text)


class AssessorUnitTests(unittest.TestCase):
    def assess(self, telemetry):
        return observed_state_assessor(FakeConnection(), None, EvidenceSnapshot(telemetry=telemetry), T0)

    def test_only_a_known_settled_consistent_state_resolves(self) -> None:
        for telemetry in (sample(closed=False, cameras="ready"), sample(closed=False, cameras="stopped"), sample(closed=True, cameras="stopped")):
            result = self.assess(telemetry)
            self.assertTrue(result.resolved)
            self.assertEqual({o for _, o in result.basis}, {"provider_reported"})
            self.assertIn("says nothing about whether any earlier command took effect", result.evidence)
        starting = {c: {"mode": "scenery", "stage": "RTSP", "state": "starting", "rtsp_state": "idle", "rtsp_port": None} for c in ("main", "wide")}
        three_of_four = {"main": {"mode": "none", "stage": "Sleep", "state": "idle", "rtsp_state": "idle", "rtsp_port": None},
                         "wide": {"mode": "none", "stage": "Sleep", "state": "idle", "rtsp_state": "starting", "rtsp_port": None}}
        mixed = {"main": {"mode": "scenery", "stage": "RTSP", "state": "working", "rtsp_state": "working", "rtsp_port": 4554},
                 "wide": {"mode": "none", "stage": "Sleep", "state": "idle", "rtsp_state": "idle", "rtsp_port": None}}
        for label, telemetry in {
            "moving": sample(move="move"), "unknown": sample(state=ValueState.UNKNOWN), "ready_but_folded": sample(closed=True, cameras="ready"),
            "unfamiliar_words": sample(cameras=starting), "three_of_four": sample(cameras=three_of_four), "one_ready": sample(cameras=mixed),
        }.items():
            self.assertFalse(self.assess(telemetry).resolved, label)
        self.assertFalse(observed_state_assessor(FakeConnection(), None, EvidenceSnapshot(), T0).resolved)
        # only the arm position unknown (the movement state is known and settled) is still refused
        base = sample(closed=False, cameras="stopped")
        items = tuple(TelemetryItem(i.name, i.source, ValueState.UNKNOWN, None) if i.name == "mount.arm_closed" else i for i in base.items)
        unknown_arm = TelemetrySample(base.provider_id, base.connection_id, base.host_observed_at, items, False)
        self.assertFalse(self.assess(unknown_arm).resolved)

    def test_the_baseline_requirements_come_from_the_integrators_window(self) -> None:
        window = ControlFreshness(timedelta(seconds=9), timedelta(seconds=1))
        baseline = seestar_baseline_recovery(window)
        self.assertTrue(baseline.freshness)
        self.assertTrue(all(r.max_age == timedelta(seconds=9) for r in baseline.freshness))
        self.assertIs(baseline.assessor, observed_state_assessor)
        by_item = {r.item: r.allowed_values for r in baseline.freshness}
        self.assertEqual(by_item["mount.move_type"], ("none",))
        self.assertEqual(set(by_item["mount.arm_closed"]), {True, False})
        self.assertEqual(set(by_item["app.main.state"]), {"working", "cancel", "complete", "idle"})


class BoundaryTests(unittest.TestCase):
    def test_no_default_deadline_window_or_timeout_in_the_new_modules(self) -> None:
        for name in ("verification", "recovery", "driver", "composition"):
            text = (PACKAGE / f"{name}.py").read_text(encoding="utf-8")
            self.assertEqual(re.findall(r"timedelta\(\s*[^0)]", text), [], name)
            self.assertNotRegex(text, r"seconds\s*=\s*\d|minutes\s*=\s*\d|timeout\s*=\s*\d", name)

    def test_only_the_composition_builds_an_executor_and_nothing_bypasses_it(self) -> None:
        users = {p.name for p in PACKAGE.glob("*.py") if re.search(r"CommandExecutor\(", p.read_text(encoding="utf-8"))}
        self.assertEqual(users, {"composition.py"})
        for name in ("verification", "recovery", "driver"):
            text = (PACKAGE / f"{name}.py").read_text(encoding="utf-8")
            self.assertNotRegex(text, r"submit_command|poll_command|cancel_command|send_command|SeestarControlTransport|SeestarCommandProvider")

    def test_the_driver_only_uses_the_executors_public_lifecycle(self) -> None:
        text = (PACKAGE / "driver.py").read_text(encoding="utf-8")
        used = set(re.findall(r"executor\.(\w+)\(", text))
        self.assertEqual(used, {"admit", "submit", "poll", "enforce_deadline", "unresolved_devices"})
        self.assertNotRegex(text, r"resolve_uncertainty|clear_uncertainty|establish_baseline|cancel\(")

    def test_nothing_is_wired_to_a_hardware_path_or_the_wider_application(self) -> None:
        root = PACKAGE.parents[2]
        for path in (root / "tsn_dss").rglob("*.py"):
            if PACKAGE in path.parents:
                continue
            self.assertNotIn("seestar_control", path.read_text(encoding="utf-8"), path.name)
        for path in (root / "tools").glob("*.py"):
            self.assertNotIn("seestar_control", path.read_text(encoding="utf-8"), path.name)


if __name__ == "__main__":
    unittest.main()
