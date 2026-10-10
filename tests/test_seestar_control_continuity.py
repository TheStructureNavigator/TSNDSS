"""DB-05: recovery continuity across replaced Connections (composition level; DB-04 semantics unchanged)."""

from __future__ import annotations

import unittest
from datetime import timedelta

from tsn_dss.engine.device_runtime import ConnectionState as C
from tsn_dss.engine.device_runtime.command_executor import ClearanceNotAuthorized, CommandExecutor, CommandIntent, RecoveryNotEstablished
from tsn_dss.engine.device_runtime.models import CommandState as S
from tsn_dss.engine.device_runtime.uncertainty import UncertaintyStore, UnknownUncertainty
from tsn_dss.engine.seestar_control import (
    ARM_DEPLOY,
    ARM_PARK,
    SCENERY_START,
    SCENERY_STOP,
    ControlAttachError,
    PendingRecovery,
)

try:
    from test_seestar_control_runtime import Allow, Deny, Operators, SimSeestar, T0, build, run
except ImportError:  # pragma: no cover
    from tests.test_seestar_control_runtime import Allow, Deny, Operators, SimSeestar, T0, build, run


def uncertain_start(**kw):
    """The device has an open arm; the start command's effect happened but its reply was lost: unknown_result."""
    sim, control, handle, clock = build(SimSeestar(arm_closed=False), **kw)
    sim.modes[SCENERY_START] = "effect_then_drop"
    outcome = run(handle, clock, SCENERY_START)
    sim.tick(); sim.tick()  # the device settles with its cameras running
    assert outcome.classification == "unknown_result" and outcome.uncertainty_open
    return sim, control, handle, clock, outcome


def reconnect(control, old):
    control.runtime.disconnect(old.connection)
    return control.attach()


class RecoveryAfterReconnectTests(unittest.TestCase):
    def test_the_new_handle_recovers_with_fresh_telemetry_and_the_original_record_is_untouched(self) -> None:
        sim, control, first, clock, outcome = uncertain_start()
        record = first.executor.get(outcome.command_id)
        history, kind = record.history, record.policy.kind_id
        second = reconnect(control, first)
        self.assertEqual(second.connection.state, C.READY)
        self.assertEqual([(p.command_id, p.kind_id, p.record_available) for p in second.pending_recoveries()],
                         [(outcome.command_id, SCENERY_START, True)])
        calls, reads = list(sim.control_calls), sim.reads
        entry = second.recover(outcome.command_id)
        self.assertGreater(sim.reads, reads)  # fresh telemetry from the new Connection
        self.assertEqual(sim.control_calls, calls)  # recovery sent nothing
        self.assertFalse(entry.unresolved)
        self.assertEqual((entry.command_id, entry.kind_id), (outcome.command_id, kind))
        self.assertEqual(entry.resolution.kind.value, "recovery_evidence")
        self.assertFalse(entry.resolution.proves_physical_effect)
        self.assertIs(record.state, S.UNKNOWN_RESULT)
        self.assertEqual(record.history, history)
        self.assertEqual(entry.original_outcome, "unknown_result")
        self.assertEqual(second.pending_recoveries(), ())

    def test_reconnecting_and_ready_are_not_evidence_and_nothing_happens_automatically(self) -> None:
        sim, control, first, clock, outcome = uncertain_start()
        calls = list(sim.control_calls)
        second = reconnect(control, first)
        self.assertEqual(second.connection.state, C.READY)
        self.assertEqual(len(control.uncertainty_store.unresolved_devices()), 1)  # attach resolved nothing
        for kind in (SCENERY_STOP, ARM_PARK):
            blocked = run(second, clock, kind)
            self.assertEqual(blocked.classification, "safety_blocked")
            self.assertIn("uncertainty_unresolved", blocked.last_evidence)
        self.assertEqual(len(control.uncertainty_store.unresolved_devices()), 1)  # a blocked request recovered nothing
        self.assertEqual(sim.control_calls, calls)

    def test_after_recovery_new_commands_still_need_fresh_safe_evidence(self) -> None:
        sim, control, first, clock, outcome = uncertain_start()
        second = reconnect(control, first)
        second.recover(outcome.command_id)
        sim.fail_app_reads = 10  # the camera state can no longer be read fresh
        blocked = run(second, clock, SCENERY_STOP)
        self.assertEqual(blocked.classification, "safety_blocked")
        self.assertIn("state_stale", blocked.last_evidence)
        sim.fail_app_reads = 0
        self.assertTrue(run(second, clock, SCENERY_STOP).succeeded)

    def test_recovery_does_not_authorize_anything(self) -> None:
        class Switch:
            allowed = True

            def is_authorized(self, requested_by, kind, ref) -> bool:
                return self.allowed

        switch = Switch()
        sim, control, first, clock, outcome = uncertain_start(authorizer=switch)
        second = reconnect(control, first)
        second.recover(outcome.command_id)
        switch.allowed = False  # the injected authorizer decides, not the recovery
        calls = list(sim.control_calls)
        blocked = run(second, clock, SCENERY_STOP)
        self.assertEqual(blocked.classification, "safety_blocked")
        self.assertFalse(blocked.submitted)
        self.assertEqual(sim.control_calls, calls)

    def test_the_blocked_state_survives_a_refused_recovery(self) -> None:
        sim, control, first, clock, outcome = uncertain_start()
        second = reconnect(control, first)
        for label, mutate, undo in (
            ("moving", lambda: setattr(sim, "moving", True), lambda: setattr(sim, "moving", False)),
            ("mixed_cameras", lambda: setattr(sim, "cameras", "starting"), lambda: setattr(sim, "cameras", "ready")),
            ("stale", lambda: setattr(sim, "fail_app_reads", 10), lambda: setattr(sim, "fail_app_reads", 0)),
        ):
            mutate()
            with self.assertRaises(RecoveryNotEstablished, msg=label):
                second.recover(outcome.command_id)
            undo()
            self.assertEqual(len(second.pending_recoveries()), 1, label)
        self.assertEqual(second.recover(outcome.command_id).unresolved, False)


class ContextRetentionTests(unittest.TestCase):
    def test_only_an_executor_that_still_owns_an_uncertainty_is_kept(self) -> None:
        sim, control, first, clock = build()
        self.assertTrue(run(first, clock, ARM_DEPLOY).succeeded)
        second = reconnect(control, first)
        self.assertEqual(control._retained, [])  # a clean history is not retained
        sim2, control2, uncertain, clock2, outcome = uncertain_start()
        second2 = reconnect(control2, uncertain)
        self.assertEqual(len(control2._retained), 1)
        third = reconnect(control2, second2)
        fourth = reconnect(control2, third)
        self.assertEqual(len(control2._retained), 1)  # repeated reconnects keep one owner, not a growing list
        self.assertEqual([p.command_id for p in fourth.pending_recoveries()], [outcome.command_id])
        fourth.recover(outcome.command_id)
        self.assertEqual(control2._retained, [])  # released as soon as nothing is owed

    def test_recovery_works_from_any_later_handle(self) -> None:
        sim, control, first, clock, outcome = uncertain_start()
        handle = first
        for _ in range(4):
            handle = reconnect(control, handle)
        self.assertEqual(len(handle.pending_recoveries()), 1)
        self.assertFalse(handle.recover(outcome.command_id).unresolved)

    def test_ids_never_repeat_across_attachments(self) -> None:
        sim, control, first, clock, outcome = uncertain_start()
        second = reconnect(control, first)
        second.recover(outcome.command_id)
        again = run(second, clock, SCENERY_STOP)
        self.assertNotEqual(again.command_id, outcome.command_id)


class IdentityTests(unittest.TestCase):
    def test_another_device_at_the_same_address_cannot_recover_and_is_not_cleared_by_it(self) -> None:
        sim, control, first, clock, outcome = uncertain_start()
        old_ref = first.connection.device.device_ref
        sim.serial = "ANOTHER-DEVICE"
        control.runtime.disconnect(first.connection)
        second = control.attach()
        self.assertNotEqual(second.connection.device.device_ref, old_ref)
        self.assertEqual(second.pending_recoveries(), ())
        calls = list(sim.control_calls)
        with self.assertRaises(UnknownUncertainty):
            second.recover(outcome.command_id)
        self.assertEqual(sim.control_calls, calls)
        self.assertEqual(len(control.uncertainty_store.unresolved_devices()), 1)
        self.assertEqual(run(second, clock, SCENERY_STOP).classification, "safety_blocked")  # its own history is unknown
        # the first device's uncertainty is untouched and still recoverable once that device is back
        sim.serial = None
        control.runtime.disconnect(second.connection)
        third = control.attach()
        self.assertEqual(third.connection.device.device_ref, old_ref)
        self.assertFalse(third.recover(outcome.command_id).unresolved)


class MissingRecordAndDuplicateTests(unittest.TestCase):
    def test_an_uncertainty_without_its_original_record_cannot_be_recovered_but_can_be_cleared_by_an_operator(self) -> None:
        store = UncertaintyStore()
        sim, control, handle, clock = build(store=store, clearance=Operators(True), baseline=False)
        ref = handle.connection.device.device_ref
        store.open_entry(control.runtime.provider_id, ref, "cmd-ghost", SCENERY_START, T0, "conn-old")
        pending = handle.pending_recoveries()
        self.assertEqual([(p.command_id, p.record_available) for p in pending], [("cmd-ghost", False)])
        with self.assertRaises(RecoveryNotEstablished) as ctx:
            handle.recover("cmd-ghost")
        self.assertEqual(str(ctx.exception), "original_record_missing")
        self.assertEqual(len(handle.pending_recoveries()), 1)
        entry = handle.executor.clear_uncertainty_by_operator(ref, "cmd-ghost", "alice", "inspected")
        self.assertEqual(entry.resolution.kind.value, "operator_clearance")

    def test_a_foreign_devices_uncertainty_is_reported_as_unknown_not_as_a_missing_record(self) -> None:
        store = UncertaintyStore()
        sim, control, handle, clock = build(store=store, baseline=False)
        store.open_entry(control.runtime.provider_id, "some-other-device", "cmd-foreign", SCENERY_START, T0, "conn-old")
        with self.assertRaises(UnknownUncertainty):
            handle.recover("cmd-foreign")
        self.assertEqual(handle.pending_recoveries(), ())

    def test_a_second_recovery_is_refused_and_keeps_the_first_resolution(self) -> None:
        sim, control, first, clock, outcome = uncertain_start()
        second = reconnect(control, first)
        resolution = second.recover(outcome.command_id).resolution
        third = reconnect(control, second)
        with self.assertRaises(RecoveryNotEstablished) as ctx:
            third.recover(outcome.command_id)
        self.assertEqual(str(ctx.exception), "already_resolved")
        entry = control.uncertainty_store.get(control.runtime.provider_id, third.connection.device.device_ref, outcome.command_id)
        self.assertEqual(entry.resolution, resolution)

    def test_an_unknown_command_id_is_refused(self) -> None:
        sim, control, handle, clock = build()
        with self.assertRaises(UnknownUncertainty):
            handle.recover("cmd-never-existed")


class RestartTests(unittest.TestCase):
    def test_a_restarted_process_has_no_history_to_recover_and_needs_a_baseline(self) -> None:
        sim, control, first, clock, outcome = uncertain_start()
        sim2, control2, handle2, clock2 = build(sim, baseline=False)  # new process: empty store, nothing retained
        self.assertEqual(handle2.pending_recoveries(), ())
        self.assertEqual(control2._retained, [])
        with self.assertRaises(UnknownUncertainty):
            handle2.recover(outcome.command_id)
        calls = list(sim.control_calls)
        blocked = run(handle2, clock2, SCENERY_STOP)
        self.assertEqual(blocked.classification, "safety_blocked")
        self.assertIn("uncertainty_unknown", blocked.last_evidence)
        resolution = handle2.executor.establish_baseline_by_recovery(handle2.connection)  # the explicit path still exists
        self.assertEqual(resolution.kind.value, "recovery_evidence")
        self.assertTrue(run(handle2, clock2, SCENERY_STOP).succeeded)
        self.assertEqual(sim.control_calls, calls + [SCENERY_STOP])


class OperatorClearanceTests(unittest.TestCase):
    def test_clearance_works_across_a_reconnect_without_the_old_executor_and_proves_nothing(self) -> None:
        sim, control, first, clock, outcome = uncertain_start(clearance=Operators(True))
        second = reconnect(control, first)
        ref = second.connection.device.device_ref
        entry = second.executor.clear_uncertainty_by_operator(ref, outcome.command_id, "alice", "looked at it")
        self.assertEqual((entry.resolution.kind.value, entry.resolution.resolved_by), ("operator_clearance", "alice"))
        self.assertFalse(entry.resolution.proves_physical_effect)
        with self.assertRaises(RecoveryNotEstablished) as ctx:
            second.recover(outcome.command_id)  # already resolved: recovery cannot overwrite a clearance
        self.assertEqual(str(ctx.exception), "already_resolved")
        self.assertTrue(run(second, clock, SCENERY_STOP).succeeded)  # still passes the gate on fresh safe evidence

    def test_clearance_needs_an_injected_authorizer(self) -> None:
        sim, control, first, clock, outcome = uncertain_start()
        second = reconnect(control, first)
        with self.assertRaises(ClearanceNotAuthorized):
            second.executor.clear_uncertainty_by_operator(second.connection.device.device_ref, outcome.command_id, "alice", "r")


class StaleHandleTests(unittest.TestCase):
    def test_a_replaced_handle_cannot_issue_or_recover_anything(self) -> None:
        sim, control, first, clock, outcome = uncertain_start(clearance=Operators(True))
        second = reconnect(control, first)
        calls = list(sim.control_calls)
        stale_errors = [
            lambda: first.executor.admit(CommandIntent(second.connection, SCENERY_STOP, "op")),
            lambda: first.executor.submit(outcome.command_id),
            lambda: first.executor.poll(outcome.command_id),
            lambda: first.executor.enforce_deadline(outcome.command_id),
            lambda: first.executor.establish_baseline_by_recovery(second.connection),
            lambda: first.executor.establish_baseline_by_operator("x", "alice", "r"),
            lambda: first.executor.clear_uncertainty_by_operator("x", outcome.command_id, "alice", "r"),
            lambda: first.executor.resolve_uncertainty_by_recovery(second.connection, outcome.command_id),
            lambda: first.driver.execute(second.connection, SCENERY_STOP, "op", deadline=clock() + timedelta(minutes=5), poll_interval=timedelta(seconds=30)),
            lambda: first.recover(outcome.command_id),
            lambda: first.pending_recoveries(),
        ]
        for index, call in enumerate(stale_errors):
            with self.assertRaises(ControlAttachError, msg=index) as ctx:
                call()
            self.assertEqual(str(ctx.exception), "stale_handle")
        self.assertEqual(sim.control_calls, calls)
        self.assertEqual(len(control.uncertainty_store.unresolved_devices()), 1)
        # reads stay available, and the current handle is unaffected
        self.assertIs(first.executor.get(outcome.command_id).state, S.UNKNOWN_RESULT)
        self.assertEqual(len(first.executor.commands), 1)
        self.assertFalse(second.recover(outcome.command_id).unresolved)

    def test_the_current_handle_stays_usable_until_it_is_replaced(self) -> None:
        sim, control, handle, clock = build()
        self.assertTrue(run(handle, clock, ARM_DEPLOY).succeeded)
        self.assertTrue(run(handle, clock, SCENERY_START).succeeded)


class SurfaceTests(unittest.TestCase):
    def test_no_old_executor_is_reachable_and_the_public_surface_is_minimal(self) -> None:
        sim, control, first, clock, outcome = uncertain_start()
        second = reconnect(control, first)
        self.assertNotIsInstance(second.executor, CommandExecutor)
        for owner in (control, first, second):
            for name in dir(owner):
                if not name.startswith("_"):
                    self.assertNotIsInstance(getattr(owner, name), CommandExecutor, name)
        self.assertEqual({n for n in dir(control) if not n.startswith("_")}, {"attach", "runtime", "uncertainty_store"})
        self.assertEqual({n for n in dir(second) if not n.startswith("_")},
                         {"connection", "driver", "executor", "pending_recoveries", "recover"})

    def test_the_pending_view_carries_no_address_or_payload(self) -> None:
        sim, control, first, clock, outcome = uncertain_start()
        second = reconnect(control, first)
        text = repr(second.pending_recoveries()) + repr(second) + repr(second.executor)
        self.assertNotIn("192.0.2", text)
        self.assertIsInstance(second.pending_recoveries()[0], PendingRecovery)


if __name__ == "__main__":
    unittest.main()
