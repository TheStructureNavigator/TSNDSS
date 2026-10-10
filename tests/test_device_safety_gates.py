"""DB-04 S3: pure safety gates, freshness predicates, gate integration in admission, and admission concurrency."""

from __future__ import annotations

import re
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tsn_dss.engine.device_runtime import ManualClock, ProviderRuntime, SequentialIdGenerator
from tsn_dss.engine.device_runtime.command_executor import CommandExecutor, CommandIntent
from tsn_dss.engine.device_runtime.command_lifecycle import CommandEvent
from tsn_dss.engine.device_runtime.command_models import (
    CommandKindPolicy,
    CommandKindRegistry,
    CommandPolicyError,
    FreshnessRequirement,
)
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
from tsn_dss.engine.device_runtime.safety_gates import (
    CAPABILITY_PREFIX,
    EvidenceSnapshot,
    GateDecision,
    GateReasonCode as R,
    UncertaintyState,
    evaluate_gate,
)

try:
    from device_runtime_support import make_provider
except ImportError:  # pragma: no cover
    from tests.device_runtime_support import make_provider

PACKAGE = Path(__file__).resolve().parents[1] / "tsn_dss" / "engine" / "device_runtime"
NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
PROV, CONN, DEV = "prov", "conn-1", "dev-1"
PR, HO = TelemetrySource.PROVIDER_REPORTED, TelemetrySource.HOST_OBSERVED


def policy(max_age=10, cap_age=30, extra=()):
    return CommandKindPolicy(
        "move", state_changing=True, physical=True, idempotent=False, safety_sensitive=True,
        freshness=(
            FreshnessRequirement("pose", timedelta(seconds=max_age)),
            FreshnessRequirement(CAPABILITY_PREFIX + "move", timedelta(seconds=cap_age)),
            *extra,
        ),
    )


def sample(items=None, age=1.0, provider_age=None, provider=PROV, connection=CONN):
    items = items if items is not None else (TelemetryItem("pose", PR, ValueState.KNOWN, "p"),)
    return TelemetrySample(
        provider_id=provider, connection_id=connection, host_observed_at=NOW - timedelta(seconds=age),
        items=tuple(items), simulated=True,
        provider_reported_at=None if provider_age is None else NOW - timedelta(seconds=provider_age),
    )


def report(available=True, supported=True, age=1.0, name="move", provider=PROV, connection=CONN):
    entry = CapabilityEntry(
        name, supported, available if supported else (None if available is None else False), True,
        CapabilityConfirmation.SIMULATED, True,
    )
    return CapabilityReport(provider, connection, NOW - timedelta(seconds=age), (entry,), True)


def snap(telemetry="default", capabilities="default"):
    return EvidenceSnapshot(
        telemetry=sample() if telemetry == "default" else telemetry,
        capabilities=report() if capabilities == "default" else capabilities,
    )


class View:
    def __init__(self, state=UncertaintyState.RESOLVED_BY_RECOVERY_EVIDENCE, boom=False):
        self.state, self.boom, self.asked = state, boom, []

    def state_for(self, provider_id, device_ref):
        self.asked.append((provider_id, device_ref))
        if self.boom:
            raise RuntimeError("store down")
        return self.state


def gate(pol=None, snapshot="default", now=NOW, view="default"):
    return evaluate_gate(
        pol or policy(), snap() if snapshot == "default" else snapshot, now,
        provider_id=PROV, connection_id=CONN, device_ref=DEV,
        uncertainty=View() if view == "default" else view,
    )


def codes(result):
    return {r.code for r in result.reasons}


class GatePassTests(unittest.TestCase):
    def test_fresh_known_uncontradicted_evidence_passes_with_labeled_basis(self) -> None:
        result = gate()
        self.assertIs(result.decision, GateDecision.PASS)
        self.assertTrue(result.passed)
        self.assertEqual(set(result.evidence_basis), {("pose", "provider_reported"), ("capability:move", "capability_report")})

    def test_a_pass_never_claims_physical_truth_or_operator_proof(self) -> None:
        """REQ-061."""
        for view in (View(), View(UncertaintyState.CLEARED_BY_OPERATOR), View(UncertaintyState.RESOLVED_BY_RECOVERY_EVIDENCE)):
            result = gate(view=view)
            self.assertTrue(result.passed)
            self.assertFalse(result.physical_truth_verified)
        self.assertTrue(gate(view=View(UncertaintyState.CLEARED_BY_OPERATOR)).operator_clearance_used)
        self.assertFalse(gate().operator_clearance_used)

    def test_a_kind_that_is_not_safety_sensitive_needs_no_evidence(self) -> None:
        plain = CommandKindPolicy("p", state_changing=True, physical=False, idempotent=False, safety_sensitive=False)
        self.assertTrue(gate(plain, snapshot=None, view=None).passed)

    def test_the_gate_is_deterministic_and_pure(self) -> None:
        a, b = gate(), gate()
        self.assertEqual(a, b)
        view = View()
        evaluate_gate(policy(), snap(), NOW, provider_id=PROV, connection_id=CONN, device_ref=DEV, uncertainty=view)
        self.assertEqual(view.asked, [(PROV, DEV)])

    def test_naive_time_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            gate(now=datetime(2026, 1, 1))


class FreshnessTests(unittest.TestCase):
    def test_the_limit_is_inclusive_and_one_tick_later_is_expired(self) -> None:
        self.assertTrue(gate(snapshot=snap(sample(age=10.0))).passed)
        late = snap(TelemetrySample(PROV, CONN, NOW - timedelta(seconds=10, microseconds=1),
                                    (TelemetryItem("pose", PR, ValueState.KNOWN, "p"),), True))
        result = gate(snapshot=late)
        self.assertEqual(codes(result), {R.EXPIRED})

    def test_values_come_from_the_policy_not_from_the_gate(self) -> None:
        evidence = snap(sample(age=20.0))
        self.assertEqual(codes(gate(policy(max_age=10), evidence)), {R.EXPIRED})
        self.assertTrue(gate(policy(max_age=25), evidence).passed)
        source = (PACKAGE / "safety_gates.py").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"timedelta\(\s*[^0)]", source), [])
        self.assertNotRegex(source, r"seconds\s*=\s*\d")

    def test_a_future_timestamp_is_not_fresh(self) -> None:
        self.assertEqual(codes(gate(snapshot=snap(sample(age=-1.0)))), {R.FUTURE_TIMESTAMP})
        self.assertEqual(codes(gate(snapshot=snap(sample(age=1.0, provider_age=-5.0)))), {R.FUTURE_TIMESTAMP})

    def test_the_older_of_host_and_provider_time_counts_for_provider_reported_items(self) -> None:
        self.assertEqual(codes(gate(snapshot=snap(sample(age=1.0, provider_age=60.0)))), {R.EXPIRED})
        self.assertTrue(gate(snapshot=snap(sample(age=1.0, provider_age=2.0))).passed)

    def test_every_requirement_must_hold_and_all_reasons_are_reported(self) -> None:
        pol = policy(extra=(FreshnessRequirement("focus", timedelta(seconds=10)),))
        result = gate(pol, snap(sample(age=99.0), report(age=99.0)))
        self.assertEqual({r.item for r in result.reasons}, {"pose", "capability:move", "focus"})
        self.assertEqual(codes(result), {R.EXPIRED, R.MISSING})

    def test_connection_ready_is_not_evidence(self) -> None:
        """Without evidence there is nothing to pass on, whatever the Connection state."""
        self.assertFalse(gate(snapshot=EvidenceSnapshot()).passed)
        self.assertFalse(gate(snapshot=None).passed)


class FailClosedTests(unittest.TestCase):
    def test_missing_evidence_blocks(self) -> None:
        self.assertEqual(codes(gate(snapshot=None)), {R.EVIDENCE_UNAVAILABLE})
        self.assertEqual(codes(gate(snapshot=snap(telemetry=None))), {R.MISSING})
        self.assertEqual(codes(gate(snapshot=snap(capabilities=None))), {R.MISSING})
        self.assertEqual(codes(gate(snapshot=snap(sample(items=())))), {R.MISSING})

    def test_stale_unknown_and_unavailable_values_block(self) -> None:
        for state, code in ((ValueState.STALE, R.STATE_STALE), (ValueState.UNKNOWN, R.STATE_UNKNOWN),
                            (ValueState.UNAVAILABLE, R.STATE_UNAVAILABLE)):
            value = "p" if state is ValueState.STALE else None
            item = TelemetryItem("pose", PR, state, value)
            self.assertEqual(codes(gate(snapshot=snap(sample(items=(item,))))), {code}, state)

    def test_contradictory_known_readings_block_and_agreeing_ones_pass(self) -> None:
        a = TelemetryItem("pose", PR, ValueState.KNOWN, "p1")
        b = TelemetryItem("pose", HO, ValueState.KNOWN, "p2")
        self.assertEqual(codes(gate(snapshot=snap(sample(items=(a, b))))), {R.CONTRADICTORY})
        same = TelemetryItem("pose", HO, ValueState.KNOWN, "p1")
        self.assertTrue(gate(snapshot=snap(sample(items=(a, same)))).passed)

    def test_a_known_reading_does_not_excuse_a_stale_twin(self) -> None:
        a = TelemetryItem("pose", PR, ValueState.KNOWN, "p")
        b = TelemetryItem("pose", HO, ValueState.STALE, "p")
        self.assertEqual(codes(gate(snapshot=snap(sample(items=(a, b))))), {R.STATE_STALE})

    def test_evidence_about_another_target_blocks(self) -> None:
        self.assertEqual(codes(gate(snapshot=snap(sample(connection="other")))), {R.WRONG_TARGET})
        self.assertEqual(codes(gate(snapshot=snap(sample(provider="other")))), {R.WRONG_TARGET})
        self.assertIn(R.WRONG_TARGET, codes(gate(snapshot=snap(capabilities=report(connection="x")))))

    def test_capability_requirements(self) -> None:
        self.assertEqual(codes(gate(snapshot=snap(capabilities=report(available=False)))), {R.STATE_UNAVAILABLE})
        self.assertEqual(codes(gate(snapshot=snap(capabilities=report(available=None)))), {R.STATE_UNKNOWN})
        self.assertEqual(codes(gate(snapshot=snap(capabilities=report(supported=False)))), {R.STATE_UNAVAILABLE})
        self.assertEqual(codes(gate(snapshot=snap(capabilities=report(name="other")))), {R.MISSING})
        self.assertEqual(codes(gate(snapshot=snap(capabilities=report(age=99.0)))), {R.EXPIRED})

    def test_a_kind_declaring_nothing_cannot_exist(self) -> None:
        """REQ-051: refused at construction, so no registry can hold it."""
        with self.assertRaises(CommandPolicyError):
            CommandKindPolicy("p", state_changing=True, physical=False, idempotent=False, safety_sensitive=True)

    def test_defensive_no_freshness_block(self) -> None:
        bad = CommandKindPolicy("p", state_changing=True, physical=False, idempotent=False, safety_sensitive=False)
        object.__setattr__(bad, "safety_sensitive", True)
        self.assertIn(R.NO_FRESHNESS_DECLARED, codes(gate(bad, snap())))


class UncertaintyViewTests(unittest.TestCase):
    def test_missing_unresolved_unknown_and_failing_views_block(self) -> None:
        self.assertEqual(codes(gate(view=None)), {R.UNCERTAINTY_VIEW_MISSING})
        self.assertEqual(codes(gate(view=View(UncertaintyState.UNRESOLVED))), {R.UNCERTAINTY_UNRESOLVED})
        self.assertEqual(codes(gate(view=View(UncertaintyState.UNKNOWN))), {R.UNCERTAINTY_UNKNOWN})
        self.assertEqual(codes(gate(view=View(boom=True))), {R.UNCERTAINTY_UNKNOWN})
        self.assertTrue(gate(view=View(UncertaintyState.UNRESOLVED)).blocked_by_uncertainty)
        self.assertFalse(gate(snapshot=None).blocked_by_uncertainty)

    def test_nothing_recorded_is_not_verified_absence_of_uncertainty(self) -> None:
        """Restart safety: even with perfectly fresh evidence, an unestablished history does not pass."""
        result = gate(view=View(UncertaintyState.NONE_RECORDED))
        self.assertFalse(result.passed)
        self.assertEqual(codes(result), {R.HISTORY_NOT_ESTABLISHED})
        self.assertTrue(result.blocked_by_uncertainty)
        self.assertTrue(gate(view=View(UncertaintyState.RESOLVED_BY_RECOVERY_EVIDENCE)).passed)

    def test_no_uncertainty_recorded_never_replaces_fresh_evidence(self) -> None:
        """REQ-050: an empty store (for example after a restart) proves nothing."""
        result = gate(snapshot=snap(sample(age=99.0)), view=View(UncertaintyState.NONE_RECORDED))
        self.assertFalse(result.passed)

    def test_operator_clearance_does_not_replace_fresh_evidence(self) -> None:
        result = gate(snapshot=snap(sample(age=99.0)), view=View(UncertaintyState.CLEARED_BY_OPERATOR))
        self.assertFalse(result.passed)
        self.assertFalse(result.physical_truth_verified)

    def test_the_view_is_asked_about_the_commands_own_provider_and_device(self) -> None:
        view = View()
        gate(view=view)
        self.assertEqual(view.asked, [(PROV, DEV)])


# --- integration in admission ---------------------------------------------------------------

KIND = policy(max_age=60, cap_age=60)
CHANGE = CommandKindPolicy("change", state_changing=True, physical=False, idempotent=False, safety_sensitive=False)
FRESH_AGE = timedelta(seconds=1)


class Allow:
    def is_authorized(self, requested_by, kind, ref) -> bool:
        return True


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
    def __init__(self, view="default", source="fresh", authorizer=None, threadsafe_clock=False, baseline=True):
        self.provider = make_provider()
        self.runtime = ProviderRuntime(self.provider, clock=ManualClock(), id_generator=SequentialIdGenerator())
        self.device = self.runtime.discover().devices[0]
        self.clock = ManualClock()
        registry = CommandKindRegistry()
        registry.register(KIND)
        registry.register(CHANGE)
        self.view = View() if view == "default" else view
        self.source_calls = 0
        self.executor = CommandExecutor(
            self.runtime, registry, authorizer or Allow(), clock=self.clock, id_generator=SequentialIdGenerator(),
            evidence_source=self.fresh if source == "fresh" else source, uncertainty=self.view,
        )
        if baseline:
            establish_baselines(self.executor, self.runtime, [self.device])

    def fresh(self, connection, pose="p", state=ValueState.KNOWN, cap=True) -> EvidenceSnapshot:
        self.source_calls += 1
        at = self.clock() - FRESH_AGE
        t = TelemetrySample(self.runtime.provider_id, connection.connection_id, at,
                            (TelemetryItem("pose", PR, state, pose if state is not ValueState.UNKNOWN else None),), True)
        entry = CapabilityEntry("move", True, cap, True, CapabilityConfirmation.SIMULATED, True)
        c = CapabilityReport(self.runtime.provider_id, connection.connection_id, at, (entry,), True)
        return EvidenceSnapshot(t, c)

    def ready(self):
        connection = self.runtime.connect(self.runtime.open_connection(self.device))
        self.runtime.refresh_evidence(connection)
        return connection

    def admit(self, connection, kind="move", by="op"):
        return self.executor.admit(CommandIntent(connection, kind, by))


class GateIntegrationTests(unittest.TestCase):
    def test_passing_gate_leaves_the_command_validated_and_holding_busy(self) -> None:
        rig = Rig()
        connection = rig.ready()
        record = rig.admit(connection)
        self.assertIs(record.state, S.VALIDATED)
        self.assertIs(connection.state, C.BUSY)
        self.assertIs(rig.executor.active_command(connection), record)
        self.assertEqual(rig.source_calls, 1)

    def test_failing_gate_blocks_and_releases_the_busy_reservation(self) -> None:
        for label, source in (
            ("stale", lambda c: Rig.fresh(rig, c, state=ValueState.STALE)),
            ("unknown", lambda c: Rig.fresh(rig, c, state=ValueState.UNKNOWN)),
            ("unavailable_capability", lambda c: Rig.fresh(rig, c, cap=False)),
            ("raises", lambda c: (_ for _ in ()).throw(RuntimeError("read failed"))),
            ("none", lambda c: None),
        ):
            rig = Rig(source=source)
            connection = rig.ready()
            record = rig.admit(connection)
            self.assertIs(record.state, S.SAFETY_BLOCKED, label)
            self.assertEqual(record.history[-1].event, CommandEvent.SAFETY_EVIDENCE_INSUFFICIENT.value, label)
            self.assertIs(connection.state, C.READY, label)
            self.assertIsNone(rig.executor.active_command(connection), label)
            events = [(h.event, h.evidence) for h in connection.history[-2:]]
            self.assertEqual(events[0][0], "busy_entered")
            self.assertEqual(events[1], ("busy_left_ready", "safety_blocked"))
            self.assertIs(rig.admit(connection, "change").state, S.VALIDATED)  # the slot is free again

    def test_uncertainty_blocks_use_their_own_event_and_also_release_busy(self) -> None:
        for view in (View(UncertaintyState.UNRESOLVED), View(UncertaintyState.UNKNOWN), View(boom=True), None):
            rig = Rig(view=view)
            connection = rig.ready()
            record = rig.admit(connection)
            self.assertIs(record.state, S.SAFETY_BLOCKED)
            self.assertEqual(record.history[-1].event, CommandEvent.UNCERTAINTY_NOT_CLEARED.value)
            self.assertIs(connection.state, C.READY)
            self.assertIsNone(rig.executor.active_command(connection))

    def test_ready_connection_with_stale_evidence_is_still_blocked(self) -> None:
        rig = Rig(source=lambda c: Rig.fresh(rig, c, state=ValueState.STALE))
        connection = rig.ready()
        self.assertIs(connection.state, C.READY)
        self.assertIs(rig.admit(connection).state, S.SAFETY_BLOCKED)

    def test_evidence_about_another_connection_blocks(self) -> None:
        rig = Rig()
        other = rig.ready()
        rig.executor._evidence_source = lambda c: rig.fresh(other)
        connection = rig.ready()
        self.assertIs(rig.admit(connection).state, S.SAFETY_BLOCKED)
        self.assertIn("wrong_target", rig.executor.commands[-1].history[-1].evidence)

    def test_the_gate_clock_is_read_after_the_evidence(self) -> None:
        """A slow evidence read must not be judged against a time taken before it."""
        rig = Rig()
        fresh = rig.fresh

        def slow(connection):
            snapshot = fresh(connection)
            rig.clock.advance(timedelta(seconds=600))
            return snapshot

        rig.executor._evidence_source = slow
        self.assertIs(rig.admit(rig.ready()).state, S.SAFETY_BLOCKED)

    def test_kinds_that_are_not_safety_sensitive_skip_the_gate_entirely(self) -> None:
        rig = Rig(view=None, source=lambda c: (_ for _ in ()).throw(AssertionError("must not be read")))
        self.assertIs(rig.admit(rig.ready(), "change").state, S.VALIDATED)

    def test_blocked_commands_never_reach_a_provider_beyond_passive_reads(self) -> None:
        rig = Rig(source=None)  # default runtime evidence: simulator has no "pose" telemetry
        rig.executor._evidence_source = rig.executor._runtime_evidence
        connection = rig.ready()
        before = len(rig.provider.calls)
        record = rig.admit(connection)
        self.assertIs(record.state, S.SAFETY_BLOCKED)
        self.assertIn("missing", record.history[-1].evidence)
        new = {call[0] for call in rig.provider.calls[before:]}
        self.assertTrue(new <= {"read_telemetry", "describe_capabilities"}, new)
        self.assertIs(connection.state, C.READY)

    def test_blocked_commands_in_every_path_never_touch_a_provider_for_non_safety_kinds(self) -> None:
        rig = Rig()
        connection = rig.ready()
        before = list(rig.provider.calls)
        rig.admit(connection, "change")
        self.assertEqual(rig.provider.calls, before)

    def test_pre_submission_timeout_does_not_apply_after_possible_submission(self) -> None:
        """S2's check_deadline only touches ``validated``; once submitted (S4) it must stay out of the way."""
        rig = Rig()
        connection = rig.ready()
        record = rig.executor.admit(CommandIntent(connection, "move", "op", deadline=rig.clock() + timedelta(seconds=5)))
        self.assertIs(record.state, S.VALIDATED)
        record.apply(CommandEvent.GATES_PASSED, rig.clock(), "simulated S4 submission boundary")
        rig.clock.advance(timedelta(seconds=600))
        self.assertIs(rig.executor.check_deadline(record.command_id).state, S.SUBMITTED)
        self.assertIs(connection.state, C.BUSY)
        self.assertIs(rig.executor.active_command(connection), record)

    def test_no_new_public_executor_surface_and_no_provider_submission(self) -> None:
        public = {n for n in dir(CommandExecutor) if not n.startswith("_")}
        self.assertEqual(public, {"admit", "check_deadline", "commands", "get", "active_command", "unresolved_devices", "uncertainty_store", "resolve_uncertainty_by_recovery", "clear_uncertainty_by_operator", "establish_baseline_by_recovery", "establish_baseline_by_operator", "submit", "poll", "cancel", "enforce_deadline"})
        text = (PACKAGE / "safety_gates.py").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"\._provider|read_telemetry|\.connect\(|\.disconnect\(|datetime\.now|utc_now|import time")

    def test_gate_integration_is_deterministic(self) -> None:
        def run():
            rig = Rig(source=lambda c: Rig.fresh(rig, c, state=ValueState.STALE))
            connection = rig.ready()
            return [(r.command_id, r.state, r.history[-1].evidence) for r in (rig.admit(connection), rig.admit(connection))]

        self.assertEqual(run(), run())


# --- concurrency ------------------------------------------------------------------------------


class ConcurrencyTests(unittest.TestCase):
    def test_a_second_admission_waits_for_the_first_and_is_then_rejected(self) -> None:
        """Deterministic: the first admission is parked inside its authorizer, holding the slot decision."""
        entered, release = threading.Event(), threading.Event()
        calls = []

        class Parking:
            def is_authorized(self, requested_by, kind, ref):
                calls.append(requested_by)
                if len(calls) == 1:
                    entered.set()
                    if not release.wait(10):
                        raise AssertionError("never released")
                return True

        rig = Rig(authorizer=Parking())
        connection = rig.ready()
        results, errors = {}, []

        def worker(name):
            try:
                results[name] = rig.admit(connection, "change", by=name)
            except BaseException as exc:  # pragma: no cover - the failure being tested for
                errors.append((name, exc))

        first = threading.Thread(target=worker, args=("first",))
        first.start()
        self.assertTrue(entered.wait(10))
        second = threading.Thread(target=worker, args=("second",))
        second.start()
        second.join(0.3)  # without serialization the second would finish (and win the slot) now
        release.set()
        first.join(10)
        second.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(results["first"].state, S.VALIDATED)
        self.assertEqual(results["second"].state, S.REJECTED)
        self.assertEqual(results["second"].history[-1].evidence, f"active_command:{results['first'].command_id}")
        self.assertIs(rig.executor.active_command(connection), results["first"])
        self.assertEqual([h.event for h in connection.history].count("busy_entered"), 1)

    def test_many_simultaneous_admissions_reserve_the_slot_exactly_once(self) -> None:
        rig = Rig()
        connection = rig.ready()
        count = 12
        barrier = threading.Barrier(count)
        results, errors = [], []

        def worker(index):
            try:
                barrier.wait(10)
                results.append(rig.admit(connection, "change", by=f"op{index}"))
            except BaseException as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual(errors, [])
        states = sorted(r.state.value for r in results)
        self.assertEqual(states, ["rejected"] * (count - 1) + ["validated"])
        self.assertEqual([h.event for h in connection.history].count("busy_entered"), 1)
        self.assertIs(connection.state, C.BUSY)
        self.assertEqual(len({r.command_id for r in results}), count)

    def test_a_blocked_admission_releases_the_slot_for_the_next_waiter(self) -> None:
        rig = Rig(source=lambda c: Rig.fresh(rig, c, state=ValueState.STALE))
        connection = rig.ready()
        barrier = threading.Barrier(6)
        results = []

        def worker(i):
            barrier.wait(10)
            results.append(rig.admit(connection, by=f"op{i}"))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual({r.state for r in results}, {S.SAFETY_BLOCKED})
        self.assertIs(connection.state, C.READY)
        self.assertIsNone(rig.executor.active_command(connection))


if __name__ == "__main__":
    unittest.main()
