"""DB-04 S2: Command admission, authorization, per-Connection exclusivity and the controlled ``busy`` lifecycle."""

from __future__ import annotations

import re
import unittest
from datetime import timedelta
from pathlib import Path

from tsn_dss.engine.device_runtime import ManualClock, ProviderRuntime, SequentialIdGenerator
from tsn_dss.engine.device_runtime.command_executor import CommandExecutor, CommandIntent
from tsn_dss.engine.device_runtime.command_models import CommandKindPolicy, CommandKindRegistry, FreshnessRequirement
from tsn_dss.engine.device_runtime.errors import InvalidTransition
from tsn_dss.engine.device_runtime.lifecycle import BUSY_CONTROL, BusyEvent, ConnectionEvent
from tsn_dss.engine.device_runtime.models import (
    CommandState as S,
    ConnectionState as C,
    TelemetryItem,
    TelemetrySample,
    TelemetrySource,
    ValueState,
)
from tsn_dss.engine.device_runtime.safety_gates import EvidenceSnapshot, UncertaintyState

try:
    from device_runtime_support import make_provider, two_devices
except ImportError:  # pragma: no cover
    from tests.device_runtime_support import make_provider, two_devices

PACKAGE = Path(__file__).resolve().parents[1] / "tsn_dss" / "engine" / "device_runtime"

CHANGE = CommandKindPolicy("change", state_changing=True, physical=False, idempotent=False, safety_sensitive=False)
IDEM = CommandKindPolicy("idem", state_changing=True, physical=False, idempotent=True, safety_sensitive=False)
READ_LIKE = CommandKindPolicy("query", state_changing=False, physical=False, idempotent=True, safety_sensitive=False)
PHYS = CommandKindPolicy(
    "move", state_changing=True, physical=True, idempotent=False, safety_sensitive=True,
    freshness=(FreshnessRequirement("pose", timedelta(seconds=60)),),
)


class Allow:
    def is_authorized(self, requested_by, kind, ref) -> bool:
        return requested_by != "intruder"


class Deny:
    def is_authorized(self, requested_by, kind, ref) -> bool:
        return False


class Boom:
    def is_authorized(self, requested_by, kind, ref):
        raise RuntimeError("authorizer down")


class Truthy:
    def is_authorized(self, requested_by, kind, ref):
        return "yes"


def kinds() -> CommandKindRegistry:
    registry = CommandKindRegistry()
    for policy in (CHANGE, IDEM, READ_LIKE, PHYS):
        registry.register(policy)
    return registry


class Clear:
    def state_for(self, provider_id, device_ref):
        return UncertaintyState.RESOLVED_BY_RECOVERY_EVIDENCE


def establish_baselines(executor, runtime, devices) -> None:
    """Test setup: an operator-cleared baseline for each device, so that gate tests can reach the gate itself."""
    from datetime import datetime, timezone

    from tsn_dss.engine.device_runtime.uncertainty import Resolution, ResolutionKind

    for device in devices:
        executor.uncertainty_store.establish_baseline(
            runtime.provider_id, device.device_ref,
            Resolution(ResolutionKind.OPERATOR_CLEARANCE, datetime(2026, 1, 1, tzinfo=timezone.utc), "test setup", resolved_by="setup"),
        )


class Fixture:
    def __init__(self, authorizer=None, devices=None, evidence_source=None, uncertainty=None, baseline=True) -> None:
        self.provider = make_provider(**({"devices": devices} if devices else {}))
        self.runtime = ProviderRuntime(self.provider, clock=ManualClock(), id_generator=SequentialIdGenerator())
        self.found = self.runtime.discover().devices
        self.clock = ManualClock()
        self.executor = CommandExecutor(
            self.runtime, kinds(), authorizer or Allow(), clock=self.clock, id_generator=SequentialIdGenerator(),
            evidence_source=evidence_source or self.fresh_pose, uncertainty=uncertainty or Clear(),
        )
        if baseline:
            establish_baselines(self.executor, self.runtime, self.found)

    def fresh_pose(self, connection) -> EvidenceSnapshot:
        sample = TelemetrySample(
            provider_id=self.runtime.provider_id, connection_id=connection.connection_id,
            host_observed_at=self.clock(), simulated=True,
            items=(TelemetryItem("pose", TelemetrySource.PROVIDER_REPORTED, ValueState.KNOWN, "x"),),
        )
        return EvidenceSnapshot(telemetry=sample)

    def connection(self, index: int = 0, ready: bool = True):
        connection = self.runtime.connect(self.runtime.open_connection(self.found[index]))
        if ready:
            self.runtime.refresh_evidence(connection)
            assert connection.state is C.READY
        return connection

    def admit(self, connection, kind="change", by="operator", **kw):
        return self.executor.admit(CommandIntent(connection, kind, by, **kw))


class AdmissionTests(unittest.TestCase):
    def test_ids_are_minted_and_bound_to_provider_and_connection(self) -> None:
        """REQ-020, REQ-044."""
        fx = Fixture()
        connection = fx.connection()
        first = fx.admit(connection, "query")
        second = fx.admit(connection, "query")
        self.assertEqual((first.command_id, second.command_id), ("cmd-0001", "cmd-0002"))
        self.assertEqual(first.provider_id, fx.runtime.provider_id)
        self.assertEqual(first.connection_id, connection.connection_id)
        self.assertIs(fx.executor.get(first.command_id), first)
        self.assertEqual([r.command_id for r in fx.executor.commands], ["cmd-0001", "cmd-0002"])

    def test_the_caller_cannot_supply_a_command_id(self) -> None:
        import inspect

        self.assertNotIn("command_id", inspect.signature(CommandIntent).parameters)

    def test_passive_reads_create_no_command(self) -> None:
        """REQ-022."""
        fx = Fixture()
        connection = fx.connection()
        fx.runtime.read_telemetry(connection)
        fx.runtime.describe_preview(connection)
        fx.runtime.capability_report(connection)
        fx.runtime.refresh_evidence(connection)
        fx.runtime.discover()
        self.assertEqual(fx.executor.commands, ())
        self.assertIsNone(fx.executor.active_command(connection))
        self.assertIs(connection.state, C.READY)

    def test_invalid_requests_are_rejected_before_any_provider_call(self) -> None:
        """REQ-023: a rejection is a terminal alternative outcome, never a step toward execution."""
        fx = Fixture()
        connection = fx.connection()
        other = Fixture().connection()
        disconnected = fx.connection(0, ready=False)
        fx.runtime.disconnect(disconnected)
        calls = list(fx.provider.calls)
        cases = {
            "unknown_kind": fx.admit(connection, "nope"),
            "malformed_request": fx.admit(connection, "change", by=" "),
            "idempotency_key_on_non_idempotent_kind": fx.admit(connection, "change", idempotency_key="k"),
            "connection_not_owned": fx.admit(other),
            "connection_not_active:disconnected": fx.admit(disconnected),
        }
        for reason, record in cases.items():
            self.assertIs(record.state, S.REJECTED, reason)
            self.assertEqual(record.history[-1].evidence, reason)
        self.assertEqual(fx.provider.calls, calls)
        self.assertIs(connection.state, C.READY)
        self.assertIsNone(fx.executor.active_command(connection))

    def test_idempotency_key_is_allowed_on_an_idempotent_kind(self) -> None:
        fx = Fixture()
        self.assertIs(fx.admit(fx.connection(), "idem", idempotency_key="k").state, S.VALIDATED)

    def test_unauthorized_requests_are_safety_blocked_before_any_provider_call(self) -> None:
        """REQ-021, fail closed."""
        for authorizer in (Deny(), Boom(), Truthy()):
            fx = Fixture(authorizer)
            connection = fx.connection()
            calls = list(fx.provider.calls)
            record = fx.admit(connection)
            self.assertIs(record.state, S.SAFETY_BLOCKED, type(authorizer).__name__)
            self.assertEqual([h.to_state for h in record.history], ["validated", "safety_blocked"])
            self.assertEqual(fx.provider.calls, calls)
            self.assertIs(connection.state, C.READY)
            self.assertIsNone(fx.executor.active_command(connection))

    def test_caller_specific_denial(self) -> None:
        fx = Fixture()
        self.assertIs(fx.admit(fx.connection(), by="intruder").state, S.SAFETY_BLOCKED)

    def test_an_explicit_authorizer_is_mandatory(self) -> None:
        fx = Fixture()
        for bad in (None, object(), lambda *a: True):
            with self.assertRaises(Exception):
                CommandExecutor(fx.runtime, kinds(), bad)  # type: ignore[arg-type]

    def test_state_changing_command_on_a_connection_that_is_not_ready_is_safety_blocked(self) -> None:
        fx = Fixture()
        connection = fx.connection(ready=False)
        self.assertIs(connection.state, C.CONNECTED)
        record = fx.admit(connection)
        self.assertIs(record.state, S.SAFETY_BLOCKED)
        self.assertIn("connection_not_ready:connected", record.history[-1].evidence)
        self.assertIs(connection.state, C.CONNECTED)
        fx.runtime.report_transport_loss(connection, C.DEGRADED)
        self.assertIs(fx.admit(connection).state, S.SAFETY_BLOCKED)

    def test_elapsed_deadline_times_out_without_submission(self) -> None:
        fx = Fixture()
        connection = fx.connection()
        calls = list(fx.provider.calls)
        past = fx.clock() - timedelta(seconds=1)
        record = fx.admit(connection, deadline=past)
        self.assertIs(record.state, S.TIMED_OUT)
        self.assertEqual(fx.provider.calls, calls)
        self.assertIs(connection.state, C.READY)

    def test_non_state_changing_commands_neither_take_the_slot_nor_enter_busy(self) -> None:
        fx = Fixture()
        connection = fx.connection()
        a, b = fx.admit(connection, "query"), fx.admit(connection, "query")
        self.assertEqual((a.state, b.state), (S.VALIDATED, S.VALIDATED))
        self.assertIs(connection.state, C.READY)
        self.assertIsNone(fx.executor.active_command(connection))
        change = fx.admit(connection)
        self.assertIs(change.state, S.VALIDATED)
        self.assertIs(fx.admit(connection, "query").state, S.VALIDATED)

    def test_physical_kind_is_admitted_like_any_state_changing_kind(self) -> None:
        fx = Fixture()
        connection = fx.connection()
        self.assertIs(fx.admit(connection, "move").state, S.VALIDATED)
        self.assertIs(connection.state, C.BUSY)

    def test_admission_is_deterministic(self) -> None:
        def run():
            fx = Fixture()
            c = fx.connection()
            return [(r.command_id, r.state, [h.evidence for h in r.history]) for r in
                    (fx.admit(c), fx.admit(c), fx.admit(c, "nope"))]

        self.assertEqual(run(), run())


class ExclusivityTests(unittest.TestCase):
    def test_first_state_changing_command_takes_the_slot_and_the_connection_is_busy(self) -> None:
        """REQ-039."""
        fx = Fixture()
        connection = fx.connection()
        first = fx.admit(connection)
        self.assertIs(first.state, S.VALIDATED)
        self.assertIs(connection.state, C.BUSY)
        self.assertIs(fx.executor.active_command(connection), first)
        record = connection.history[-1]
        self.assertEqual((record.from_state, record.event, record.to_state, record.evidence),
                         ("ready", "busy_entered", "busy", first.command_id))

    def test_conflicting_second_command_is_rejected_before_any_provider_call(self) -> None:
        """REQ-040."""
        fx = Fixture()
        connection = fx.connection()
        first = fx.admit(connection)
        calls = list(fx.provider.calls)
        for kind in ("change", "move", "idem"):
            second = fx.admit(connection, kind)
            self.assertIs(second.state, S.REJECTED, kind)
            self.assertEqual(second.history[-1].evidence, f"active_command:{first.command_id}")
        self.assertEqual(fx.provider.calls, calls)
        self.assertIs(first.state, S.VALIDATED)
        self.assertIs(fx.executor.active_command(connection), first)
        self.assertIs(connection.state, C.BUSY)

    def test_conflict_is_judged_before_authorization(self) -> None:
        fx = Fixture()
        connection = fx.connection()
        fx.admit(connection)
        self.assertIs(fx.admit(connection, by="intruder").state, S.REJECTED)

    def test_a_busy_connection_without_an_attributed_command_is_still_a_conflict(self) -> None:
        fx = Fixture()
        connection = fx.connection()
        connection.apply_busy(BusyEvent.BUSY_ENTERED, fx.clock(), "external", BUSY_CONTROL)
        record = fx.admit(connection)
        self.assertIs(record.state, S.REJECTED)
        self.assertEqual(record.history[-1].evidence, "connection_busy_unattributed")

    def test_exclusivity_is_per_connection(self) -> None:
        fx = Fixture(devices=two_devices())
        one, two = fx.connection(0), fx.connection(1)
        a = fx.admit(one)
        b = fx.admit(two)
        self.assertEqual((a.state, b.state), (S.VALIDATED, S.VALIDATED))
        self.assertEqual((one.state, two.state), (C.BUSY, C.BUSY))
        self.assertIs(fx.executor.active_command(two), b)

    def test_a_rejected_or_blocked_command_never_holds_the_slot(self) -> None:
        fx = Fixture()
        connection = fx.connection()
        fx.admit(connection, "nope")
        fx.admit(connection, by="intruder")
        self.assertIsNone(fx.executor.active_command(connection))
        self.assertIs(fx.admit(connection).state, S.VALIDATED)


class BusyLifecycleTests(unittest.TestCase):
    def test_busy_is_left_when_the_active_command_reaches_a_terminal_outcome(self) -> None:
        fx = Fixture()
        connection = fx.connection()
        record = fx.admit(connection, deadline=fx.clock() + timedelta(seconds=30))
        self.assertIs(fx.executor.check_deadline(record.command_id).state, S.VALIDATED)
        self.assertIs(connection.state, C.BUSY)
        fx.clock.advance(timedelta(seconds=60))
        calls = list(fx.provider.calls)
        self.assertIs(fx.executor.check_deadline(record.command_id).state, S.TIMED_OUT)
        self.assertEqual(fx.provider.calls, calls)
        self.assertIs(connection.state, C.READY)
        self.assertIsNone(fx.executor.active_command(connection))
        last = connection.history[-1]
        self.assertEqual((last.event, last.to_state, last.evidence), ("busy_left_ready", "ready", "timed_out"))
        self.assertIs(fx.admit(connection).state, S.VALIDATED)

    def test_check_deadline_is_a_no_op_for_a_terminal_or_deadline_free_command(self) -> None:
        fx = Fixture()
        connection = fx.connection()
        free = fx.admit(connection)
        self.assertIs(fx.executor.check_deadline(free.command_id).state, S.VALIDATED)
        self.assertIs(connection.state, C.BUSY)
        rejected = fx.admit(connection)
        history = rejected.history
        self.assertIs(fx.executor.check_deadline(rejected.command_id).state, S.REJECTED)
        self.assertEqual(rejected.history, history)
        self.assertIs(connection.state, C.BUSY)

    def test_transport_loss_before_submission_blocks_the_command_and_frees_the_slot(self) -> None:
        """S4 (observer): nothing was submitted, so the Command is safety_blocked, not unknown_result."""
        fx = Fixture()
        connection = fx.connection()
        record = fx.admit(connection, deadline=fx.clock() + timedelta(seconds=5))
        fx.runtime.report_transport_loss(connection, C.DEGRADED)
        self.assertIs(connection.state, C.DEGRADED)
        self.assertIs(record.state, S.SAFETY_BLOCKED)
        self.assertEqual(record.history[-1].evidence, "transport_lost_before_submission")
        self.assertIsNone(fx.executor.active_command(connection))
        fx.clock.advance(timedelta(seconds=60))
        self.assertIs(fx.executor.check_deadline(record.command_id).state, S.SAFETY_BLOCKED)
        self.assertIs(connection.state, C.DEGRADED)

    def test_transport_loss_while_busy_is_a_valid_connection_transition(self) -> None:
        """Section 6: any nonterminal state may lose its transport."""
        for outcome in (C.DISCONNECTED, C.DEGRADED, C.FAILED):
            fx = Fixture()
            connection = fx.connection()
            fx.admit(connection)
            fx.runtime.report_transport_loss(connection, outcome)
            self.assertIs(connection.state, outcome)

    def test_ordinary_disconnect_while_busy_is_refused_without_a_provider_call(self) -> None:
        """REQ-047 (connection side): stays busy, no disconnect call."""
        fx = Fixture()
        connection = fx.connection()
        fx.admit(connection)
        with self.assertRaises(InvalidTransition):
            fx.runtime.disconnect(connection)
        self.assertIs(connection.state, C.BUSY)
        self.assertEqual([c for c in fx.provider.calls if c[0] == "disconnect"], [])

    def test_the_refused_disconnect_leaves_an_audit_record(self) -> None:
        """Contract section 6: busy + ordinary disconnect -> busy with a rejection record and no provider call."""
        fx = Fixture()
        connection = fx.connection()
        fx.admit(connection)
        before = len(connection.history)
        calls = list(fx.provider.calls)
        for _ in range(2):
            with self.assertRaises(InvalidTransition):
                fx.runtime.disconnect(connection)
        records = connection.history[before:]
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertEqual((record.from_state, record.event, record.to_state), ("busy", "disconnect_requested", "busy"))
            self.assertIn("rejected", record.evidence)
        self.assertEqual(fx.provider.calls, calls)
        self.assertIs(connection.state, C.BUSY)

    def test_passive_reads_still_work_while_busy_and_never_change_it(self) -> None:
        fx = Fixture()
        connection = fx.connection()
        fx.admit(connection)
        fx.runtime.read_telemetry(connection)
        fx.runtime.describe_preview(connection)
        fx.runtime.capability_report(connection)
        self.assertIs(fx.runtime.refresh_evidence(connection), C.BUSY)
        fx.provider.script_evidence_failure("scripted")
        self.assertIs(fx.runtime.refresh_evidence(connection), C.BUSY)
        self.assertEqual(len(fx.executor.commands), 1)

    def test_repeated_connect_while_busy_makes_no_provider_call(self) -> None:
        fx = Fixture()
        connection = fx.connection()
        fx.admit(connection)
        before = list(fx.provider.calls)
        with self.assertRaises(InvalidTransition):
            fx.runtime.connect(connection)
        self.assertEqual(fx.provider.calls, before)


class BusyControlTests(unittest.TestCase):
    def test_no_public_connection_event_enters_or_leaves_busy(self) -> None:
        for state in (C.READY, C.CONNECTED, C.DEGRADED):
            fx = Fixture()
            connection = fx.connection(ready=state is not C.CONNECTED)
            if state is C.DEGRADED:
                fx.runtime.report_transport_loss(connection, C.DEGRADED)
            for event in ConnectionEvent:
                probe = fx.connection(ready=state is not C.CONNECTED)
                if state is C.DEGRADED:
                    fx.runtime.report_transport_loss(probe, C.DEGRADED)
                try:
                    probe.apply(event, fx.clock())
                except InvalidTransition:
                    pass
                self.assertIsNot(probe.state, C.BUSY, (state, event))

    def test_apply_busy_requires_the_control_token(self) -> None:
        fx = Fixture()
        connection = fx.connection()
        history = connection.history
        for control in (None, object(), BusyEvent.BUSY_ENTERED, "BUSY_CONTROL"):
            with self.assertRaises(InvalidTransition):
                connection.apply_busy(BusyEvent.BUSY_ENTERED, fx.clock(), "e", control)
        self.assertIs(connection.state, C.READY)
        self.assertEqual(connection.history, history)

    def test_busy_transitions_are_exactly_the_contract_rows(self) -> None:
        fx = Fixture()
        connection = fx.connection(ready=False)
        with self.assertRaises(InvalidTransition):  # connected -> busy is not a contract row
            connection.apply_busy(BusyEvent.BUSY_ENTERED, fx.clock(), "e", BUSY_CONTROL)
        ready = fx.connection()
        with self.assertRaises(InvalidTransition):  # ready has nothing to leave
            ready.apply_busy(BusyEvent.BUSY_LEFT_READY, fx.clock(), "e", BUSY_CONTROL)
        ready.apply_busy(BusyEvent.BUSY_ENTERED, fx.clock(), "e", BUSY_CONTROL)
        with self.assertRaises(InvalidTransition):  # already busy
            ready.apply_busy(BusyEvent.BUSY_ENTERED, fx.clock(), "e", BUSY_CONTROL)
        ready.apply_busy(BusyEvent.BUSY_LEFT_DEGRADED, fx.clock(), "e", BUSY_CONTROL)
        self.assertIs(ready.state, C.DEGRADED)

    def test_only_the_executor_module_drives_busy(self) -> None:
        allowed = {"command_executor.py", "lifecycle.py", "connection.py", "__init__.py"}
        pattern = re.compile(r"BUSY_CONTROL|apply_busy|BusyEvent")
        users = {p.name for p in PACKAGE.glob("*.py") if pattern.search(p.read_text(encoding="utf-8"))}
        self.assertEqual(users - allowed, set())
        self.assertIn("command_executor.py", users)

    def test_the_runtime_never_drives_busy_or_knows_the_executor(self) -> None:
        text = (PACKAGE / "runtime.py").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"BUSY_CONTROL|apply_busy|BusyEvent|command_executor|CommandExecutor|CommandRecord")


class NoSubmissionBoundaryTests(unittest.TestCase):
    def test_executor_never_touches_a_provider(self) -> None:
        text = (PACKAGE / "command_executor.py").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"\._provider|DeviceProvider|\.connect\(|\.disconnect\(|\.discover\(")
        calls = re.findall(r"(\w+(?:\.\w+)*)\.(?:submit_command|poll_command|cancel_command)\(", text)
        self.assertTrue(calls)
        self.assertEqual(set(calls), {"self._command_provider"})
        self.assertNotRegex(text, r"\.(send|execute|invoke)\w*\(")

    def test_provider_contract_gains_no_command_method(self) -> None:
        from tsn_dss.engine.device_runtime import DeviceProvider, SimulatorProvider

        for cls in (DeviceProvider, SimulatorProvider):
            names = {n.lower() for n in dir(cls) if not n.startswith("_")}
            self.assertFalse([n for n in names if "submit" in n or "execute" in n or "command" in n], cls)

    def test_executor_public_surface_is_exactly_the_known_set(self) -> None:
        public = {n for n in dir(CommandExecutor) if not n.startswith("_")}
        self.assertEqual(public, {"admit", "check_deadline", "commands", "get", "active_command", "unresolved_devices", "uncertainty_store", "resolve_uncertainty_by_recovery", "clear_uncertainty_by_operator", "establish_baseline_by_recovery", "establish_baseline_by_operator", "submit", "poll", "cancel", "enforce_deadline"})

    def test_package_exports_nothing_from_the_executor(self) -> None:
        import tsn_dss.engine.device_runtime as pkg

        self.assertTrue({"CommandExecutor", "CommandIntent", "CommandAuthorizer"}.isdisjoint(pkg.__all__))


if __name__ == "__main__":
    unittest.main()
