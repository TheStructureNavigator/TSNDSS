"""DB-04 S5: unresolved physical uncertainty store, recovery, operator clearance and restart safety."""

from __future__ import annotations

import re
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tsn_dss.engine.device_runtime import ManualClock, ProviderRuntime, SequentialIdGenerator, SimulatorProvider
from tsn_dss.engine.device_runtime.command_executor import (
    ClearanceNotAuthorized,
    CommandExecutor,
    CommandIntent,
    RecoveryNotEstablished,
)
from tsn_dss.engine.device_runtime.command_lifecycle import CommandEvent
from tsn_dss.engine.device_runtime.command_models import CommandKindRegistry
from tsn_dss.engine.device_runtime.errors import InvalidTransition
from tsn_dss.engine.device_runtime.models import CommandState as S, ConnectionState as C
from tsn_dss.engine.device_runtime.safety_gates import UncertaintyState as U
from tsn_dss.engine.device_runtime.simulator import SimulatedDeviceSpec
from tsn_dss.engine.device_runtime.uncertainty import (
    RecoveryVerdict,
    Resolution,
    ResolutionKind as K,
    UncertaintyAlreadyResolved,
    UncertaintyStore,
    UnknownUncertainty,
)

try:
    from test_device_command_submission import Allow, Clear, MOVE, Rig
except ImportError:  # pragma: no cover
    from tests.test_device_command_submission import Allow, Clear, MOVE, Rig

PACKAGE = Path(__file__).resolve().parents[1] / "tsn_dss" / "engine" / "device_runtime"
T0 = datetime(2026, 5, 1, tzinfo=timezone.utc)
BASIS = (("pose", "provider_reported"),)


def recovered(connection, record, snapshot, now):
    return RecoveryVerdict(True, "pose read back at the parked position", BASIS)


class Op:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.asked = []

    def may_clear(self, operator_id, provider_id, device_ref):
        self.asked.append((operator_id, provider_id, device_ref))
        return self.allowed


def uncertain_rig(**extra):
    """A Rig whose device carries an unresolved physical uncertainty (transport lost while submitting)."""
    rig = Rig(store=UncertaintyStore(), **extra)
    connection, record = rig.started("move", submit="transport_loss")
    assert record.state is S.UNKNOWN_RESULT
    return rig, connection, record


class StoreTests(unittest.TestCase):
    def open(self, store, provider="p", device="d", command="c1"):
        return store.open_entry(provider, device, command, "move", T0, "conn-1")

    def test_a_new_store_is_empty_and_says_nothing_about_safety(self) -> None:
        store = UncertaintyStore()
        self.assertIs(store.state_for("p", "d"), U.UNKNOWN)  # not verified absence of uncertainty
        self.assertFalse(store.history_established("p", "d"))
        self.assertEqual(store.unresolved_devices(), frozenset())

    def test_entries_are_keyed_by_provider_and_device_and_isolated(self) -> None:
        store = UncertaintyStore()
        self.open(store, "p1", "d1")
        self.assertIs(store.state_for("p1", "d1"), U.UNRESOLVED)
        for other in (("p1", "d2"), ("p2", "d1")):
            self.assertIs(store.state_for(*other), U.UNKNOWN, other)
        self.assertEqual(store.unresolved_devices(), {("p1", "d1")})

    def test_opening_the_same_command_twice_returns_the_same_entry(self) -> None:
        store = UncertaintyStore()
        a = self.open(store)
        self.assertIs(self.open(store), a)
        self.assertEqual(len(store.entries("p", "d")), 1)
        self.assertEqual((a.original_outcome, a.unresolved), ("unknown_result", True))

    def test_unusable_identities_fail_closed(self) -> None:
        store = UncertaintyStore()
        for bad in (("", "d"), ("p", ""), (" ", "d"), (None, "d"), ("p", None)):
            self.assertIs(store.state_for(*bad), U.UNKNOWN, bad)

    def test_any_open_entry_keeps_the_device_unresolved_and_the_latest_resolution_labels_the_rest(self) -> None:
        store = UncertaintyStore()
        self.open(store, command="c1")
        self.open(store, command="c2")
        store.resolve("p", "d", "c1", Resolution(K.RECOVERY_EVIDENCE, T0, "e", basis=BASIS))
        self.assertIs(store.state_for("p", "d"), U.UNRESOLVED)
        store.resolve("p", "d", "c2", Resolution(K.OPERATOR_CLEARANCE, T0 + timedelta(seconds=1), "r", resolved_by="op"))
        self.assertIs(store.state_for("p", "d"), U.CLEARED_BY_OPERATOR)
        store2 = UncertaintyStore()
        self.open(store2, command="c1")
        self.open(store2, command="c2")
        store2.resolve("p", "d", "c1", Resolution(K.OPERATOR_CLEARANCE, T0, "r", resolved_by="op"))
        store2.resolve("p", "d", "c2", Resolution(K.RECOVERY_EVIDENCE, T0 + timedelta(seconds=1), "e", basis=BASIS))
        self.assertIs(store2.state_for("p", "d"), U.RESOLVED_BY_RECOVERY_EVIDENCE)

    def test_a_resolution_is_never_replaced_and_unknown_entries_are_refused(self) -> None:
        store = UncertaintyStore()
        self.open(store)
        store.resolve("p", "d", "c1", Resolution(K.OPERATOR_CLEARANCE, T0, "r", resolved_by="op"))
        with self.assertRaises(UncertaintyAlreadyResolved):
            store.resolve("p", "d", "c1", Resolution(K.RECOVERY_EVIDENCE, T0, "e", basis=BASIS))
        self.assertIs(store.get("p", "d", "c1").resolution.kind, K.OPERATOR_CLEARANCE)
        for args in (("p", "d", "nope"), ("p", "x", "c1"), ("q", "d", "c1")):
            with self.assertRaises(UnknownUncertainty):
                store.resolve(*args, Resolution(K.OPERATOR_CLEARANCE, T0, "r", resolved_by="op"))
            with self.assertRaises(UnknownUncertainty):
                store.get(*args)

    def test_resolution_records_must_say_who_and_on_what_basis(self) -> None:
        with self.assertRaises(ValueError):
            Resolution(K.OPERATOR_CLEARANCE, T0, "reason")
        with self.assertRaises(ValueError):
            Resolution(K.OPERATOR_CLEARANCE, T0, "reason", resolved_by=" ")
        with self.assertRaises(ValueError):
            Resolution(K.RECOVERY_EVIDENCE, T0, "evidence")
        with self.assertRaises(ValueError):
            Resolution(K.RECOVERY_EVIDENCE, T0, " ", basis=BASIS)

    def test_no_resolution_ever_proves_a_physical_effect(self) -> None:
        for resolution in (Resolution(K.OPERATOR_CLEARANCE, T0, "r", resolved_by="op"),
                           Resolution(K.RECOVERY_EVIDENCE, T0, "e", basis=BASIS)):
            self.assertFalse(resolution.proves_physical_effect)

    def test_entries_are_immutable(self) -> None:
        entry = self.open(UncertaintyStore())
        with self.assertRaises(Exception):
            entry.resolution = None  # type: ignore[misc]


class OpeningTests(unittest.TestCase):
    def test_an_uncertain_physical_command_opens_exactly_one_entry(self) -> None:
        rig, connection, record = uncertain_rig()
        entries = rig.executor.uncertainty_store.entries(rig.runtime.provider_id, connection.device.device_ref)
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual((entry.command_id, entry.kind_id, entry.origin_connection_id, entry.unresolved),
                         (record.command_id, "move", connection.connection_id, True))
        self.assertEqual(rig.executor.unresolved_devices(), {(rig.runtime.provider_id, connection.device.device_ref)})

    def test_other_outcomes_open_nothing(self) -> None:
        for kind, script in (("config", dict(submit="transport_loss")), ("idem", dict(submit="transport_loss")),
                             ("move", dict(submit="reject_no_effect"))):
            rig = Rig(store=UncertaintyStore())
            rig.started(kind, **script)
            self.assertEqual(rig.executor.unresolved_devices(), frozenset(), (kind, script))

    def test_the_explicit_store_is_the_executors_store(self) -> None:
        store = UncertaintyStore()
        rig = Rig(store=store)
        self.assertIs(rig.executor.uncertainty_store, store)


class BlockingTests(unittest.TestCase):
    def test_uncertainty_survives_disconnect_and_reconnect_and_blocks_whatever_the_connection_state(self) -> None:
        rig, connection, record = uncertain_rig()
        again = rig.ready()
        self.assertIsNot(again.connection_id, connection.connection_id)
        self.assertIs(again.state, C.READY)  # ready is not safety
        blocked = rig.admit(again, "move")
        self.assertIs(blocked.state, S.SAFETY_BLOCKED)
        self.assertEqual(blocked.history[-1].event, CommandEvent.UNCERTAINTY_NOT_CLEARED.value)
        self.assertIs(again.state, C.READY)
        self.assertEqual(len(rig.submits()), 1)

    def test_the_unresolved_check_withholds_ready_from_a_degraded_connection(self) -> None:
        rig, connection, record = uncertain_rig()
        self.assertIs(connection.state, C.DEGRADED)
        self.assertIs(rig.runtime.refresh_evidence(connection), C.CONNECTED)
        rig.runtime.read_telemetry(connection)  # reads keep working
        rig.runtime.describe_preview(connection)

    def test_a_degraded_connection_returns_to_ready_once_the_uncertainty_is_resolved(self) -> None:
        rig, connection, record = uncertain_rig(clearance_authorizer=Op())
        rig.runtime.report_transport_loss(connection, C.DEGRADED) if connection.state is not C.DEGRADED else None
        rig.executor.clear_uncertainty_by_operator(connection.device.device_ref, record.command_id, "op", "inspected")
        self.assertIs(rig.runtime.refresh_evidence(connection), C.READY)

    def test_a_store_alone_cannot_pass_a_missing_configured_view(self) -> None:
        rig = Rig(with_view=False)
        self.assertIs(rig.admit(rig.ready(), "move").state, S.SAFETY_BLOCKED)

    def test_a_configured_view_that_says_unresolved_still_blocks_with_an_empty_store(self) -> None:
        class Stuck:
            def state_for(self, provider_id, device_ref):
                return U.UNRESOLVED

        rig = Rig(store=None)
        rig.executor._uncertainty = Stuck()
        self.assertIs(rig.admit(rig.ready(), "move").state, S.SAFETY_BLOCKED)

    def test_a_configured_view_still_blocks_after_the_store_resolved_its_own_entry(self) -> None:
        class Stuck:
            def state_for(self, provider_id, device_ref):
                return U.UNRESOLVED

        rig, connection, record = uncertain_rig(clearance_authorizer=Op())
        rig.executor.clear_uncertainty_by_operator(connection.device.device_ref, record.command_id, "alice", "r")
        self.assertIs(rig.admit(rig.ready(), "move").state, S.VALIDATED)
        rig2, c2, r2 = uncertain_rig(clearance_authorizer=Op())
        rig2.executor._uncertainty = Stuck()
        rig2.executor.clear_uncertainty_by_operator(c2.device.device_ref, r2.command_id, "alice", "r")
        self.assertIs(rig2.admit(rig2.ready(), "move").state, S.SAFETY_BLOCKED)

    def test_the_store_blocks_even_when_the_configured_view_says_none(self) -> None:
        rig = Rig()  # configured view: Clear()
        connection, record = rig.started("move", submit="transport_loss")
        self.assertIs(rig.admit(rig.ready(), "move").state, S.SAFETY_BLOCKED)
        self.assertEqual(len(rig.submits()), 1)

    def test_isolation_across_devices(self) -> None:
        rig = Rig(devices=(SimulatedDeviceSpec("sim-a"), SimulatedDeviceSpec("sim-b")), store=UncertaintyStore())
        rig.started("move", submit="transport_loss")
        other = rig.ready(1)
        self.assertIs(rig.admit(other, "move").state, S.VALIDATED)


class RecoveryTests(unittest.TestCase):
    def rig(self):
        return uncertain_rig(recovery_assessors={"move": recovered})

    def test_fresh_recovery_evidence_resolves_without_touching_the_command_or_the_provider(self) -> None:
        rig, connection, record = self.rig()
        history = record.history
        calls = list(rig.provider.calls)
        entry = rig.executor.resolve_uncertainty_by_recovery(connection, record.command_id)
        self.assertFalse(entry.unresolved)
        self.assertIs(entry.resolution.kind, K.RECOVERY_EVIDENCE)
        self.assertEqual(entry.resolution.basis, BASIS)
        self.assertFalse(entry.resolution.proves_physical_effect)
        self.assertIs(record.state, S.UNKNOWN_RESULT)
        self.assertEqual(record.history, history)
        self.assertEqual(rig.provider.calls, calls)
        self.assertIs(rig.executor.uncertainty_store.state_for(rig.runtime.provider_id, connection.device.device_ref),
                      U.RESOLVED_BY_RECOVERY_EVIDENCE)

    def test_after_recovery_a_new_safety_sensitive_command_may_be_admitted_but_the_old_one_never_retried(self) -> None:
        rig, connection, record = self.rig()
        rig.executor.resolve_uncertainty_by_recovery(connection, record.command_id)
        self.assertIs(rig.runtime.refresh_evidence(connection), C.READY)
        self.assertIs(rig.admit(connection, "move").state, S.VALIDATED)
        with self.assertRaises(InvalidTransition):
            rig.executor.submit(record.command_id)
        self.assertEqual(len(rig.submits()), 1)

    def test_recovery_works_from_a_reconnected_connection(self) -> None:
        rig, connection, record = self.rig()
        again = rig.ready()
        entry = rig.executor.resolve_uncertainty_by_recovery(again, record.command_id)
        self.assertFalse(entry.unresolved)
        self.assertEqual(entry.origin_connection_id, connection.connection_id)

    def refused(self, rig, connection, record, fragment):
        with self.assertRaises(RecoveryNotEstablished) as ctx:
            rig.executor.resolve_uncertainty_by_recovery(connection, record.command_id)
        self.assertIn(fragment, str(ctx.exception))
        self.assertTrue(rig.executor.uncertainty_store.get(
            rig.runtime.provider_id, connection.device.device_ref, record.command_id).unresolved)

    def test_stale_evidence_does_not_resolve(self) -> None:
        rig, connection, record = self.rig()
        rig.stale = True
        self.refused(rig, connection, record, "evidence_not_fresh")

    def test_unavailable_or_wrong_target_evidence_does_not_resolve(self) -> None:
        rig, connection, record = self.rig()
        original = rig.executor._evidence_source
        rig.executor._evidence_source = lambda c: (_ for _ in ()).throw(RuntimeError("down"))
        self.refused(rig, connection, record, "evidence_unavailable")
        other = rig.ready()
        rig.executor._evidence_source = lambda c: original(other)
        self.refused(rig, connection, record, "wrong_target")

    def test_assessor_failures_and_negative_verdicts_do_not_resolve(self) -> None:
        for assessor, fragment in (
            (lambda *a: RecoveryVerdict(False), "recovery_not_established"),
            (lambda *a: (_ for _ in ()).throw(RuntimeError("x")), "assessor_failed"),
            (lambda *a: RecoveryVerdict(True, "looks fine", ()), "rests on"),
            (lambda *a: RecoveryVerdict(True, " ", BASIS), "evidence"),
        ):
            rig, connection, record = uncertain_rig(recovery_assessors={"move": assessor})
            self.refused(rig, connection, record, fragment)

    def test_a_kind_without_an_assessor_cannot_be_recovered_automatically(self) -> None:
        rig, connection, record = uncertain_rig()
        self.refused(rig, connection, record, "no_recovery_assessor_for_kind")

    def test_connection_checks(self) -> None:
        rig, connection, record = self.rig()
        foreign, _, _ = self.rig()
        for bad, fragment in ((foreign.ready(), "connection_not_owned"), ("nope", "unknown command or connection")):
            with self.assertRaises(RecoveryNotEstablished) as ctx:
                rig.executor.resolve_uncertainty_by_recovery(bad, record.command_id)
            self.assertIn(fragment, str(ctx.exception))
        rig.runtime.report_transport_loss(connection, C.DISCONNECTED)
        self.refused(rig, connection, record, "connection_not_active")
        with self.assertRaises(RecoveryNotEstablished):
            rig.executor.resolve_uncertainty_by_recovery(rig.ready(), "cmd-unknown")

    def test_resolving_twice_is_refused_and_keeps_the_first_resolution(self) -> None:
        rig, connection, record = self.rig()
        first = rig.executor.resolve_uncertainty_by_recovery(connection, record.command_id)
        with self.assertRaises(RecoveryNotEstablished) as ctx:
            rig.executor.resolve_uncertainty_by_recovery(connection, record.command_id)
        self.assertIn("already_resolved", str(ctx.exception))
        again = rig.executor.uncertainty_store.get(rig.runtime.provider_id, connection.device.device_ref, record.command_id)
        self.assertEqual(again.resolution, first.resolution)

    def test_a_device_mismatch_finds_no_entry(self) -> None:
        rig = Rig(devices=(SimulatedDeviceSpec("sim-a"), SimulatedDeviceSpec("sim-b")), store=UncertaintyStore(),
                  recovery_assessors={"move": recovered})
        a, record = rig.started("move", submit="transport_loss")
        with self.assertRaises(UnknownUncertainty):
            rig.executor.resolve_uncertainty_by_recovery(rig.ready(1), record.command_id)
        self.assertEqual(len(rig.executor.unresolved_devices()), 1)

    def test_concurrent_recovery_resolves_once(self) -> None:
        rig, connection, record = self.rig()
        barrier = threading.Barrier(6)
        results = []

        def worker():
            barrier.wait(10)
            try:
                rig.executor.resolve_uncertainty_by_recovery(connection, record.command_id)
                results.append("ok")
            except RecoveryNotEstablished:
                results.append("refused")

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual(sorted(results), ["ok"] + ["refused"] * 5)


class ClearanceTests(unittest.TestCase):
    def test_authorized_clearance_resolves_and_is_labeled_as_clearance(self) -> None:
        op = Op()
        rig, connection, record = uncertain_rig(clearance_authorizer=op)
        history = record.history
        entry = rig.executor.clear_uncertainty_by_operator(connection.device.device_ref, record.command_id, "alice", "inspected on site")
        self.assertIs(entry.resolution.kind, K.OPERATOR_CLEARANCE)
        self.assertEqual((entry.resolution.resolved_by, entry.resolution.evidence), ("alice", "inspected on site"))
        self.assertEqual(entry.resolution.basis, ())
        self.assertFalse(entry.resolution.proves_physical_effect)
        self.assertEqual(op.asked, [("alice", rig.runtime.provider_id, connection.device.device_ref)])
        self.assertIs(record.state, S.UNKNOWN_RESULT)
        self.assertEqual(record.history, history)
        self.assertIs(rig.executor.uncertainty_store.state_for(rig.runtime.provider_id, connection.device.device_ref),
                      U.CLEARED_BY_OPERATOR)

    def test_clearance_does_not_replace_fresh_evidence(self) -> None:
        rig, connection, record = uncertain_rig(clearance_authorizer=Op())
        rig.executor.clear_uncertainty_by_operator(connection.device.device_ref, record.command_id, "alice", "inspected")
        again = rig.ready()
        rig.stale = True
        self.assertIs(rig.admit(again, "move").state, S.SAFETY_BLOCKED)
        rig.stale = False
        self.assertIs(rig.admit(again, "move").state, S.VALIDATED)

    def test_clearance_needs_an_authorizer_that_says_true(self) -> None:
        class Truthy:
            def may_clear(self, *a):
                return "yes"

        class Boom:
            def may_clear(self, *a):
                raise RuntimeError("down")

        for extra in ({}, {"clearance_authorizer": Op(False)}, {"clearance_authorizer": Truthy()},
                      {"clearance_authorizer": Boom()}):
            rig, connection, record = uncertain_rig(**extra)
            with self.assertRaises(ClearanceNotAuthorized):
                rig.executor.clear_uncertainty_by_operator(connection.device.device_ref, record.command_id, "alice", "r")
            self.assertEqual(len(rig.executor.unresolved_devices()), 1, extra)

    def test_clearance_needs_an_operator_and_a_reason(self) -> None:
        rig, connection, record = uncertain_rig(clearance_authorizer=Op())
        for operator, reason in (("", "r"), (" ", "r"), ("alice", ""), ("alice", " ")):
            with self.assertRaises(ClearanceNotAuthorized):
                rig.executor.clear_uncertainty_by_operator(connection.device.device_ref, record.command_id, operator, reason)
        self.assertEqual(len(rig.executor.unresolved_devices()), 1)

    def test_unknown_entries_and_repeats_are_refused(self) -> None:
        rig, connection, record = uncertain_rig(clearance_authorizer=Op(), recovery_assessors={"move": recovered})
        ref = connection.device.device_ref
        with self.assertRaises(UnknownUncertainty):
            rig.executor.clear_uncertainty_by_operator(ref, "cmd-unknown", "alice", "r")
        with self.assertRaises(UnknownUncertainty):
            rig.executor.clear_uncertainty_by_operator("other-device", record.command_id, "alice", "r")
        rig.executor.clear_uncertainty_by_operator(ref, record.command_id, "alice", "r")
        with self.assertRaises(UncertaintyAlreadyResolved):
            rig.executor.clear_uncertainty_by_operator(ref, record.command_id, "bob", "again")
        with self.assertRaises(RecoveryNotEstablished):
            rig.executor.resolve_uncertainty_by_recovery(connection, record.command_id)
        self.assertEqual(rig.executor.uncertainty_store.get(rig.runtime.provider_id, ref, record.command_id).resolution.resolved_by, "alice")

    def test_recovery_and_clearance_stay_distinguishable(self) -> None:
        rig, connection, record = uncertain_rig(recovery_assessors={"move": recovered})
        rig.executor.resolve_uncertainty_by_recovery(connection, record.command_id)
        entry = rig.executor.uncertainty_store.get(rig.runtime.provider_id, connection.device.device_ref, record.command_id)
        self.assertIsNone(entry.resolution.resolved_by)
        self.assertNotEqual(entry.resolution.basis, ())

    def test_concurrent_clearance_resolves_once(self) -> None:
        rig, connection, record = uncertain_rig(clearance_authorizer=Op())
        barrier = threading.Barrier(6)
        results = []

        def worker(i):
            barrier.wait(10)
            try:
                rig.executor.clear_uncertainty_by_operator(connection.device.device_ref, record.command_id, f"op{i}", "r")
                results.append("ok")
            except Exception as exc:
                results.append(type(exc).__name__)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual(sorted(results), ["UncertaintyAlreadyResolved"] * 5 + ["ok"])


class RestartTests(unittest.TestCase):
    """A restarted process starts with an empty store. That is unknown history, never verified absence of
    unresolved physical effects."""

    def fresh_rig(self, **extra):
        return Rig(baseline=False, store=UncertaintyStore(), **extra)

    def test_a_new_store_blocks_safety_sensitive_commands_even_with_fresh_evidence(self) -> None:
        old, connection, record = uncertain_rig()
        self.assertEqual(len(old.executor.unresolved_devices()), 1)
        fresh = self.fresh_rig()
        again = fresh.ready()
        self.assertEqual(fresh.executor.unresolved_devices(), frozenset())  # nothing persisted
        self.assertIs(fresh.executor.uncertainty_store.state_for(fresh.runtime.provider_id, again.device.device_ref), U.UNKNOWN)
        blocked = fresh.admit(again, "move")
        self.assertIs(blocked.state, S.SAFETY_BLOCKED)
        self.assertIn("uncertainty_unknown", blocked.history[-1].evidence)
        self.assertEqual(fresh.submits(), [])
        self.assertIs(again.state, C.READY)

    def test_an_unestablished_history_blocks_even_when_the_configured_view_says_established(self) -> None:
        rig = Rig(baseline=False)  # the executor's own empty store plus a separate, permissive configured view
        blocked = rig.admit(rig.ready(), "move")
        self.assertIs(blocked.state, S.SAFETY_BLOCKED)
        self.assertIn("uncertainty_unknown", blocked.history[-1].evidence)

    def test_passive_reads_and_non_safety_kinds_are_unaffected(self) -> None:
        fresh = self.fresh_rig()
        connection = fresh.ready()
        fresh.runtime.read_telemetry(connection)
        fresh.runtime.describe_preview(connection)
        fresh.runtime.capability_report(connection)
        self.assertIs(fresh.runtime.refresh_evidence(connection), C.READY)
        self.assertIs(fresh.admit(connection, "config").state, S.VALIDATED)

    def test_a_configured_view_saying_nothing_recorded_does_not_pass(self) -> None:
        class Nothing:
            def state_for(self, provider_id, device_ref):
                return U.NONE_RECORDED

        rig = Rig(store=UncertaintyStore())
        rig.executor._uncertainty = Nothing()
        blocked = rig.admit(rig.ready(), "move")
        self.assertIs(blocked.state, S.SAFETY_BLOCKED)
        self.assertIn("history_not_established", blocked.history[-1].evidence)

    def test_operator_baseline_establishes_unknown_history_and_freshness_is_still_required(self) -> None:
        fresh = self.fresh_rig(clearance_authorizer=Op())
        connection = fresh.ready()
        ref = connection.device.device_ref
        resolution = fresh.executor.establish_baseline_by_operator(ref, "alice", "inspected the device")
        self.assertIs(resolution.kind, K.OPERATOR_CLEARANCE)
        self.assertFalse(resolution.proves_physical_effect)
        self.assertIs(fresh.executor.uncertainty_store.state_for(fresh.runtime.provider_id, ref), U.CLEARED_BY_OPERATOR)
        fresh.stale = True
        self.assertIs(fresh.admit(connection, "move").state, S.SAFETY_BLOCKED)
        fresh.stale = False
        self.assertIs(fresh.admit(connection, "move").state, S.VALIDATED)

    def test_operator_baseline_needs_authorization_operator_and_reason(self) -> None:
        for extra in ({}, {"clearance_authorizer": Op(False)}):
            fresh = self.fresh_rig(**extra)
            ref = fresh.ready().device.device_ref
            with self.assertRaises(ClearanceNotAuthorized):
                fresh.executor.establish_baseline_by_operator(ref, "alice", "r")
            self.assertFalse(fresh.executor.uncertainty_store.history_established(fresh.runtime.provider_id, ref))
        fresh = self.fresh_rig(clearance_authorizer=Op())
        ref = fresh.ready().device.device_ref
        for operator, reason in (("", "r"), ("alice", " ")):
            with self.assertRaises(ClearanceNotAuthorized):
                fresh.executor.establish_baseline_by_operator(ref, operator, reason)

    def baseline_recovery(self, resolved=True, basis=BASIS):
        from tsn_dss.engine.device_runtime.command_models import FreshnessRequirement
        from tsn_dss.engine.device_runtime.uncertainty import BaselineRecovery

        return BaselineRecovery(lambda c, r, snap, now: RecoveryVerdict(resolved, "device state read back", basis),
                                (FreshnessRequirement("pose", timedelta(minutes=1)),))

    def test_recovery_baseline_needs_fresh_evidence_and_an_assessor_verdict(self) -> None:
        fresh = self.fresh_rig(baseline_recovery=self.baseline_recovery())
        connection = fresh.ready()
        ref = connection.device.device_ref
        fresh.stale = True
        with self.assertRaises(RecoveryNotEstablished) as ctx:
            fresh.executor.establish_baseline_by_recovery(connection)
        self.assertIn("evidence_not_fresh", str(ctx.exception))
        fresh.stale = False
        resolution = fresh.executor.establish_baseline_by_recovery(connection)
        self.assertIs(resolution.kind, K.RECOVERY_EVIDENCE)
        self.assertEqual(resolution.basis, BASIS)
        self.assertIs(fresh.executor.uncertainty_store.state_for(fresh.runtime.provider_id, ref), U.RESOLVED_BY_RECOVERY_EVIDENCE)
        self.assertIs(fresh.admit(connection, "move").state, S.VALIDATED)

    def test_recovery_baseline_refusals_leave_the_history_unknown(self) -> None:
        cases = (
            (Rig(baseline=False, store=UncertaintyStore()), "no_baseline_recovery_configured"),
            (Rig(baseline=False, store=UncertaintyStore(), baseline_recovery=self.baseline_recovery(resolved=False)), "recovery_not_established"),
            (Rig(baseline=False, store=UncertaintyStore(), baseline_recovery=self.baseline_recovery(basis=())), "rests on"),
        )
        for rig, fragment in cases:
            connection = rig.ready()
            with self.assertRaises(RecoveryNotEstablished) as ctx:
                rig.executor.establish_baseline_by_recovery(connection)
            self.assertIn(fragment, str(ctx.exception))
            self.assertFalse(rig.executor.uncertainty_store.history_established(rig.runtime.provider_id, connection.device.device_ref))
            self.assertIs(rig.admit(connection, "move").state, S.SAFETY_BLOCKED)

    def test_recovery_baseline_connection_checks_and_double_establishment(self) -> None:
        fresh = self.fresh_rig(baseline_recovery=self.baseline_recovery())
        connection = fresh.ready()
        with self.assertRaises(RecoveryNotEstablished):
            fresh.executor.establish_baseline_by_recovery("nope")
        fresh.executor.establish_baseline_by_recovery(connection)
        with self.assertRaises(RecoveryNotEstablished) as ctx:
            fresh.executor.establish_baseline_by_recovery(fresh.ready())
        self.assertIn("already_established", str(ctx.exception))
        from tsn_dss.engine.device_runtime.uncertainty import BaselineAlreadyEstablished

        with self.assertRaises(BaselineAlreadyEstablished):
            fresh.executor.uncertainty_store.establish_baseline(
                fresh.runtime.provider_id, connection.device.device_ref,
                Resolution(K.OPERATOR_CLEARANCE, T0, "again", resolved_by="op"))

    def test_a_baseline_never_hides_an_open_uncertainty(self) -> None:
        rig, connection, record = uncertain_rig(clearance_authorizer=Op(), baseline_recovery=self.baseline_recovery())
        ref = connection.device.device_ref
        with self.assertRaises(RecoveryNotEstablished) as ctx:
            rig.executor.establish_baseline_by_recovery(rig.ready())
        self.assertIn("unresolved_uncertainty_present", str(ctx.exception))
        with self.assertRaises(ClearanceNotAuthorized):
            rig.executor.establish_baseline_by_operator(ref, "alice", "r")
        self.assertIs(rig.executor.uncertainty_store.state_for(rig.runtime.provider_id, ref), U.UNRESOLVED)

    def test_established_history_survives_reconnect_but_not_a_new_store(self) -> None:
        fresh = self.fresh_rig(clearance_authorizer=Op())
        connection = fresh.ready()
        fresh.executor.establish_baseline_by_operator(connection.device.device_ref, "alice", "r")
        fresh.runtime.disconnect(connection)
        self.assertIs(fresh.admit(fresh.ready(), "move").state, S.VALIDATED)
        restarted = self.fresh_rig(clearance_authorizer=Op())
        self.assertIs(restarted.admit(restarted.ready(), "move").state, S.SAFETY_BLOCKED)

    def test_resolving_an_uncertainty_also_establishes_the_history(self) -> None:
        rig = self.fresh_rig(clearance_authorizer=Op())
        connection = rig.ready()
        ref = connection.device.device_ref
        rig.executor.establish_baseline_by_operator(ref, "alice", "start")
        store, provider_id = rig.executor.uncertainty_store, rig.runtime.provider_id
        fresh = self.fresh_rig()
        store = fresh.executor.uncertainty_store
        store.open_entry(provider_id, ref, "c-x", "move", T0, "conn")
        self.assertIs(store.state_for(provider_id, ref), U.UNRESOLVED)
        self.assertFalse(store.history_established(provider_id, ref))
        store.resolve(provider_id, ref, "c-x", Resolution(K.RECOVERY_EVIDENCE, T0, "e", basis=BASIS))
        self.assertTrue(store.history_established(provider_id, ref))
        self.assertIs(store.state_for(provider_id, ref), U.RESOLVED_BY_RECOVERY_EVIDENCE)

    def test_the_package_does_no_io_for_uncertainty(self) -> None:
        text = (PACKAGE / "uncertainty.py").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"\bopen\(|pickle|json|sqlite|shelve|pathlib|import os\b|socket")

    def test_baseline_recovery_requires_declared_freshness(self) -> None:
        from tsn_dss.engine.device_runtime.uncertainty import BaselineRecovery

        for bad in ((), ("pose",)):
            with self.assertRaises(ValueError):
                BaselineRecovery(lambda *a: RecoveryVerdict(True, "e", BASIS), bad)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            BaselineRecovery("not callable", ())  # type: ignore[arg-type]


class RuntimeCheckTests(unittest.TestCase):
    def test_the_unresolved_check_only_affects_degraded_to_ready(self) -> None:
        for answer, expected in ((True, C.CONNECTED), (False, C.READY), ("odd", C.CONNECTED)):
            rig = Rig()
            connection = rig.ready()
            rig.runtime.report_transport_loss(connection, C.DEGRADED)
            rig.runtime.add_unresolved_condition_check(lambda c, a=answer: a)
            self.assertIs(rig.runtime.refresh_evidence(connection), expected, answer)

    def test_a_failing_check_counts_as_unresolved_and_connected_still_becomes_ready(self) -> None:
        rig = Rig()
        rig.runtime.add_unresolved_condition_check(lambda c: 1 / 0)
        connection = rig.runtime.connect(rig.runtime.open_connection(rig.devices[0]))
        self.assertIs(rig.runtime.refresh_evidence(connection), C.READY)  # section 6: connected -> ready as before
        rig.runtime.report_transport_loss(connection, C.DEGRADED)
        self.assertIs(rig.runtime.refresh_evidence(connection), C.CONNECTED)

    def test_without_any_check_db01_behavior_is_unchanged(self) -> None:
        runtime = ProviderRuntime(SimulatorProvider(clock=ManualClock()), clock=ManualClock(), id_generator=SequentialIdGenerator())
        connection = runtime.connect(runtime.open_connection(runtime.discover().devices[0]))
        runtime.refresh_evidence(connection)
        runtime.report_transport_loss(connection, C.DEGRADED)
        self.assertIs(runtime.refresh_evidence(connection), C.READY)


class BoundaryTests(unittest.TestCase):
    def test_only_the_executor_writes_the_store(self) -> None:
        pattern = re.compile(r"\.(open_entry|resolve)\(")
        users = {p.name for p in PACKAGE.glob("*.py") if pattern.search(p.read_text(encoding="utf-8"))}
        self.assertEqual(users, {"command_executor.py"})

    def test_the_store_calls_no_provider_and_the_runtime_does_not_know_it(self) -> None:
        self.assertNotRegex((PACKAGE / "uncertainty.py").read_text(encoding="utf-8"), r"submit_command|poll_command|_provider|read_telemetry")
        self.assertNotRegex((PACKAGE / "runtime.py").read_text(encoding="utf-8"), r"UncertaintyStore|uncertainty import")

    def test_the_package_does_not_export_the_new_surface(self) -> None:
        import tsn_dss.engine.device_runtime as pkg

        self.assertTrue({"UncertaintyStore", "ClearanceAuthorizer", "RecoveryVerdict"}.isdisjoint(pkg.__all__))

    def test_documented_contract_discrepancies_exist(self) -> None:
        doc = (PACKAGE.parents[2] / "docs" / "DB-04_CONTRACT_DISCREPANCIES.md").read_text(encoding="utf-8")
        for marker in ("D1", "D2", "D3", "D4", "D5"):
            self.assertIn(marker, doc)


if __name__ == "__main__":
    unittest.main()
