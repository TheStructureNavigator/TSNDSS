"""DB-04 S4: submission, outcomes, deadlines, transport loss and cancellation (simulator only)."""

from __future__ import annotations

import re
import threading
import unittest
from datetime import timedelta
from pathlib import Path

from tsn_dss.engine.device_runtime import DeviceProvider, ManualClock, ProviderRuntime, SequentialIdGenerator, SimulatorProvider
from tsn_dss.engine.device_runtime.command_effects import EffectVerdict, EffectVerdictKind as V
from tsn_dss.engine.device_runtime.command_executor import CommandExecutor, CommandIntent
from tsn_dss.engine.device_runtime.command_lifecycle import CommandEvent
from tsn_dss.engine.device_runtime.command_models import CommandKindPolicy, CommandKindRegistry, FreshnessRequirement
from tsn_dss.engine.device_runtime.errors import DeviceRuntimeError, InvalidTransition
from tsn_dss.engine.device_runtime.models import (
    CapabilityConfirmation,
    CapabilityEntry,
    CapabilityReport,
    CommandState as S,
    ConnectionState as C,
    TelemetryItem,
    TelemetrySample,
    TelemetrySource,
    ValueState,
)
from tsn_dss.engine.device_runtime.provider import (
    CommandCapableProvider,
    ProviderCancelOutcome as CO,
    ProviderCommandReport,
    ProviderCommandStatus as PS,
)
from tsn_dss.engine.device_runtime.safety_gates import EvidenceSnapshot, UncertaintyState
from tsn_dss.engine.device_runtime.simulator import CommandScript, SimulatedDeviceSpec, SimulatorCommandProvider

PACKAGE = Path(__file__).resolve().parents[1] / "tsn_dss" / "engine" / "device_runtime"
MINUTE = timedelta(minutes=1)

CONFIG = CommandKindPolicy("config", state_changing=True, physical=False, idempotent=False, safety_sensitive=False)
IDEM = CommandKindPolicy("idem", state_changing=True, physical=False, idempotent=True, safety_sensitive=False)
QUERY = CommandKindPolicy("query", state_changing=False, physical=False, idempotent=True, safety_sensitive=False)
MOVE = CommandKindPolicy(
    "move", state_changing=True, physical=True, idempotent=False, safety_sensitive=True,
    freshness=(FreshnessRequirement("pose", MINUTE), FreshnessRequirement("capability:move", MINUTE)),
)


class Allow:
    def is_authorized(self, requested_by, kind, ref) -> bool:
        return True


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


class Rig:
    def __init__(self, devices=None, verifiers=None, with_view=True, with_provider=True, store=None, baseline=True, **extra) -> None:
        kw = {"clock": ManualClock()}
        if devices:
            kw["devices"] = devices
        self.provider = SimulatorCommandProvider(**kw)
        self.runtime = ProviderRuntime(self.provider, clock=ManualClock(), id_generator=SequentialIdGenerator())
        self.devices = self.runtime.discover().devices
        self.clock = ManualClock()
        self.stale = False
        registry = CommandKindRegistry()
        for policy in (CONFIG, IDEM, QUERY, MOVE):
            registry.register(policy)
        self.executor = CommandExecutor(
            self.runtime, registry, Allow(), clock=self.clock, id_generator=SequentialIdGenerator(),
            evidence_source=self.evidence, uncertainty=store if store is not None else (Clear() if with_view else None),
            command_provider=self.provider if with_provider else None, verifiers=verifiers, **extra,
        )
        if baseline:
            establish_baselines(self.executor, self.runtime, self.devices)

    def evidence(self, connection) -> EvidenceSnapshot:
        at = self.clock() - timedelta(seconds=1)
        state = ValueState.STALE if self.stale else ValueState.KNOWN
        t = TelemetrySample(self.runtime.provider_id, connection.connection_id, at,
                            (TelemetryItem("pose", TelemetrySource.PROVIDER_REPORTED, state, "p"),), True)
        e = CapabilityEntry("move", True, True, True, CapabilityConfirmation.SIMULATED, True)
        return EvidenceSnapshot(t, CapabilityReport(self.runtime.provider_id, connection.connection_id, at, (e,), True))

    def ready(self, index=0):
        connection = self.runtime.connect(self.runtime.open_connection(self.devices[index]))
        self.runtime.refresh_evidence(connection)
        return connection

    def admit(self, connection, kind="config", deadline=None, key=None):
        return self.executor.admit(CommandIntent(connection, kind, "op", deadline=deadline, idempotency_key=key))

    def submits(self):
        return [c for c in self.provider.calls if c[0] == "submit_command"]

    def calls(self, name):
        return [c for c in self.provider.calls if c[0] == name]

    def script(self, **kw):
        self.provider.script_next_command(CommandScript(**kw))

    def deadline(self, seconds=30):
        return self.clock() + timedelta(seconds=seconds)

    def started(self, kind="config", **script):
        """Admit and submit one Command with a scripted outcome; returns (connection, record)."""
        connection = self.ready()
        if script:
            self.script(**script)
        record = self.admit(connection, kind, deadline=self.deadline())
        self.executor.submit(record.command_id)
        return connection, record


def done(status):
    return ProviderCommandReport(status)


class SubmissionTests(unittest.TestCase):
    def test_submit_acknowledges_and_an_acknowledgement_is_not_success(self) -> None:
        rig = Rig()
        connection, record = rig.started()
        self.assertIs(record.state, S.ACKNOWLEDGED)
        self.assertEqual(len(rig.submits()), 1)
        self.assertIs(connection.state, C.BUSY)
        self.assertIs(rig.executor.active_command(connection), record)
        self.assertEqual([h.to_state for h in record.history], ["validated", "submitted", "acknowledged"])

    def test_the_possible_submission_boundary_is_recorded_before_the_provider_call(self) -> None:
        rig = Rig()
        seen = []
        original = rig.provider.submit_command

        def spy(connection_id, command_id, kind_id, key):
            seen.append(rig.executor.get(command_id).state)
            return original(connection_id, command_id, kind_id, key)

        rig.provider.submit_command = spy
        rig.started()
        self.assertEqual(seen, [S.SUBMITTED])

    def test_a_command_is_submitted_at_most_once(self) -> None:
        rig = Rig()
        _, record = rig.started()
        for _ in range(3):
            with self.assertRaises(InvalidTransition):
                rig.executor.submit(record.command_id)
        self.assertEqual(len(rig.submits()), 1)

    def test_nothing_is_submitted_for_rejected_blocked_or_expired_requests(self) -> None:
        rig = Rig()
        connection = rig.ready()
        records = [
            rig.admit(connection, "nope"),
            rig.executor.admit(CommandIntent(connection, "config", " ")),
            rig.admit(connection, "config", deadline=rig.clock() - timedelta(seconds=1)),
        ]
        first = rig.admit(connection)
        records.append(rig.admit(connection))  # conflicting
        rig.stale = True
        rig.executor.check_deadline(first.command_id)
        for record in records:
            self.assertTrue(record.state.is_terminal)
            with self.assertRaises(InvalidTransition):
                rig.executor.submit(record.command_id)
        self.assertEqual(rig.submits(), [])

    def test_a_deadline_that_elapsed_before_submit_times_out_without_a_provider_call(self) -> None:
        rig = Rig()
        connection = rig.ready()
        record = rig.admit(connection, deadline=rig.deadline(5))
        rig.clock.advance(timedelta(seconds=60))
        rig.executor.submit(record.command_id)
        self.assertIs(record.state, S.TIMED_OUT)
        self.assertEqual(rig.submits(), [])
        self.assertIs(connection.state, C.READY)
        self.assertIsNone(rig.executor.active_command(connection))

    def test_the_gate_runs_again_right_before_submission(self) -> None:
        rig = Rig()
        connection = rig.ready()
        record = rig.admit(connection, "move")
        self.assertIs(record.state, S.VALIDATED)
        rig.stale = True
        rig.executor.submit(record.command_id)
        self.assertIs(record.state, S.SAFETY_BLOCKED)
        self.assertEqual(rig.submits(), [])
        self.assertIs(connection.state, C.READY)
        self.assertIsNone(rig.executor.active_command(connection))

    def test_a_command_whose_connection_was_lost_is_never_submitted(self) -> None:
        rig = Rig()
        connection = rig.ready()
        record = rig.admit(connection)
        rig.runtime.report_transport_loss(connection, C.DISCONNECTED)
        self.assertIs(record.state, S.SAFETY_BLOCKED)
        with self.assertRaises(InvalidTransition):
            rig.executor.submit(record.command_id)
        self.assertEqual(rig.submits(), [])

    def test_a_command_capable_provider_is_required_and_must_match_the_runtime(self) -> None:
        rig = Rig(with_provider=False)
        record = rig.admit(rig.ready())
        with self.assertRaises(DeviceRuntimeError):
            rig.executor.submit(record.command_id)
        self.assertIs(record.state, S.VALIDATED)
        plain = SimulatorProvider(clock=ManualClock())
        runtime = ProviderRuntime(plain, clock=ManualClock(), id_generator=SequentialIdGenerator())
        with self.assertRaises(DeviceRuntimeError):
            CommandExecutor(runtime, CommandKindRegistry(), Allow(), command_provider=plain)  # type: ignore[arg-type]
        other = SimulatorCommandProvider(provider_id="other", clock=ManualClock())
        with self.assertRaises(DeviceRuntimeError):
            CommandExecutor(rig.runtime, CommandKindRegistry(), Allow(), command_provider=other)


class ProviderRejectionTests(unittest.TestCase):
    def test_rejection_without_possible_effect_is_failed(self) -> None:
        rig = Rig()
        connection, record = rig.started(submit="reject_no_effect")
        self.assertIs(record.state, S.FAILED)
        self.assertIs(connection.state, C.READY)
        self.assertEqual(len(rig.submits()), 1)

    def test_rejection_with_possible_effect_is_unknown_result_not_failed(self) -> None:
        rig = Rig()
        connection, record = rig.started("config", submit="reject_effect_possible")
        self.assertIs(record.state, S.UNKNOWN_RESULT)
        self.assertEqual(rig.executor.unresolved_devices(), frozenset())  # not a physical Command
        self.assertIs(connection.state, C.READY)

    def test_physical_rejection_with_possible_effect_latches_the_device_and_degrades(self) -> None:
        rig = Rig()
        connection, record = rig.started("move", submit="reject_effect_possible")
        self.assertIs(record.state, S.UNKNOWN_RESULT)
        self.assertEqual(rig.executor.unresolved_devices(), {(rig.runtime.provider_id, connection.device.device_ref)})
        self.assertIs(connection.state, C.DEGRADED)
        last = connection.history[-1]
        self.assertEqual((last.event, last.evidence), ("busy_left_degraded", "unknown_result"))


class RejectionDefaultsTests(unittest.TestCase):
    def test_anything_but_an_explicit_no_effect_statement_is_a_possible_effect(self) -> None:
        from tsn_dss.engine.device_runtime.errors import ProviderCommandRejected

        for stated in (None, 0, "no"):
            rig = Rig()
            def refuse(*args, _stated=stated, **kw):
                raise ProviderCommandRejected("odd", effect_possible=_stated)
            rig.provider.submit_command = refuse
            connection = rig.ready()
            record = rig.admit(connection)
            rig.executor.submit(record.command_id)
            self.assertIs(record.state, S.UNKNOWN_RESULT, stated)


class ProgressAndVerificationTests(unittest.TestCase):
    def test_progress_then_verified_success(self) -> None:
        verifier = lambda connection, record: EffectVerdict(V.VERIFIED, "fresh post-command reading")
        rig = Rig(verifiers={"move": verifier})
        connection, record = rig.started("move", polls=(PS.IN_PROGRESS, PS.REPORTED_COMPLETE))
        rig.executor.poll(record.command_id)
        self.assertIs(record.state, S.IN_PROGRESS)
        self.assertIs(connection.state, C.BUSY)
        rig.executor.poll(record.command_id)
        self.assertIs(record.state, S.SUCCEEDED)
        self.assertEqual(record.history[-1].evidence, "fresh post-command reading")
        self.assertIs(connection.state, C.READY)
        self.assertEqual(rig.executor.unresolved_devices(), frozenset())

    def test_provider_completion_alone_is_never_success(self) -> None:
        for verifier in (None, lambda c, r: EffectVerdict(V.PENDING), lambda c, r: 1 / 0):
            rig = Rig(verifiers={"move": verifier} if verifier else None)
            connection, record = rig.started("move", polls=(PS.IN_PROGRESS, PS.REPORTED_COMPLETE))
            rig.executor.poll(record.command_id)
            rig.executor.poll(record.command_id)
            self.assertIs(record.state, S.IN_PROGRESS)
            self.assertIs(connection.state, C.BUSY)

    def test_non_physical_kind_may_be_verified_by_acknowledgement(self) -> None:
        rig = Rig(verifiers={"config": lambda c, r: EffectVerdict(V.VERIFIED_BY_ACKNOWLEDGEMENT, "setting read back")})
        connection, record = rig.started("config", polls=(PS.REPORTED_COMPLETE,))
        rig.executor.poll(record.command_id)
        self.assertIs(record.state, S.SUCCEEDED)
        self.assertEqual(record.history[-1].event, CommandEvent.ACKNOWLEDGEMENT_IS_VERIFIED_EFFECT.value)
        self.assertIs(connection.state, C.READY)

    def test_a_physical_kind_cannot_be_verified_by_acknowledgement(self) -> None:
        rig = Rig(verifiers={"move": lambda c, r: EffectVerdict(V.VERIFIED_BY_ACKNOWLEDGEMENT, "x")})
        _, record = rig.started("move", polls=(PS.REPORTED_COMPLETE,))
        rig.executor.poll(record.command_id)
        self.assertIs(record.state, S.ACKNOWLEDGED)

    def test_failed_verification_is_failed(self) -> None:
        rig = Rig(verifiers={"config": lambda c, r: EffectVerdict(V.FAILED, "read back differs")})
        _, record = rig.started("config", polls=(PS.REPORTED_COMPLETE,))
        rig.executor.poll(record.command_id)
        self.assertIs(record.state, S.FAILED)

    def test_provider_failure_report_of_a_kind_that_cannot_have_a_physical_effect_is_failed(self) -> None:
        for effect_possible in (True, False, None):
            rig = Rig()
            _, record = rig.started("config", polls=(ProviderCommandReport(PS.REPORTED_FAILED, effect_possible=effect_possible),))
            rig.executor.poll(record.command_id)
            self.assertIs(record.state, S.FAILED, effect_possible)

    def test_physical_failure_report_with_no_possible_effect_is_failed(self) -> None:
        """Amendment A1, first row: only the Provider's explicit statement lets a physical failure be `failed`."""
        for polls_before in (0, 1):
            rig = Rig()
            polls = (PS.IN_PROGRESS,) * polls_before + (ProviderCommandReport(PS.REPORTED_FAILED, effect_possible=False),)
            connection, record = rig.started("move", polls=polls)
            for _ in polls:
                rig.executor.poll(record.command_id)
            self.assertIs(record.state, S.FAILED, polls_before)
            self.assertIs(connection.state, C.READY)
            self.assertEqual(rig.executor.unresolved_devices(), frozenset())
            self.assertIn("no physical effect possible", record.history[-1].evidence)

    def test_physical_failure_report_with_a_possible_effect_is_unknown_result_at_once(self) -> None:
        """Amendment A1, second row: classified immediately, not left open until the deadline."""
        for stated in (True, None, 0, "no", "false"):
            for stage in ("acknowledged", "in_progress"):
                rig = Rig()
                polls = ((PS.IN_PROGRESS,) if stage == "in_progress" else ()) + (
                    ProviderCommandReport(PS.REPORTED_FAILED, effect_possible=stated, detail="axis stalled"),)
                connection, record = rig.started("move", polls=polls)
                for _ in polls:
                    rig.executor.poll(record.command_id)
                self.assertIs(record.state, S.UNKNOWN_RESULT, (stated, stage))
                self.assertEqual(record.history[-1].event, CommandEvent.PROVIDER_FAILURE_EFFECT_POSSIBLE.value)
                self.assertIn("physical effect may have occurred", record.history[-1].evidence)
                self.assertEqual(len(rig.executor.unresolved_devices()), 1, (stated, stage))
                self.assertIs(connection.state, C.DEGRADED)
                self.assertIsNone(rig.executor.active_command(connection))
                self.assertEqual(len(rig.submits()), 1)  # never retried

    def test_the_default_provider_report_assumes_a_possible_effect(self) -> None:
        self.assertIs(ProviderCommandReport(PS.REPORTED_FAILED).effect_possible, True)
        rig = Rig()
        _, record = rig.started("move", polls=(PS.REPORTED_FAILED,))
        rig.executor.poll(record.command_id)
        self.assertIs(record.state, S.UNKNOWN_RESULT)

    def test_a_provider_failure_report_without_a_deadline_does_not_wait_forever(self) -> None:
        rig = Rig()
        connection = rig.ready()
        rig.script(polls=(PS.REPORTED_FAILED,))
        record = rig.admit(connection, "move")  # no deadline
        rig.executor.submit(record.command_id)
        rig.executor.poll(record.command_id)
        self.assertIs(record.state, S.UNKNOWN_RESULT)

    def test_a_failure_report_never_establishes_physical_truth_or_resolves_uncertainty(self) -> None:
        rig = Rig()
        connection, record = rig.started("move", polls=(PS.REPORTED_FAILED,))
        rig.executor.poll(record.command_id)
        entries = rig.executor.uncertainty_store.entries(rig.runtime.provider_id, connection.device.device_ref)
        self.assertEqual([e.unresolved for e in entries], [True])
        self.assertIs(rig.admit(rig.ready(), "move").state, S.SAFETY_BLOCKED)

    def test_polling_requires_an_acknowledged_or_in_progress_command(self) -> None:
        rig = Rig()
        connection = rig.ready()
        record = rig.admit(connection)
        with self.assertRaises(InvalidTransition):
            rig.executor.poll(record.command_id)
        self.assertEqual(rig.calls("poll_command"), [])

    def test_success_is_only_ever_reached_through_a_verification_event(self) -> None:
        rig = Rig(verifiers={"move": lambda c, r: EffectVerdict(V.VERIFIED, "e")})
        _, record = rig.started("move", polls=(PS.REPORTED_COMPLETE,))
        rig.executor.poll(record.command_id)
        step = [h for h in record.history if h.to_state == "succeeded"]
        self.assertEqual([h.event for h in step], ["effect_verified"])


class DeadlineTests(unittest.TestCase):
    def test_non_physical_command_times_out_after_submission(self) -> None:
        rig = Rig()
        connection, record = rig.started("config")
        self.assertIs(rig.executor.enforce_deadline(record.command_id).state, S.ACKNOWLEDGED)  # not yet elapsed
        rig.clock.advance(timedelta(minutes=5))
        self.assertIs(rig.executor.enforce_deadline(record.command_id).state, S.TIMED_OUT)
        self.assertEqual(record.history[-1].event, "deadline_non_physical")
        self.assertIs(connection.state, C.READY)

    def test_physical_command_past_its_deadline_is_unknown_result_never_timed_out_or_failed(self) -> None:
        for polls in ((PS.ACKNOWLEDGED,), (PS.IN_PROGRESS,)):
            rig = Rig()
            connection, record = rig.started("move", polls=polls)
            rig.executor.poll(record.command_id)
            rig.clock.advance(timedelta(minutes=5))
            rig.executor.enforce_deadline(record.command_id)
            self.assertIs(record.state, S.UNKNOWN_RESULT)
            self.assertIs(connection.state, C.DEGRADED)
            self.assertEqual(len(rig.submits()), 1)

    def test_no_deadline_never_expires_and_validated_commands_use_the_pre_submission_path(self) -> None:
        rig = Rig()
        connection = rig.ready()
        record = rig.admit(connection)
        rig.executor.submit(record.command_id)
        rig.clock.advance(timedelta(days=1))
        self.assertIs(rig.executor.enforce_deadline(record.command_id).state, S.ACKNOWLEDGED)
        connection = rig.ready()
        validated = rig.admit(connection, deadline=rig.deadline(5))
        rig.clock.advance(timedelta(seconds=60))
        self.assertIs(rig.executor.enforce_deadline(validated.command_id).state, S.TIMED_OUT)
        self.assertEqual(validated.history[-1].event, "deadline_before_submission")


class CancellationTests(unittest.TestCase):
    def test_cancel_with_no_effect_evidence_is_cancelled(self) -> None:
        for kind in ("config", "move"):
            rig = Rig()
            connection, record = rig.started(kind, cancel=CO.CANCELLED_NO_EFFECT, cancel_evidence="motion never started")
            rig.executor.cancel(record.command_id)
            self.assertIs(record.state, S.CANCELLED, kind)
            self.assertEqual(record.history[-1].evidence, "motion never started")
            self.assertIs(connection.state, C.READY)
            self.assertEqual(rig.executor.unresolved_devices(), frozenset())

    def test_cancel_without_evidence_or_with_a_race_preserves_uncertainty(self) -> None:
        for kind, latched in (("config", False), ("move", True)):
            for result in (dict(cancel=CO.CANCELLED_NO_EFFECT, cancel_evidence=" "), dict(cancel=CO.RACE_UNDETERMINED)):
                rig = Rig()
                connection, record = rig.started(kind, **result)
                rig.executor.cancel(record.command_id)
                self.assertIs(record.state, S.UNKNOWN_RESULT, (kind, result))
                self.assertEqual(bool(rig.executor.unresolved_devices()), latched)
                self.assertIs(connection.state, C.DEGRADED if latched else C.READY)

    def test_a_refused_cancellation_changes_nothing(self) -> None:
        rig = Rig()
        connection, record = rig.started("move", cancel=CO.REFUSED)
        rig.executor.cancel(record.command_id)
        self.assertIs(record.state, S.ACKNOWLEDGED)
        self.assertIs(connection.state, C.BUSY)

    def test_cancel_after_transport_failure_is_unknown_result(self) -> None:
        rig = Rig()
        connection, record = rig.started("move", cancel="transport_loss")
        rig.executor.cancel(record.command_id)
        self.assertIs(record.state, S.UNKNOWN_RESULT)
        self.assertIs(connection.state, C.DEGRADED)

    def test_cancel_needs_a_submitted_command(self) -> None:
        rig = Rig()
        record = rig.admit(rig.ready())
        with self.assertRaises(InvalidTransition):
            rig.executor.cancel(record.command_id)
        self.assertEqual(rig.calls("cancel_command"), [])


class TransportLossTests(unittest.TestCase):
    def test_loss_while_submitting_is_unknown_result_with_an_independent_connection_state(self) -> None:
        rig = Rig()
        connection, record = rig.started("config", submit="transport_loss")
        self.assertIs(record.state, S.UNKNOWN_RESULT)
        self.assertIs(connection.state, C.DEGRADED)
        self.assertEqual(len(rig.submits()), 1)
        self.assertEqual(rig.executor.unresolved_devices(), frozenset())

    def test_physical_loss_while_submitting_latches_the_device(self) -> None:
        rig = Rig()
        connection, record = rig.started("move", submit="transport_loss")
        self.assertIs(record.state, S.UNKNOWN_RESULT)
        self.assertEqual(rig.executor.unresolved_devices(), {(rig.runtime.provider_id, connection.device.device_ref)})

    def test_loss_while_polling_is_unknown_result(self) -> None:
        rig = Rig()
        connection, record = rig.started("move", polls=(PS.IN_PROGRESS, "transport_loss"))
        rig.executor.poll(record.command_id)
        rig.executor.poll(record.command_id)
        self.assertIs(record.state, S.UNKNOWN_RESULT)
        self.assertIs(connection.state, C.DEGRADED)

    def test_runtime_reported_loss_classifies_every_open_command(self) -> None:
        for outcome in (C.DISCONNECTED, C.DEGRADED, C.FAILED):
            for stage in ("validated", "acknowledged", "in_progress"):
                rig = Rig()
                connection = rig.ready()
                record = rig.admit(connection, "move", deadline=rig.deadline())
                reader = rig.admit(connection, "query")
                if stage != "validated":
                    rig.script(polls=(PS.IN_PROGRESS,))
                    rig.executor.submit(record.command_id)
                    if stage == "in_progress":
                        rig.executor.poll(record.command_id)
                rig.runtime.report_transport_loss(connection, outcome)
                expected = S.SAFETY_BLOCKED if stage == "validated" else S.UNKNOWN_RESULT
                self.assertIs(record.state, expected, (outcome, stage))
                self.assertIs(reader.state, S.SAFETY_BLOCKED, (outcome, stage))
                self.assertIs(connection.state, outcome, "Connection state stays what the runtime recorded")
                self.assertIsNone(rig.executor.active_command(connection))
                self.assertEqual(bool(rig.executor.unresolved_devices()), stage != "validated")
                self.assertLessEqual(len(rig.submits()), 1)

    def test_loss_after_a_terminal_outcome_touches_nothing(self) -> None:
        rig = Rig()
        connection, record = rig.started("config", submit="reject_no_effect")
        history = record.history
        rig.runtime.report_transport_loss(connection, C.DEGRADED)
        self.assertEqual(record.history, history)


class UncertaintyAndRetryTests(unittest.TestCase):
    def test_an_uncertain_physical_command_is_never_retried(self) -> None:
        for script in (dict(submit="transport_loss"), dict(submit="reject_effect_possible"),
                       dict(polls=(PS.ACKNOWLEDGED,))):
            rig = Rig()
            connection, record = rig.started("move", **script)
            if record.state is not S.UNKNOWN_RESULT:
                rig.clock.advance(timedelta(minutes=5))
                rig.executor.enforce_deadline(record.command_id)
            self.assertIs(record.state, S.UNKNOWN_RESULT, script)
            with self.assertRaises(InvalidTransition):
                rig.executor.submit(record.command_id)
            rig.executor.enforce_deadline(record.command_id)
            self.assertEqual(len(rig.submits()), 1, script)

    def test_unresolved_uncertainty_blocks_later_safety_sensitive_commands_even_after_reconnect(self) -> None:
        rig = Rig()
        connection, record = rig.started("move", submit="transport_loss")
        rig.runtime.disconnect(connection) if connection.state is not C.DISCONNECTED else None
        again = rig.ready()
        self.assertIsNot(again.connection_id, connection.connection_id)
        blocked = rig.admit(again, "move")
        self.assertIs(blocked.state, S.SAFETY_BLOCKED)
        self.assertEqual(blocked.history[-1].event, CommandEvent.UNCERTAINTY_NOT_CLEARED.value)
        self.assertIn("uncertainty_unresolved", blocked.history[-1].evidence)
        self.assertEqual(len(rig.submits()), 1)
        self.assertIs(again.state, C.READY)

    def test_the_latch_is_per_device_and_does_not_block_non_safety_kinds(self) -> None:
        rig = Rig(devices=(SimulatedDeviceSpec("sim-a"), SimulatedDeviceSpec("sim-b")))
        a, record = rig.started("move", submit="transport_loss")
        b = rig.ready(1)
        self.assertIs(rig.admit(b, "move").state, S.VALIDATED)
        a2 = rig.ready(0)  # reconnect to the latched device: non-safety kinds still work
        self.assertIs(rig.admit(a2, "config").state, S.VALIDATED)

    def test_a_degraded_connection_with_unresolved_uncertainty_does_not_return_to_ready(self) -> None:
        rig = Rig()
        connection, record = rig.started("move", submit="transport_loss")
        self.assertIs(connection.state, C.DEGRADED)
        self.assertIs(rig.runtime.refresh_evidence(connection), C.CONNECTED)  # reads work; ready is withheld
        self.assertIs(rig.admit(connection, "move").state, S.SAFETY_BLOCKED)
        self.assertEqual(len(rig.executor.unresolved_devices()), 1)

    def test_a_missing_uncertainty_view_keeps_safety_sensitive_commands_blocked(self) -> None:
        rig = Rig(with_view=False)
        record = rig.admit(rig.ready(), "move")
        self.assertIs(record.state, S.SAFETY_BLOCKED)
        self.assertEqual(rig.submits(), [])

    def test_idempotent_kinds_are_not_retried_either_and_do_not_latch(self) -> None:
        rig = Rig()
        connection, record = rig.started("idem", submit="transport_loss")
        self.assertIs(record.state, S.UNKNOWN_RESULT)
        self.assertEqual(rig.executor.unresolved_devices(), frozenset())
        with self.assertRaises(InvalidTransition):
            rig.executor.submit(record.command_id)
        self.assertEqual(len(rig.submits()), 1)


class ConcurrencyAndBusyTests(unittest.TestCase):
    def test_simultaneous_submit_calls_reach_the_provider_once(self) -> None:
        rig = Rig()
        connection = rig.ready()
        record = rig.admit(connection)
        barrier = threading.Barrier(8)
        outcomes = []

        def worker():
            barrier.wait(10)
            try:
                rig.executor.submit(record.command_id)
                outcomes.append("ok")
            except InvalidTransition:
                outcomes.append("refused")

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual(sorted(outcomes), ["ok"] + ["refused"] * 7)
        self.assertEqual(len(rig.submits()), 1)

    def test_busy_is_held_until_a_terminal_outcome_and_a_second_command_is_rejected_meanwhile(self) -> None:
        rig = Rig()
        connection, record = rig.started("config")
        second = rig.admit(connection)
        self.assertIs(second.state, S.REJECTED)
        self.assertIs(connection.state, C.BUSY)
        rig.clock.advance(timedelta(minutes=5))
        rig.executor.enforce_deadline(record.command_id)
        self.assertIs(connection.state, C.READY)
        self.assertIs(rig.admit(connection).state, S.VALIDATED)

    def test_leaving_busy_to_ready_is_not_a_safety_claim_for_non_physical_unknown_results(self) -> None:
        rig = Rig()
        connection, record = rig.started("config", polls=(PS.ACKNOWLEDGED,))
        rig.executor.cancel(record.command_id)  # REFUSED by default script
        self.assertIs(record.state, S.ACKNOWLEDGED)


class BoundaryTests(unittest.TestCase):
    def test_only_the_executor_calls_provider_command_methods(self) -> None:
        pattern = re.compile(r"\.(submit_command|poll_command|cancel_command)\(")
        users = {p.name for p in PACKAGE.glob("*.py") if pattern.search(p.read_text(encoding="utf-8"))}
        self.assertEqual(users, {"command_executor.py"})

    def test_device_provider_stays_read_only_and_command_support_is_a_separate_protocol(self) -> None:
        for cls in (DeviceProvider, SimulatorProvider):
            names = {n.lower() for n in dir(cls) if not n.startswith("_")}
            self.assertFalse([n for n in names if "command" in n or "submit" in n], cls)
        self.assertNotIsInstance(SimulatorProvider(clock=ManualClock()), CommandCapableProvider)
        self.assertIsInstance(SimulatorCommandProvider(clock=ManualClock()), CommandCapableProvider)
        self.assertIsInstance(SimulatorCommandProvider(clock=ManualClock()), DeviceProvider)

    def test_the_runtime_has_no_command_surface_and_does_not_know_the_executor(self) -> None:
        text = (PACKAGE / "runtime.py").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"submit_command|poll_command|cancel_command|CommandExecutor|CommandRecord|command_executor")

    def test_no_default_permissive_authorizer_gate_or_view(self) -> None:
        text = (PACKAGE / "command_executor.py").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"return True\s*$|lambda .*: True")
        with self.assertRaises(TypeError):
            CommandExecutor(Rig().runtime, CommandKindRegistry())  # type: ignore[call-arg]

    def test_simulator_commands_are_deterministic_and_logged(self) -> None:
        def run():
            rig = Rig()
            connection, record = rig.started("move", polls=(PS.IN_PROGRESS,))
            rig.executor.poll(record.command_id)
            return [c[0] for c in rig.provider.calls if "command" in c[0]], record.state

        self.assertEqual(run(), run())
        self.assertEqual(run()[0], ["submit_command", "poll_command"])

    def test_the_package_still_names_no_vendor_or_acquisition_concepts(self) -> None:
        for path in PACKAGE.glob("*.py"):
            text = path.read_text(encoding="utf-8").lower()
            for token in ("seestar", "rtsp", "alpaca", "ascom"):
                self.assertNotIn(token, text, (path.name, token))


if __name__ == "__main__":
    unittest.main()
