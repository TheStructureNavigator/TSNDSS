"""DB-05 Slice 0: allowed-value constraints in the provider-neutral safety gate (STATE_UNSAFE)."""

from __future__ import annotations

import re
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
    value_allowed,
)
from tsn_dss.engine.device_runtime.models import (
    CommandState as S,
    ConnectionState as C,
    TelemetryItem,
    TelemetrySample,
    TelemetrySource,
    ValueState,
)
from tsn_dss.engine.device_runtime.safety_gates import (
    EvidenceSnapshot,
    GateReasonCode as R,
    UncertaintyState,
    evaluate_gate,
)
from tsn_dss.engine.device_runtime.simulator import SimulatorCommandProvider

try:
    from test_device_command_submission import Allow, Clear, establish_baselines
except ImportError:  # pragma: no cover
    from tests.test_device_command_submission import Allow, Clear, establish_baselines

PACKAGE = Path(__file__).resolve().parents[1] / "tsn_dss" / "engine" / "device_runtime"
NOW = datetime(2026, 4, 1, 9, 0, 0, tzinfo=timezone.utc)
PROV, CONN, DEV = "prov", "conn-1", "dev-1"
PR, HO = TelemetrySource.PROVIDER_REPORTED, TelemetrySource.HOST_OBSERVED
MINUTE = timedelta(minutes=1)


def req(item="mode", allowed=("alpha", "beta"), age=MINUTE):
    return FreshnessRequirement(item, age, allowed)


def pol(*reqs):
    return CommandKindPolicy("act", state_changing=True, physical=True, idempotent=False, safety_sensitive=True, freshness=tuple(reqs))


class Established:
    def state_for(self, provider_id, device_ref):
        return UncertaintyState.RESOLVED_BY_RECOVERY_EVIDENCE


def sample(*items, age=1.0):
    return TelemetrySample(PROV, CONN, NOW - timedelta(seconds=age), tuple(items), True)


def item(name="mode", value="alpha", state=ValueState.KNOWN, source=PR):
    return TelemetryItem(name, source, state, value if state is ValueState.KNOWN else None)


def gate(policy, *items, age=1.0, view=Established()):
    return evaluate_gate(policy, EvidenceSnapshot(telemetry=sample(*items, age=age)), NOW,
                         provider_id=PROV, connection_id=CONN, device_ref=DEV, uncertainty=view)


def codes(result):
    return [r.code for r in result.reasons]


class RequirementTests(unittest.TestCase):
    def test_the_constraint_is_optional_and_off_by_default(self) -> None:
        plain = FreshnessRequirement("mode", MINUTE)
        self.assertIsNone(plain.allowed_values)
        self.assertEqual(FreshnessRequirement("mode", MINUTE), FreshnessRequirement("mode", MINUTE, None))

    def test_any_collection_is_stored_as_a_tuple(self) -> None:
        for allowed in (["a", "b"], ("a", "b"), {"a", "b"}, frozenset({"a", "b"})):
            stored = FreshnessRequirement("mode", MINUTE, allowed).allowed_values
            self.assertIsInstance(stored, tuple)
            self.assertEqual(set(stored), {"a", "b"})

    def test_scalars_of_every_supported_type_are_accepted_and_kept_distinct(self) -> None:
        stored = req(allowed=("a", 1, 1.0, True, False, 0)).allowed_values
        self.assertEqual(len(stored), 6)

    def test_malformed_constraints_are_refused(self) -> None:
        bad = ((), [], "abc", b"x", 5, None.__class__, ("a", None), ("a", float("nan")), ("a", ["b"]), ("a", {"b": 1}),
               ("a", "a"), (1, 1), (b"x",), ("a", object()))
        for allowed in bad:
            if allowed is None.__class__:
                continue
            with self.assertRaises(CommandPolicyError, msg=repr(allowed)):
                FreshnessRequirement("mode", MINUTE, allowed)  # type: ignore[arg-type]

    def test_a_capability_requirement_cannot_carry_values(self) -> None:
        with self.assertRaises(CommandPolicyError):
            FreshnessRequirement("capability:act", MINUTE, ("x",))
        FreshnessRequirement("capability:act", MINUTE)

    def test_the_requirement_is_immutable_and_still_has_no_default_age(self) -> None:
        with self.assertRaises(Exception):
            req().allowed_values = ("z",)  # type: ignore[misc]
        with self.assertRaises(TypeError):
            FreshnessRequirement("mode")  # type: ignore[call-arg]

    def test_matching_is_type_sensitive(self) -> None:
        self.assertTrue(value_allowed("alpha", ("alpha",)))
        self.assertFalse(value_allowed("Alpha", ("alpha",)))
        self.assertFalse(value_allowed(True, (1,)))
        self.assertFalse(value_allowed(1, (True,)))
        self.assertFalse(value_allowed(1, (1.0,)))
        self.assertFalse(value_allowed(0.0, (0,)))
        self.assertFalse(value_allowed("1", (1,)))
        self.assertFalse(value_allowed(0, (False,)))
        self.assertTrue(value_allowed(False, (False, 0)))
        self.assertTrue(value_allowed(0, (False, 0)))
        self.assertFalse(value_allowed(float("nan"), (1.0,)))


class GateValueTests(unittest.TestCase):
    def test_an_allowed_value_passes_and_a_disallowed_one_is_state_unsafe(self) -> None:
        policy = pol(req())
        for value in ("alpha", "beta"):
            self.assertTrue(gate(policy, item(value=value)).passed, value)
        result = gate(policy, item(value="gamma"))
        self.assertFalse(result.passed)
        self.assertEqual(codes(result), [R.STATE_UNSAFE])
        self.assertEqual((result.reasons[0].item, result.reasons[0].detail), ("mode", "provider_reported"))
        self.assertFalse(result.blocked_by_uncertainty)
        self.assertFalse(result.physical_truth_verified)

    def test_the_rejected_value_is_never_echoed(self) -> None:
        result = gate(pol(req()), item(value="secret-value-xyz"))
        self.assertNotIn("secret-value-xyz", result.summary())
        self.assertNotIn("secret-value-xyz", repr(result))

    def test_the_boundary_of_freshness_still_applies_to_the_value_check(self) -> None:
        policy = pol(req(age=timedelta(seconds=10)))
        self.assertTrue(gate(policy, item(value="alpha"), age=10.0).passed)
        self.assertEqual(codes(gate(policy, item(value="gamma"), age=10.0)), [R.STATE_UNSAFE])

    def test_without_a_constraint_the_value_is_irrelevant_exactly_as_before(self) -> None:
        plain = pol(FreshnessRequirement("mode", MINUTE))
        for value in ("alpha", "anything", 7, True, 0.5, ""):
            self.assertTrue(gate(plain, item(value=value)).passed, value)

    def test_type_sensitive_comparison_in_the_gate(self) -> None:
        for allowed, value, ok in (((True,), 1, False), ((1,), True, False), ((1,), 1.0, False), ((0.0,), 0, False),
                                   ((True,), True, True), ((0,), 0, True), (("1",), 1, False), ((False,), 0, False)):
            result = gate(pol(req(allowed=allowed)), item(value=value))
            self.assertEqual(result.passed, ok, (allowed, value))
            if not ok:
                self.assertEqual(codes(result), [R.STATE_UNSAFE])

    def test_non_fresh_or_non_known_evidence_keeps_its_own_reason_and_is_never_unsafe_or_accepted(self) -> None:
        policy = pol(req())
        self.assertEqual(codes(gate(policy, item(value="gamma"), age=99.0)), [R.EXPIRED])
        self.assertEqual(codes(gate(policy, item(value="alpha"), age=99.0)), [R.EXPIRED])
        self.assertEqual(codes(gate(policy, item(value="alpha"), age=-5.0)), [R.FUTURE_TIMESTAMP])
        stale = TelemetryItem("mode", PR, ValueState.STALE, "alpha")
        self.assertEqual(codes(gate(policy, stale)), [R.STATE_STALE])
        self.assertEqual(codes(gate(policy, item(state=ValueState.UNKNOWN))), [R.STATE_UNKNOWN])
        self.assertEqual(codes(gate(policy, item(state=ValueState.UNAVAILABLE))), [R.STATE_UNAVAILABLE])
        self.assertEqual(codes(gate(policy)), [R.MISSING])
        self.assertEqual(codes(evaluate_gate(policy, None, NOW, provider_id=PROV, connection_id=CONN, device_ref=DEV, uncertainty=Established())),
                         [R.EVIDENCE_UNAVAILABLE])

    def test_contradictory_readings(self) -> None:
        policy = pol(req())
        both_allowed = gate(policy, item(value="alpha", source=PR), item(value="beta", source=HO))
        self.assertEqual(codes(both_allowed), [R.CONTRADICTORY])
        mixed = gate(policy, item(value="alpha", source=PR), item(value="gamma", source=HO))
        self.assertEqual(sorted(c.value for c in codes(mixed)), sorted([R.CONTRADICTORY.value, R.STATE_UNSAFE.value]))
        agreeing_bad = gate(policy, item(value="gamma", source=PR), item(value="gamma", source=HO))
        self.assertEqual(codes(agreeing_bad), [R.STATE_UNSAFE, R.STATE_UNSAFE])
        self.assertTrue(gate(policy, item(value="alpha", source=PR), item(value="alpha", source=HO)).passed)

    def test_mixed_requirements_all_must_hold_and_every_reason_is_reported(self) -> None:
        policy = pol(req("mode"), req("level", allowed=(0, 1)), FreshnessRequirement("plain", MINUTE))
        ok = gate(policy, item("mode"), item("level", 1), item("plain", "whatever"))
        self.assertTrue(ok.passed)
        bad = gate(policy, item("mode", "gamma"), item("level", 1), item("plain", "whatever"))
        self.assertEqual([(r.code, r.item) for r in bad.reasons], [(R.STATE_UNSAFE, "mode")])
        worse = gate(policy, item("mode", "gamma"), item("level", 5), item("plain", state=ValueState.STALE, value="x"))
        self.assertEqual({(r.code, r.item) for r in worse.reasons},
                         {(R.STATE_UNSAFE, "mode"), (R.STATE_UNSAFE, "level"), (R.STATE_STALE, "plain")})
        missing = gate(policy, item("mode"), item("plain", "x"))
        self.assertEqual([(r.code, r.item) for r in missing.reasons], [(R.MISSING, "level")])

    def test_a_value_failure_does_not_hide_an_uncertainty_block_and_vice_versa(self) -> None:
        class Unresolved:
            def state_for(self, provider_id, device_ref):
                return UncertaintyState.UNRESOLVED

        result = gate(pol(req()), item(value="gamma"), view=Unresolved())
        self.assertEqual({c for c in codes(result)}, {R.STATE_UNSAFE, R.UNCERTAINTY_UNRESOLVED})
        self.assertTrue(result.blocked_by_uncertainty)
        self.assertFalse(gate(pol(req()), item(value="alpha"), view=Unresolved()).passed)

    def test_evidence_about_another_target_is_still_wrong_target(self) -> None:
        other = TelemetrySample("other", CONN, NOW - timedelta(seconds=1), (item(value="alpha"),), True)
        result = evaluate_gate(pol(req()), EvidenceSnapshot(telemetry=other), NOW, provider_id=PROV, connection_id=CONN,
                               device_ref=DEV, uncertainty=Established())
        self.assertIn(R.WRONG_TARGET, codes(result))

    def test_the_gate_stays_deterministic_and_pure(self) -> None:
        self.assertEqual(gate(pol(req()), item(value="gamma")), gate(pol(req()), item(value="gamma")))

    def test_a_kind_that_is_not_safety_sensitive_needs_no_values_either(self) -> None:
        plain = CommandKindPolicy("p", state_changing=True, physical=False, idempotent=False, safety_sensitive=False)
        self.assertTrue(evaluate_gate(plain, None, NOW, provider_id=PROV, connection_id=CONN, device_ref=DEV, uncertainty=None).passed)


class Rig:
    """Admission and submission with an evidence value that the test can change."""

    def __init__(self, allowed=("alpha", "beta"), value="alpha") -> None:
        self.value = value
        self.source_calls = 0
        self.provider = SimulatorCommandProvider(clock=ManualClock())
        self.runtime = ProviderRuntime(self.provider, clock=ManualClock(), id_generator=SequentialIdGenerator())
        self.device = self.runtime.discover().devices[0]
        self.clock = ManualClock()
        registry = CommandKindRegistry()
        registry.register(pol(req(allowed=allowed), FreshnessRequirement("plain", MINUTE)))
        registry.register(CommandKindPolicy("loose", state_changing=True, physical=True, idempotent=False, safety_sensitive=True,
                                            freshness=(FreshnessRequirement("mode", MINUTE),)))
        self.executor = CommandExecutor(
            self.runtime, registry, Allow(), clock=self.clock, id_generator=SequentialIdGenerator(),
            evidence_source=self.evidence, uncertainty=Clear(), command_provider=self.provider,
        )
        establish_baselines(self.executor, self.runtime, [self.device])

    def evidence(self, connection) -> EvidenceSnapshot:
        self.source_calls += 1
        at = self.clock() - timedelta(seconds=1)
        items = (TelemetryItem("mode", PR, ValueState.KNOWN, self.value), TelemetryItem("plain", PR, ValueState.KNOWN, "x"))
        return EvidenceSnapshot(TelemetrySample(self.runtime.provider_id, connection.connection_id, at, items, True))

    def ready(self):
        connection = self.runtime.connect(self.runtime.open_connection(self.device))
        self.runtime.refresh_evidence(connection)
        return connection

    def admit(self, connection, kind="act"):
        return self.executor.admit(CommandIntent(connection, kind, "op"))

    def submits(self):
        return [c for c in self.provider.calls if c[0] == "submit_command"]


class IntegrationTests(unittest.TestCase):
    def test_a_disallowed_value_is_safety_blocked_at_admission_and_releases_busy(self) -> None:
        rig = Rig(value="gamma")
        connection = rig.ready()
        record = rig.admit(connection)
        self.assertIs(record.state, S.SAFETY_BLOCKED)
        self.assertEqual(record.history[-1].event, CommandEvent.SAFETY_EVIDENCE_INSUFFICIENT.value)
        self.assertIn("state_unsafe:mode", record.history[-1].evidence)
        self.assertNotIn("gamma", record.history[-1].evidence)
        self.assertIs(connection.state, C.READY)
        self.assertIsNone(rig.executor.active_command(connection))
        self.assertEqual(rig.submits(), [])

    def test_an_allowed_value_is_admitted(self) -> None:
        rig = Rig()
        connection = rig.ready()
        self.assertIs(rig.admit(connection).state, S.VALIDATED)
        self.assertIs(connection.state, C.BUSY)

    def test_the_value_is_checked_again_immediately_before_submission(self) -> None:
        rig = Rig()
        connection = rig.ready()
        record = rig.admit(connection)
        self.assertIs(record.state, S.VALIDATED)
        rig.value = "gamma"  # the device changed between admission and submission
        calls = rig.source_calls
        rig.executor.submit(record.command_id)
        self.assertEqual(rig.source_calls, calls + 1)
        self.assertIs(record.state, S.SAFETY_BLOCKED)
        self.assertIn("state_unsafe:mode", record.history[-1].evidence)
        self.assertEqual(rig.submits(), [])
        self.assertIs(connection.state, C.READY)
        self.assertIsNone(rig.executor.active_command(connection))

    def test_an_allowed_value_at_both_points_reaches_the_provider_once(self) -> None:
        rig = Rig()
        record = rig.admit(rig.ready())
        rig.executor.submit(record.command_id)
        self.assertEqual(len(rig.submits()), 1)
        self.assertIs(record.state, S.ACKNOWLEDGED)
        self.assertEqual(rig.source_calls, 2)  # admission and submission

    def test_a_kind_without_a_value_constraint_is_unaffected(self) -> None:
        rig = Rig(value="gamma")
        record = rig.admit(rig.ready(), "loose")
        self.assertIs(record.state, S.VALIDATED)

    def test_a_blocked_value_leaves_the_slot_free_for_the_next_command(self) -> None:
        rig = Rig(value="gamma")
        connection = rig.ready()
        rig.admit(connection)
        rig.value = "alpha"
        self.assertIs(rig.admit(connection).state, S.VALIDATED)


class RecoveryIgnoresValueConstraintsTests(unittest.TestCase):
    """Recovery judges the state after an uncertain Command; the kind's pre-command value constraints must not apply to it."""

    def uncertain(self):
        from tsn_dss.engine.device_runtime.simulator import CommandScript
        from tsn_dss.engine.device_runtime.uncertainty import RecoveryVerdict

        rig = Rig()
        connection = rig.ready()
        rig.provider.script_next_command(CommandScript(submit="transport_loss"))
        record = rig.admit(connection)
        rig.executor.submit(record.command_id)
        assert record.state is S.UNKNOWN_RESULT
        rig.executor._assessors = {"act": lambda c, r, snap, now: RecoveryVerdict(True, "observed", (("mode", "provider_reported"),))}
        return rig, connection, record

    def test_an_observed_value_outside_the_pre_command_constraint_does_not_block_recovery_evidence(self) -> None:
        rig, connection, record = self.uncertain()
        rig.value = "gamma"  # not allowed before running; legitimate as an observed state afterwards
        entry = rig.executor.resolve_uncertainty_by_recovery(connection, record.command_id)
        self.assertFalse(entry.unresolved)
        self.assertIs(record.state, S.UNKNOWN_RESULT)

    def test_freshness_and_availability_still_apply_to_recovery_evidence(self) -> None:
        from tsn_dss.engine.device_runtime.command_executor import RecoveryNotEstablished

        rig, connection, record = self.uncertain()
        original = rig.evidence

        def stale(c):
            snapshot = original(c)
            t = snapshot.telemetry
            return EvidenceSnapshot(TelemetrySample(t.provider_id, t.connection_id, t.host_observed_at - timedelta(hours=1), t.items, True))

        rig.executor._evidence_source = stale
        with self.assertRaises(RecoveryNotEstablished) as ctx:
            rig.executor.resolve_uncertainty_by_recovery(connection, record.command_id)
        self.assertIn("expired", str(ctx.exception))


class BoundaryTests(unittest.TestCase):
    def test_the_neutral_runtime_names_no_device_states_and_adds_no_defaults(self) -> None:
        for name in ("command_models.py", "safety_gates.py", "command_executor.py"):
            text = (PACKAGE / name).read_text(encoding="utf-8")
            self.assertNotRegex(text.lower(), r"seestar|scenery|rtsp|horizon|\bpark\b|move_type|\bmount\b|\bcamera\b|arm_closed")
        self.assertIsNone(FreshnessRequirement.__dataclass_fields__["allowed_values"].default)
        self.assertNotIn("allowed_values=(", (PACKAGE / "command_models.py").read_text(encoding="utf-8"))

    def test_the_executor_public_surface_is_unchanged(self) -> None:
        public = {n for n in dir(CommandExecutor) if not n.startswith("_")}
        self.assertEqual(public, {"admit", "check_deadline", "commands", "get", "active_command", "unresolved_devices",
                                  "uncertainty_store", "resolve_uncertainty_by_recovery", "clear_uncertainty_by_operator",
                                  "establish_baseline_by_recovery", "establish_baseline_by_operator", "submit", "poll",
                                  "cancel", "enforce_deadline"})

    def test_the_package_exports_nothing_new(self) -> None:
        import tsn_dss.engine.device_runtime as pkg

        for name in ("FreshnessRequirement", "value_allowed", "GateReasonCode", "evaluate_gate"):
            self.assertNotIn(name, pkg.__all__)


if __name__ == "__main__":
    unittest.main()
