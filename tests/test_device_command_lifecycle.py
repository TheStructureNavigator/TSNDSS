"""DB-04 S1: Command kind policy, registry and the pure Command lifecycle table (DSS-CTR-013 section 9)."""

from __future__ import annotations

import itertools
import unittest
from datetime import datetime, timedelta, timezone

from tsn_dss.engine.device_runtime.command_lifecycle import (
    COMMAND_TRANSITIONS,
    CommandEvent as E,
    CommandRecord,
    command_next,
)
from tsn_dss.engine.device_runtime.command_models import (
    CommandKindAlreadyRegistered,
    CommandKindPolicy,
    CommandKindRegistry,
    CommandPolicyError,
    CommandRequest,
    FreshnessRequirement,
    UnknownCommandKind,
)
from tsn_dss.engine.device_runtime.errors import InvalidTransition
from tsn_dss.engine.device_runtime.models import COMMAND_TERMINAL_STATES, CommandRef, CommandState as S

AT = datetime(2026, 1, 1, tzinfo=timezone.utc)
MINUTE = timedelta(minutes=1)


def policy(**kw) -> CommandKindPolicy:
    base = dict(kind_id="k", state_changing=True, physical=False, idempotent=False, safety_sensitive=False)
    base.update(kw)
    return CommandKindPolicy(**base)


PHYSICAL = policy(
    kind_id="p", physical=True, safety_sensitive=True, freshness=(FreshnessRequirement("state.x", MINUTE),)
)
NON_PHYSICAL = policy(kind_id="n")

# Independent restatement of the contract section 9 table ("rejected or safety_blocked" expanded
# into its two alternatives; shared rows expanded per starting state).
PRE = (S.REQUESTED, S.VALIDATED)
LIVE = (S.SUBMITTED, S.ACKNOWLEDGED, S.IN_PROGRESS)
EXPECTED = {
    (S.REQUESTED, E.REQUEST_INVALID_OR_UNSUPPORTED): S.REJECTED,
    (S.REQUESTED, E.CONFLICT_REJECTED): S.REJECTED,
    (S.REQUESTED, E.CONFLICT_SAFETY_BLOCKED): S.SAFETY_BLOCKED,
    (S.REQUESTED, E.REQUEST_VALID): S.VALIDATED,
    (S.VALIDATED, E.SAFETY_EVIDENCE_INSUFFICIENT): S.SAFETY_BLOCKED,
    (S.VALIDATED, E.UNCERTAINTY_NOT_CLEARED): S.SAFETY_BLOCKED,
    (S.VALIDATED, E.GATES_PASSED): S.SUBMITTED,
    (S.SUBMITTED, E.PROVIDER_REJECTED_NO_EFFECT): S.FAILED,
    (S.SUBMITTED, E.PROVIDER_REJECTED_EFFECT_POSSIBLE): S.UNKNOWN_RESULT,
    (S.SUBMITTED, E.PROVIDER_ACKNOWLEDGED): S.ACKNOWLEDGED,
    (S.SUBMITTED, E.DEADLINE_NO_EFFECT_PROVEN): S.TIMED_OUT,
    (S.ACKNOWLEDGED, E.MONITORING_STARTED): S.IN_PROGRESS,
    (S.ACKNOWLEDGED, E.ACKNOWLEDGEMENT_IS_VERIFIED_EFFECT): S.SUCCEEDED,
}
for _s in PRE:
    EXPECTED[(_s, E.DEADLINE_BEFORE_SUBMISSION)] = S.TIMED_OUT
for _s in (S.ACKNOWLEDGED, S.IN_PROGRESS):
    EXPECTED[(_s, E.EFFECT_VERIFIED)] = S.SUCCEEDED
    EXPECTED[(_s, E.EFFECT_FAILED)] = S.FAILED
    EXPECTED[(_s, E.PROVIDER_FAILURE_EFFECT_POSSIBLE)] = S.UNKNOWN_RESULT
for _s in LIVE:
    EXPECTED[(_s, E.DEADLINE_EFFECT_UNDETERMINED)] = S.UNKNOWN_RESULT
    EXPECTED[(_s, E.DEADLINE_NON_PHYSICAL)] = S.TIMED_OUT
    EXPECTED[(_s, E.TRANSPORT_LOST_EFFECT_UNKNOWN)] = S.UNKNOWN_RESULT
    EXPECTED[(_s, E.CANCEL_ACCEPTED_NO_EFFECT)] = S.CANCELLED
    EXPECTED[(_s, E.CANCEL_RACE_UNDETERMINED)] = S.UNKNOWN_RESULT
NON_PHYSICAL_ONLY = {
    (S.ACKNOWLEDGED, E.ACKNOWLEDGEMENT_IS_VERIFIED_EFFECT),
    *((s, E.DEADLINE_NON_PHYSICAL) for s in LIVE),
}


def record(kind: CommandKindPolicy = NON_PHYSICAL, path: tuple[E, ...] = ()) -> CommandRecord:
    rec = CommandRecord(command_id="c1", provider_id="prov", connection_id="conn", policy=kind)
    for event in path:
        rec.apply(event, AT, "evidence")
    return rec


class TransitionTableTests(unittest.TestCase):
    def test_table_equals_the_contract_rows_exactly(self) -> None:
        self.assertEqual({k: v.next_state for k, v in COMMAND_TRANSITIONS.items()}, EXPECTED)
        self.assertEqual(len(EXPECTED), 36)

    def test_every_contract_row_is_reachable_with_the_right_outcome(self) -> None:
        for (state, event), nxt in EXPECTED.items():
            kind = NON_PHYSICAL if (state, event) in NON_PHYSICAL_ONLY else PHYSICAL
            self.assertIs(command_next(state, event, kind).next_state, nxt, (state, event))

    def test_every_other_state_event_pair_is_illegal(self) -> None:
        for state, event in itertools.product(S, E):
            if (state, event) in EXPECTED:
                continue
            with self.assertRaises(InvalidTransition, msg=(state, event)):
                command_next(state, event, NON_PHYSICAL)

    def test_every_row_names_its_required_evidence(self) -> None:
        for key, row in COMMAND_TRANSITIONS.items():
            self.assertTrue(row.required_evidence.strip(), key)

    def test_all_contract_states_exist_and_nothing_else(self) -> None:
        self.assertEqual(
            {s.value for s in S},
            {"requested", "validated", "rejected", "safety_blocked", "submitted", "acknowledged", "in_progress",
             "succeeded", "failed", "timed_out", "cancelled", "unknown_result"},
        )


class TerminalStateTests(unittest.TestCase):
    def test_terminal_states_match_the_contract(self) -> None:
        self.assertEqual(
            COMMAND_TERMINAL_STATES,
            {S.REJECTED, S.SAFETY_BLOCKED, S.SUCCEEDED, S.FAILED, S.TIMED_OUT, S.CANCELLED, S.UNKNOWN_RESULT},
        )

    def test_terminal_states_have_no_outgoing_transition_and_nonterminal_states_have_one(self) -> None:
        sources = {state for state, _ in COMMAND_TRANSITIONS}
        for state in S:
            self.assertEqual(state in sources, not state.is_terminal, state)

    def test_no_transition_reaches_a_terminal_state_via_an_alternative_chain(self) -> None:
        """REQ-023: rejected and safety_blocked are outcomes, never steps toward execution."""
        for (state, _), row in COMMAND_TRANSITIONS.items():
            if row.next_state in (S.REJECTED, S.SAFETY_BLOCKED):
                self.assertIn(state, PRE)
        self.assertNotIn(S.SUBMITTED, {r.next_state for (s, _), r in COMMAND_TRANSITIONS.items() if s is S.REQUESTED})

    def test_submission_is_reachable_only_from_validated(self) -> None:
        sources = {s for (s, _), r in COMMAND_TRANSITIONS.items() if r.next_state is S.SUBMITTED}
        self.assertEqual(sources, {S.VALIDATED})


class AcknowledgementIsNotSuccessTests(unittest.TestCase):
    def test_acknowledgement_reaches_acknowledged_only(self) -> None:
        """REQ-024."""
        rec = record(PHYSICAL, (E.REQUEST_VALID, E.GATES_PASSED, E.PROVIDER_ACKNOWLEDGED))
        self.assertIs(rec.state, S.ACKNOWLEDGED)
        self.assertFalse(rec.state.is_terminal)

    def test_success_needs_a_verification_event(self) -> None:
        succeeding = {k for k, v in COMMAND_TRANSITIONS.items() if v.next_state is S.SUCCEEDED}
        self.assertEqual({e for _, e in succeeding}, {E.EFFECT_VERIFIED, E.ACKNOWLEDGEMENT_IS_VERIFIED_EFFECT})
        self.assertNotIn(S.SUCCEEDED, {v.next_state for (_, e), v in COMMAND_TRANSITIONS.items() if e is E.PROVIDER_ACKNOWLEDGED})
        self.assertTrue(all(s in (S.ACKNOWLEDGED, S.IN_PROGRESS) for s, _ in succeeding))

    def test_success_cannot_follow_submission_directly(self) -> None:
        for event in (E.EFFECT_VERIFIED, E.ACKNOWLEDGEMENT_IS_VERIFIED_EFFECT):
            with self.assertRaises(InvalidTransition):
                command_next(S.SUBMITTED, event, NON_PHYSICAL)

    def test_a_physical_kind_cannot_succeed_on_acknowledgement_alone(self) -> None:
        for kind in (PHYSICAL, None):
            with self.assertRaises(InvalidTransition):
                command_next(S.ACKNOWLEDGED, E.ACKNOWLEDGEMENT_IS_VERIFIED_EFFECT, kind)
        self.assertIs(
            command_next(S.ACKNOWLEDGED, E.ACKNOWLEDGEMENT_IS_VERIFIED_EFFECT, NON_PHYSICAL).next_state, S.SUCCEEDED
        )
        rec = record(PHYSICAL, (E.REQUEST_VALID, E.GATES_PASSED, E.PROVIDER_ACKNOWLEDGED))
        with self.assertRaises(InvalidTransition):
            rec.apply(E.ACKNOWLEDGEMENT_IS_VERIFIED_EFFECT, AT, "e")
        self.assertIs(rec.state, S.ACKNOWLEDGED)

    def test_verified_success_follows_monitoring(self) -> None:
        rec = record(PHYSICAL, (E.REQUEST_VALID, E.GATES_PASSED, E.PROVIDER_ACKNOWLEDGED, E.MONITORING_STARTED, E.EFFECT_VERIFIED))
        self.assertIs(rec.state, S.SUCCEEDED)


class TimeoutAndUncertaintyTests(unittest.TestCase):
    def test_deadline_alone_has_no_event_and_never_means_failure_or_success(self) -> None:
        """REQ-025, REQ-045: every deadline event names its evidence class; none yields failed or succeeded."""
        deadline_events = [e for e in E if "deadline" in e.value]
        self.assertTrue(deadline_events)
        for (state, event), row in COMMAND_TRANSITIONS.items():
            if event in deadline_events:
                self.assertIn(row.next_state, (S.TIMED_OUT, S.UNKNOWN_RESULT))

    def test_physical_kind_deadline_after_possible_submission_is_unknown_result(self) -> None:
        """REQ-046."""
        for state in LIVE:
            self.assertIs(command_next(state, E.DEADLINE_EFFECT_UNDETERMINED, PHYSICAL).next_state, S.UNKNOWN_RESULT)
            with self.assertRaises(InvalidTransition):
                command_next(state, E.DEADLINE_NON_PHYSICAL, PHYSICAL)

    def test_no_effect_proof_timeout_is_possible_only_from_submitted(self) -> None:
        for state in (S.ACKNOWLEDGED, S.IN_PROGRESS):
            with self.assertRaises(InvalidTransition):
                command_next(state, E.DEADLINE_NO_EFFECT_PROVEN, PHYSICAL)

    def test_deadline_before_submission_is_timed_out_from_requested_and_validated_only(self) -> None:
        for state in S:
            if state in PRE:
                self.assertIs(command_next(state, E.DEADLINE_BEFORE_SUBMISSION).next_state, S.TIMED_OUT)
            else:
                with self.assertRaises(InvalidTransition):
                    command_next(state, E.DEADLINE_BEFORE_SUBMISSION)

    def test_transport_loss_and_cancel_race_preserve_uncertainty(self) -> None:
        """REQ-026."""
        for state in LIVE:
            self.assertIs(command_next(state, E.TRANSPORT_LOST_EFFECT_UNKNOWN).next_state, S.UNKNOWN_RESULT)
            self.assertIs(command_next(state, E.CANCEL_RACE_UNDETERMINED).next_state, S.UNKNOWN_RESULT)
        for state in PRE:
            with self.assertRaises(InvalidTransition):
                command_next(state, E.TRANSPORT_LOST_EFFECT_UNKNOWN)

    def test_provider_rejection_with_possible_effect_is_unknown_result(self) -> None:
        self.assertIs(command_next(S.SUBMITTED, E.PROVIDER_REJECTED_EFFECT_POSSIBLE).next_state, S.UNKNOWN_RESULT)
        self.assertIs(command_next(S.SUBMITTED, E.PROVIDER_REJECTED_NO_EFFECT).next_state, S.FAILED)

    def test_nothing_leaves_unknown_result(self) -> None:
        """Later classification never erases an uncertain outcome."""
        self.assertFalse([k for k in COMMAND_TRANSITIONS if k[0] is S.UNKNOWN_RESULT])


class CommandRecordTests(unittest.TestCase):
    def test_history_and_binding(self) -> None:
        rec = record(NON_PHYSICAL, (E.REQUEST_VALID, E.GATES_PASSED))
        self.assertEqual((rec.command_id, rec.provider_id, rec.connection_id), ("c1", "prov", "conn"))
        self.assertEqual([(h.from_state, h.event, h.to_state) for h in rec.history],
                         [("requested", "request_valid", "validated"), ("validated", "gates_passed", "submitted")])

    def test_evidence_is_mandatory_and_a_failed_apply_changes_nothing(self) -> None:
        rec = record()
        for bad in ("", "  ", None):
            with self.assertRaises(InvalidTransition):
                rec.apply(E.REQUEST_VALID, AT, bad)
        self.assertIs(rec.state, S.REQUESTED)
        self.assertEqual(rec.history, ())

    def test_terminal_record_is_never_reactivated(self) -> None:
        rec = record(NON_PHYSICAL, (E.REQUEST_INVALID_OR_UNSUPPORTED,))
        for event in E:
            with self.assertRaises(InvalidTransition):
                rec.apply(event, AT, "e")
        self.assertIs(rec.state, S.REJECTED)

    def test_history_is_a_copy(self) -> None:
        rec = record(NON_PHYSICAL, (E.REQUEST_VALID,))
        self.assertIsInstance(rec.history, tuple)


class PolicyTests(unittest.TestCase):
    def test_physical_kind_must_be_state_changing_and_safety_sensitive(self) -> None:
        with self.assertRaises(CommandPolicyError):
            policy(physical=True, safety_sensitive=False)
        with self.assertRaises(CommandPolicyError):
            policy(physical=True, state_changing=False, safety_sensitive=True, freshness=(FreshnessRequirement("a", MINUTE),))

    def test_safety_sensitive_kind_needs_freshness_requirements(self) -> None:
        """REQ-051, fail closed."""
        with self.assertRaises(CommandPolicyError):
            policy(safety_sensitive=True)

    def test_physical_kind_is_never_idempotent(self) -> None:
        """REQ-027."""
        with self.assertRaises(CommandPolicyError):
            policy(physical=True, safety_sensitive=True, idempotent=True,
                   freshness=(FreshnessRequirement("a", MINUTE),))

    def test_freshness_has_no_default_and_must_be_positive(self) -> None:
        with self.assertRaises(TypeError):
            FreshnessRequirement("a")  # type: ignore[call-arg]
        for bad in (timedelta(0), timedelta(seconds=-1), 5, None):
            with self.assertRaises(CommandPolicyError):
                FreshnessRequirement("a", bad)  # type: ignore[arg-type]
        for bad in ("", " "):
            with self.assertRaises(CommandPolicyError):
                FreshnessRequirement(bad, MINUTE)

    def test_duplicate_freshness_items_and_bad_fields_are_refused(self) -> None:
        with self.assertRaises(CommandPolicyError):
            policy(safety_sensitive=True, freshness=(FreshnessRequirement("a", MINUTE), FreshnessRequirement("a", MINUTE)))
        with self.assertRaises(CommandPolicyError):
            policy(kind_id=" ")
        with self.assertRaises(CommandPolicyError):
            policy(physical=1)  # type: ignore[arg-type]
        with self.assertRaises(CommandPolicyError):
            policy(freshness=("a",))  # type: ignore[arg-type]

    def test_no_freshness_default_is_embedded_in_the_policy_modules(self) -> None:
        import inspect
        from tsn_dss.engine.device_runtime import command_models

        self.assertNotIn("timedelta(", inspect.getsource(command_models).replace("timedelta(0)", ""))
        self.assertEqual(policy().freshness, ())

    def test_policy_and_requirement_are_immutable(self) -> None:
        with self.assertRaises(Exception):
            NON_PHYSICAL.physical = True  # type: ignore[misc]


class RequestTests(unittest.TestCase):
    REF = CommandRef("c1", "prov", "conn")

    def req(self, **kw) -> CommandRequest:
        base = dict(ref=self.REF, kind_id="n", requested_by="operator", requested_at=AT)
        base.update(kw)
        return CommandRequest(**base)

    def test_request_binds_provider_and_connection_and_requires_a_caller(self) -> None:
        """REQ-021, REQ-044."""
        request = self.req()
        self.assertEqual((request.ref.provider_id, request.ref.connection_id), ("prov", "conn"))
        for bad in ("", " ", None):
            with self.assertRaises(CommandPolicyError):
                self.req(requested_by=bad)

    def test_timestamps_must_be_aware(self) -> None:
        with self.assertRaises(CommandPolicyError):
            self.req(requested_at=datetime(2026, 1, 1))
        with self.assertRaises(CommandPolicyError):
            self.req(deadline=datetime(2026, 1, 1))
        self.assertIsNone(self.req().deadline)

    def test_idempotency_key_only_for_idempotent_kinds(self) -> None:
        idem = policy(kind_id="n", idempotent=True)
        self.assertIsNone(self.req(idempotency_key="k").violates_policy(idem))
        self.assertEqual(self.req(idempotency_key="k").violates_policy(NON_PHYSICAL), "idempotency_key_on_non_idempotent_kind")
        self.assertEqual(self.req(kind_id="other").violates_policy(NON_PHYSICAL), "kind_mismatch")
        self.assertIsNone(self.req().violates_policy(NON_PHYSICAL))
        with self.assertRaises(CommandPolicyError):
            self.req(idempotency_key=" ")


class RegistryTests(unittest.TestCase):
    def test_register_get_and_duplicate(self) -> None:
        registry = CommandKindRegistry()
        self.assertEqual(registry.kind_ids(), ())
        registry.register(NON_PHYSICAL)
        registry.register(PHYSICAL)
        self.assertIs(registry.get("n"), NON_PHYSICAL)
        self.assertEqual(registry.kind_ids(), ("n", "p"))
        with self.assertRaises(CommandKindAlreadyRegistered):
            registry.register(NON_PHYSICAL)
        self.assertIs(registry.get("n"), NON_PHYSICAL)

    def test_unknown_kind_is_refused_and_the_core_names_no_kind(self) -> None:
        registry = CommandKindRegistry()
        with self.assertRaises(UnknownCommandKind):
            registry.get("anything")
        with self.assertRaises(CommandPolicyError):
            registry.register("not a policy")  # type: ignore[arg-type]

    def test_registries_are_independent(self) -> None:
        a, b = CommandKindRegistry(), CommandKindRegistry()
        a.register(NON_PHYSICAL)
        self.assertEqual(b.kind_ids(), ())


class IndependenceTests(unittest.TestCase):
    def test_command_events_do_not_collide_with_connection_events(self) -> None:
        """REQ-011, REQ-012."""
        from tsn_dss.engine.device_runtime.lifecycle import ConnectionEvent, ProviderEvent

        self.assertTrue({e.value for e in E}.isdisjoint({e.value for e in ConnectionEvent} | {e.value for e in ProviderEvent}))

    def test_s1_exposes_no_public_executable_api(self) -> None:
        import tsn_dss.engine.device_runtime as pkg

        s1_names = {"CommandRecord", "CommandEvent", "CommandKindPolicy", "CommandKindRegistry", "CommandRequest",
                    "FreshnessRequirement", "command_next", "COMMAND_TRANSITIONS"}
        self.assertTrue(s1_names.isdisjoint(pkg.__all__))
        self.assertFalse([n for n in pkg.__all__ if "execut" in n.lower() or "submit" in n.lower()])


if __name__ == "__main__":
    unittest.main()
