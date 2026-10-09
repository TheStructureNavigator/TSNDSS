"""DB-03 Wave 2: preview stream manager orchestration, isolation, gating and recovery."""

from __future__ import annotations

import unittest
from datetime import timedelta
from unittest import mock

from tsn_dss.engine.device_runtime.models import PreviewAvailability as A
from tsn_dss.engine.device_runtime.preview_manager import MAX_ACTIVE_STREAMS, OpenRefusal as R, PreviewStreamManager
from tsn_dss.engine.device_runtime.preview_readiness import GateReason as G, ReadinessGate
from tsn_dss.engine.device_runtime.preview_simulator import (
    ImageStep,
    ScriptSegment,
    SimulatedPreviewSource,
    scenario_fresh,
    scenario_loss,
    scenario_repeat,
    scenario_stall,
)
from tsn_dss.engine.device_runtime.preview_stream import PreviewStream, PreviewStreamState as S
from tsn_dss.engine.device_runtime.support import SequentialIdGenerator

try:
    from preview_manager_support import CAM_A, CAM_B, Rig
except ModuleNotFoundError:  # pragma: no cover
    from tests.preview_manager_support import CAM_A, CAM_B, Rig


class BoomRead(SimulatedPreviewSource):
    def read(self):
        raise RuntimeError("secret device detail")


class BoomOpen(SimulatedPreviewSource):
    def open(self):
        self.open_calls += 1
        raise RuntimeError("secret device detail")


class BoomClose(SimulatedPreviewSource):
    def close(self):
        super().close()
        raise RuntimeError("secret device detail")


def both_open(rig: Rig):
    a, b = rig.manager.open_stream(CAM_A), rig.manager.open_stream(CAM_B)
    return a, b


class ConstructionTests(unittest.TestCase):
    def test_camera_and_limit_validation(self) -> None:
        rig = Rig()
        kw = dict(connection=rig.connection, gate=rig.manager._gate, source_factory=rig.factory)
        for cameras in ((), ("a", "b", "c"), ("a", "a"), ("bad label",)):
            with self.assertRaises(ValueError, msg=str(cameras)):
                PreviewStreamManager(cameras=cameras, **kw)
        for limit in (0, 3, True):
            with self.assertRaises(ValueError):
                PreviewStreamManager(cameras=("a",), max_active_streams=limit, **kw)
        self.assertEqual(MAX_ACTIVE_STREAMS, 2)

    def test_nothing_opens_on_its_own(self) -> None:
        rig = Rig()
        rig.manager.poll_all()
        rig.manager.view(CAM_A)
        rig.manager.fresh_pixels(CAM_A)
        rig.manager.close_all()
        self.assertEqual((rig.evidence.reads, len(rig.factory.calls)), (0, 0))
        self.assertEqual(rig.manager.active_labels, ())
        self.assertEqual(dict(rig.manager.states()), {CAM_A: None, CAM_B: None})


class OpenAndGateTests(unittest.TestCase):
    def test_two_independent_streams_with_distinct_identities(self) -> None:
        rig = Rig()
        a, b = both_open(rig)
        self.assertTrue(a.opened and b.opened)
        self.assertNotEqual(a.stream_id, b.stream_id)
        self.assertEqual(set(rig.manager.active_labels), {CAM_A, CAM_B})
        results = rig.manager.poll_all()
        self.assertTrue(all(r.evidence is not None for r in results))
        self.assertEqual({r.evidence.connection_id for r in results}, {rig.connection.connection_id})
        self.assertEqual({r.evidence.source_label for r in results}, {CAM_A, CAM_B})
        self.assertTrue(all(r.evidence.simulated for r in results))

    def test_denied_gate_never_builds_or_opens_a_source(self) -> None:
        rig = Rig()
        cases = {
            "unknown": dict(availability=A.UNKNOWN),
            "unavailable": dict(availability=A.UNAVAILABLE),
            "other_connection": dict(connection_id="other-connection"),
            "other_device": dict(device_ref="other-device"),
            "other_provider": dict(provider_id="other-provider"),
            "other_camera": dict(source_label=CAM_B),
        }
        for name, change in cases.items():
            rig.evidence.override[CAM_A] = rig.evidence.live(CAM_A, **change)
            out = rig.manager.open_stream(CAM_A)
            self.assertFalse(out.opened, name)
            self.assertIs(out.refusal, R.GATE_DENIED, name)
            self.assertFalse(out.decision.allowed, name)
        rig.evidence.override[CAM_A] = None
        self.assertEqual(rig.manager.open_stream(CAM_A).decision.reason, G.NO_EVIDENCE)
        rig.evidence.override[CAM_A] = rig.evidence.live(CAM_A)
        rig.advance(11)  # evidence now older than the gate's max age
        self.assertEqual(rig.manager.open_stream(CAM_A).decision.reason, G.STALE_EVIDENCE)
        self.assertEqual(rig.factory.calls, [])
        self.assertEqual(rig.factory.total_open_calls, 0)
        self.assertEqual(rig.manager.active_labels, ())
        self.assertIsNone(rig.manager.stream_id(CAM_A))

    def test_ready_connection_alone_is_not_enough(self) -> None:
        rig = Rig()
        self.assertEqual(rig.connection.state.value, "ready")
        rig.evidence.override[CAM_A] = None
        self.assertFalse(rig.manager.open_stream(CAM_A).opened)
        self.assertEqual(rig.factory.total_open_calls, 0)

    def test_gate_is_consulted_on_every_open_and_never_cached(self) -> None:
        rig = Rig()
        rig.manager.open_stream(CAM_A)
        self.assertEqual(rig.evidence.reads, 1)
        rig.manager.close_stream(CAM_A)
        rig.advance(1)
        rig.evidence.override[CAM_A] = rig.evidence.live(CAM_A, A.UNKNOWN)
        self.assertFalse(rig.manager.open_stream(CAM_A).opened)
        self.assertEqual(rig.evidence.reads, 2)

    def test_one_camera_denial_does_not_block_the_other(self) -> None:
        rig = Rig()
        rig.evidence.availability[CAM_A] = A.UNAVAILABLE
        a, b = both_open(rig)
        self.assertFalse(a.opened)
        self.assertTrue(b.opened)
        self.assertEqual(rig.factory.calls, [CAM_B])

    def test_duplicate_open_is_refused_without_side_effects(self) -> None:
        rig = Rig()
        first = rig.manager.open_stream(CAM_A)
        reads, calls = rig.evidence.reads, list(rig.factory.calls)
        second = rig.manager.open_stream(CAM_A)
        self.assertEqual((second.opened, second.refusal), (False, R.ALREADY_ACTIVE))
        self.assertEqual(second.stream_id, first.stream_id)
        self.assertEqual((rig.evidence.reads, rig.factory.calls), (reads, calls))
        self.assertEqual(rig.manager.active_labels, (CAM_A,))

    def test_stream_limit(self) -> None:
        rig = Rig(max_active=1)
        self.assertTrue(rig.manager.open_stream(CAM_A).opened)
        out = rig.manager.open_stream(CAM_B)
        self.assertEqual((out.opened, out.refusal), (False, R.STREAM_LIMIT))
        self.assertEqual(rig.factory.calls, [CAM_A])
        rig.manager.close_stream(CAM_A)
        rig.advance(1)
        self.assertTrue(rig.manager.open_stream(CAM_B).opened)

    def test_unknown_camera(self) -> None:
        rig = Rig()
        out = rig.manager.open_stream("cam_x")
        self.assertEqual(out.refusal, R.UNKNOWN_CAMERA)
        self.assertEqual((rig.evidence.reads, rig.factory.calls), (0, []))

    def test_clock_failure_refuses_without_reading_evidence(self) -> None:
        rig = Rig()
        rig.fail_clock = True
        out = rig.manager.open_stream(CAM_A)
        self.assertEqual(out.refusal, R.CLOCK_FAILURE)
        self.assertEqual((rig.evidence.reads, rig.factory.calls), (0, []))

    def test_evidence_provider_failure_is_a_denial(self) -> None:
        rig = Rig()
        rig.evidence.override[CAM_A] = RuntimeError("x")
        out = rig.manager.open_stream(CAM_A)
        self.assertEqual((out.refusal, out.decision.reason), (R.GATE_DENIED, G.EVIDENCE_ERROR))
        self.assertEqual(rig.factory.calls, [])

    def test_gate_object_failure_is_a_denial(self) -> None:
        rig = Rig()
        with mock.patch.object(ReadinessGate, "check", side_effect=RuntimeError("x")):
            out = rig.manager.open_stream(CAM_A)
        self.assertEqual((out.refusal, out.decision.reason), (R.GATE_DENIED, G.EVIDENCE_ERROR))
        self.assertEqual(rig.factory.calls, [])


class IsolationTests(unittest.TestCase):
    def test_loss_of_one_camera_does_not_stop_the_other(self) -> None:
        rig = Rig()
        rig.factory.queues[CAM_A] = [scenario_loss(1)]
        both_open(rig)
        rig.manager.poll_all()
        results = {r.source_label: r for r in rig.manager.poll_all()}
        self.assertIs(results[CAM_A].state, S.LOST)
        self.assertEqual(results[CAM_A].error_category, "link_down")
        self.assertIs(results[CAM_B].state, S.RECEIVING)
        self.assertEqual(rig.manager.active_labels, (CAM_B,))
        self.assertTrue(rig.manager.view(CAM_B).fresh)

    def test_unexpected_exception_in_poll_is_contained(self) -> None:
        rig = Rig()
        rig.factory.queues[CAM_A] = [BoomRead([ScriptSegment()])]
        both_open(rig)
        results = {r.source_label: r for r in rig.manager.poll_all()}
        self.assertEqual(results[CAM_A].error_category, "unexpected_error")
        self.assertIs(results[CAM_A].state, S.LOST)
        self.assertNotIn("secret", repr(results[CAM_A]))
        self.assertIsNotNone(results[CAM_B].evidence)
        self.assertEqual(rig.factory.sources[0].close_calls, 1)

    def test_open_failures_are_contained_per_camera(self) -> None:
        rig = Rig()
        rig.factory.queues[CAM_A] = [BoomOpen([ScriptSegment()])]
        a, b = both_open(rig)
        self.assertEqual((a.opened, a.refusal, a.error_category), (False, R.OPEN_FAILED, "unexpected_error"))
        self.assertTrue(b.opened)
        self.assertIs(rig.manager.states()[CAM_A], S.LOST)
        self.assertEqual(rig.factory.sources[0].close_calls, 1)

    def test_source_open_error_reports_its_category(self) -> None:
        rig = Rig()
        rig.factory.queues[CAM_A] = [SimulatedPreviewSource([ScriptSegment(open_failure="unreachable")])]
        out = rig.manager.open_stream(CAM_A)
        self.assertEqual((out.refusal, out.error_category), (R.OPEN_FAILED, "unreachable"))
        self.assertEqual(rig.manager.active_labels, ())

    def test_factory_failure_creates_no_stream(self) -> None:
        rig = Rig()
        rig.factory.fail = RuntimeError("secret device detail")
        out = rig.manager.open_stream(CAM_A)
        self.assertEqual((out.refusal, out.error_category), (R.SOURCE_FACTORY_FAILED, "factory_error"))
        self.assertIsNone(rig.manager.stream_id(CAM_A))
        self.assertNotIn("secret", repr(out))

    def test_factory_returning_a_non_source_is_refused(self) -> None:
        rig = Rig()
        rig.factory.queues[CAM_A] = [object()]
        self.assertEqual(rig.manager.open_stream(CAM_A).refusal, R.SOURCE_FACTORY_FAILED)

    def test_cameras_have_independent_image_streams_and_ids(self) -> None:
        rig = Rig()
        both_open(rig)
        rig.manager.poll(CAM_A)
        self.assertIsNone(rig.manager.view(CAM_B).evidence)
        self.assertEqual(rig.manager.view(CAM_A).evidence.sequence, 1)
        rig.manager.poll(CAM_B)
        self.assertEqual(rig.manager.view(CAM_B).evidence.sequence, 1)
        self.assertNotEqual(rig.manager.stream_id(CAM_A), rig.manager.stream_id(CAM_B))


class CloseTests(unittest.TestCase):
    def test_close_all_closes_every_source_exactly_once_and_is_idempotent(self) -> None:
        rig = Rig()
        both_open(rig)
        report = rig.manager.close_all()
        self.assertEqual((report.closed, report.errors), ((CAM_A, CAM_B), ()))
        self.assertEqual([s.close_calls for s in rig.factory.sources], [1, 1])
        again = rig.manager.close_all()
        self.assertEqual((again.closed, again.errors), ((), ()))
        self.assertEqual([s.close_calls for s in rig.factory.sources], [1, 1])
        self.assertEqual(rig.manager.active_labels, ())

    def test_close_failure_of_one_does_not_block_the_other(self) -> None:
        rig = Rig()
        rig.factory.queues[CAM_A] = [BoomClose([ScriptSegment(steps=(ImageStep(1),))])]
        both_open(rig)
        report = rig.manager.close_all()
        self.assertEqual(report.closed, (CAM_A, CAM_B))
        self.assertEqual(report.errors, ((CAM_A, "unexpected_error"),))
        self.assertEqual([s.close_calls for s in rig.factory.sources], [1, 1])
        self.assertNotIn("secret", repr(report))

    def test_close_stream_is_per_camera_and_idempotent(self) -> None:
        rig = Rig()
        both_open(rig)
        self.assertEqual(rig.manager.close_stream(CAM_A).closed, (CAM_A,))
        self.assertEqual(rig.manager.close_stream(CAM_A).closed, ())
        self.assertEqual(rig.manager.active_labels, (CAM_B,))
        self.assertEqual(rig.factory.sources[0].close_calls, 1)
        self.assertEqual(rig.manager.close_stream("cam_x").closed, ())

    def test_source_close_error_category_is_reported(self) -> None:
        class CloseCategory(SimulatedPreviewSource):
            def close(self):
                super().close()
                from tsn_dss.engine.device_runtime.preview_models import PreviewSourceError
                raise PreviewSourceError("close_failed")
        rig = Rig()
        rig.factory.queues[CAM_A] = [CloseCategory([ScriptSegment()])]
        rig.manager.open_stream(CAM_A)
        self.assertEqual(rig.manager.close_stream(CAM_A).errors, ((CAM_A, "close_failed"),))

    def test_disconnect_closes_everything_and_blocks_opening(self) -> None:
        rig = Rig()
        both_open(rig)
        rig.runtime.disconnect(rig.connection)
        results = rig.manager.poll_all()
        self.assertTrue(all(r.state is None for r in results))
        self.assertEqual([s.close_calls for s in rig.factory.sources], [1, 1])
        reads = rig.evidence.reads
        out = rig.manager.open_stream(CAM_A)
        self.assertEqual(out.refusal, R.CONNECTION_NOT_USABLE)
        self.assertEqual(rig.evidence.reads, reads)
        self.assertEqual(len(rig.factory.calls), 2)
        self.assertFalse(rig.manager.view(CAM_A).fresh)

    def test_view_after_disconnect_also_enforces(self) -> None:
        rig = Rig()
        rig.manager.open_stream(CAM_A)
        rig.runtime.disconnect(rig.connection)
        view = rig.manager.view(CAM_A)
        self.assertEqual((view.fresh, view.reason), (False, "connection_not_usable"))
        self.assertEqual(rig.factory.sources[0].close_calls, 1)

    def test_context_manager_closes_all(self) -> None:
        rig = Rig()
        with rig.manager:
            both_open(rig)
        self.assertEqual([s.close_calls for s in rig.factory.sources], [1, 1])


class RecoveryTests(unittest.TestCase):
    def lose_a(self, rig):
        rig.factory.queues[CAM_A] = [scenario_loss(1), scenario_fresh(5)]
        both_open(rig)
        rig.manager.poll_all(); rig.manager.poll_all()
        self.assertIs(rig.manager.states()[CAM_A], S.LOST)

    def test_lost_stream_is_not_reactivated_and_same_instant_evidence_is_refused(self) -> None:
        rig = Rig()
        self.lose_a(rig)
        old_id = rig.manager.stream_id(CAM_A)
        out = rig.manager.open_stream(CAM_A)  # same host instant as the loss: not newer evidence
        self.assertEqual((out.opened, out.decision.reason), (False, G.PREDATES_LOSS))
        self.assertEqual(rig.factory.calls, [CAM_A, CAM_B])
        self.assertEqual(rig.manager.stream_id(CAM_A), old_id)
        self.assertIs(rig.manager.states()[CAM_A], S.LOST)
        self.assertIs(rig.manager.poll(CAM_A).state, S.LOST)

    def test_recovery_needs_new_positive_evidence_and_creates_a_new_stream(self) -> None:
        rig = Rig()
        self.lose_a(rig)
        old_id = rig.manager.stream_id(CAM_A)
        rig.advance(1)
        out = rig.manager.open_stream(CAM_A)
        self.assertTrue(out.opened)
        self.assertNotEqual(out.stream_id, old_id)
        self.assertEqual(rig.factory.calls, [CAM_A, CAM_B, CAM_A])
        self.assertIs(rig.manager.poll(CAM_A).state, S.RECEIVING)
        self.assertEqual(rig.manager.poll(CAM_A).evidence.sequence, 2)  # a fresh stream counts from 1
        self.assertEqual(rig.factory.sources[0].close_calls, 1)  # the lost source stays released once

    def test_cached_old_evidence_does_not_authorise_recovery(self) -> None:
        rig = Rig()
        rig.factory.queues[CAM_A] = [scenario_loss(1), scenario_fresh(5)]
        cached = rig.evidence.live(CAM_A)
        rig.evidence.override[CAM_A] = cached
        both_open(rig)
        rig.manager.poll_all(); rig.manager.poll_all()
        rig.advance(1)
        out = rig.manager.open_stream(CAM_A)
        self.assertEqual((out.opened, out.decision.reason), (False, G.PREDATES_LOSS))

    def test_recovery_after_loss_is_blocked_by_negative_evidence(self) -> None:
        rig = Rig()
        self.lose_a(rig)
        rig.advance(1)
        for availability in (A.UNKNOWN, A.UNAVAILABLE):
            rig.evidence.override[CAM_A] = rig.evidence.live(CAM_A, availability)
            self.assertFalse(rig.manager.open_stream(CAM_A).opened)
        rig.evidence.override[CAM_A] = None
        self.assertFalse(rig.manager.open_stream(CAM_A).opened)
        self.assertEqual(rig.factory.calls, [CAM_A, CAM_B])

    def test_restored_connection_state_does_not_imply_camera_readiness(self) -> None:
        rig = Rig()
        self.lose_a(rig)
        rig.advance(1)
        self.assertEqual(rig.connection.state.value, "ready")
        rig.evidence.availability[CAM_A] = A.UNKNOWN
        self.assertFalse(rig.manager.open_stream(CAM_A).opened)

    def test_reopen_after_explicit_close_also_needs_new_evidence(self) -> None:
        rig = Rig()
        rig.manager.open_stream(CAM_A)
        rig.manager.close_stream(CAM_A)
        out = rig.manager.open_stream(CAM_A)  # same instant: evidence is not newer than the close
        self.assertEqual(out.decision.reason, G.PREDATES_LOSS)
        rig.advance(1)
        self.assertTrue(rig.manager.open_stream(CAM_A).opened)

    def test_failed_open_then_recovery(self) -> None:
        rig = Rig()
        rig.factory.queues[CAM_A] = [SimulatedPreviewSource([ScriptSegment(open_failure="unreachable")]), scenario_fresh(3)]
        self.assertFalse(rig.manager.open_stream(CAM_A).opened)
        rig.advance(1)
        self.assertTrue(rig.manager.open_stream(CAM_A).opened)


class FreshnessTests(unittest.TestCase):
    def test_fresh_view_then_old_image_is_not_presented_as_fresh(self) -> None:
        rig = Rig()
        rig.manager.open_stream(CAM_A)
        rig.manager.poll(CAM_A)
        view = rig.manager.view(CAM_A)
        self.assertEqual((view.fresh, view.liveness_checked, view.reason), (True, True, "fresh"))
        self.assertIsNotNone(rig.manager.fresh_pixels(CAM_A))
        rig.advance(5)  # exactly max_age: still fresh
        self.assertTrue(rig.manager.view(CAM_A).fresh)
        rig.advance(1)
        view = rig.manager.view(CAM_A)
        self.assertFalse(view.fresh)
        self.assertIs(view.state, S.STALLED)
        self.assertEqual(view.evidence.freshness_reason.value, "stream_not_receiving")
        self.assertIsNone(rig.manager.fresh_pixels(CAM_A))

    def test_staleness_is_detected_without_polling(self) -> None:
        rig = Rig()
        rig.factory.queues[CAM_A] = [scenario_stall(2)]
        rig.manager.open_stream(CAM_A)
        rig.manager.poll(CAM_A); rig.manager.poll(CAM_A)
        rig.advance(60)
        self.assertFalse(rig.manager.view(CAM_A).fresh)
        self.assertIsNone(rig.manager.fresh_pixels(CAM_A))

    def test_stalled_stream_recovers_on_new_image_without_reopening(self) -> None:
        rig = Rig()
        source = SimulatedPreviewSource([ScriptSegment(steps=(ImageStep(1),))])
        rig.factory.queues[CAM_A] = [source]
        rig.manager.open_stream(CAM_A)
        rig.manager.poll(CAM_A)
        rig.advance(9)
        self.assertFalse(rig.manager.view(CAM_A).fresh)
        source._segments = (ScriptSegment(steps=(ImageStep(1), ImageStep(2))),)
        rig.manager.poll(CAM_A)
        self.assertTrue(rig.manager.view(CAM_A).fresh)
        self.assertEqual(len(rig.factory.calls), 1)

    def test_clock_failure_never_confirms_freshness(self) -> None:
        rig = Rig()
        rig.manager.open_stream(CAM_A)
        rig.manager.poll(CAM_A)
        rig.fail_clock = True
        view = rig.manager.view(CAM_A)
        self.assertEqual((view.fresh, view.liveness_checked, view.reason), (False, False, "clock_failure"))
        self.assertIsNone(rig.manager.fresh_pixels(CAM_A))
        rig.fail_clock = False
        self.assertTrue(rig.manager.view(CAM_A).fresh)

    def test_failed_liveness_check_never_confirms_freshness(self) -> None:
        rig = Rig()
        rig.manager.open_stream(CAM_A)
        rig.manager.poll(CAM_A)
        with mock.patch.object(PreviewStream, "check_liveness", side_effect=RuntimeError("x")):
            view = rig.manager.view(CAM_A)
            self.assertEqual((view.fresh, view.liveness_checked, view.reason), (False, False, "liveness_check_failed"))
            self.assertIsNone(rig.manager.fresh_pixels(CAM_A))

    def test_backward_clock_is_not_fresh(self) -> None:
        rig = Rig()
        rig.manager.open_stream(CAM_A)
        rig.manager.poll(CAM_A)
        rig.advance(-30)
        view = rig.manager.view(CAM_A)
        self.assertFalse(view.fresh)
        self.assertEqual(view.reason, "clock_regression")

    def test_repeated_identical_images_are_not_confirmed_fresh(self) -> None:
        rig = Rig()
        rig.factory.queues[CAM_A] = [scenario_repeat(8)]
        rig.manager.open_stream(CAM_A)
        for _ in range(8):
            rig.manager.poll(CAM_A)
            rig.advance(1)
        rig.advance(-1)
        view = rig.manager.view(CAM_A)
        self.assertFalse(view.fresh)
        self.assertEqual(view.reason, "repeated_content_uncorroborated")

    def test_view_without_image_or_stream(self) -> None:
        rig = Rig()
        self.assertEqual(rig.manager.view(CAM_A).reason, "no_stream")
        rig.manager.open_stream(CAM_A)
        view = rig.manager.view(CAM_A)
        self.assertEqual((view.fresh, view.reason), (False, "no_image_yet"))
        self.assertEqual(rig.manager.view("cam_x").reason, "no_stream")

    def test_view_of_lost_stream_is_not_fresh_and_records_the_loss(self) -> None:
        rig = Rig()
        rig.factory.queues[CAM_A] = [scenario_loss(1)]
        rig.manager.open_stream(CAM_A)
        rig.manager.poll(CAM_A); rig.manager.poll(CAM_A)
        view = rig.manager.view(CAM_A)
        self.assertFalse(view.fresh)
        self.assertIs(view.state, S.LOST)
        self.assertIsNone(rig.manager.fresh_pixels(CAM_A))


class DeterminismTests(unittest.TestCase):
    def run_once(self):
        rig = Rig()
        rig.factory.queues[CAM_A] = [scenario_loss(1), scenario_fresh(5)]
        log = [tuple((o.opened, o.refusal, o.stream_id) for o in both_open(rig))]
        for step in range(4):
            log.append(tuple((r.source_label, r.state, r.error_category, r.evidence and r.evidence.content_digest)
                             for r in rig.manager.poll_all()))
            rig.advance(1)
        out = rig.manager.open_stream(CAM_A)
        log.append((out.opened, out.refusal, out.stream_id, out.decision.reason if out.decision else None))
        log.append(tuple((v.fresh, v.reason) for v in (rig.manager.view(CAM_A), rig.manager.view(CAM_B))))
        return log

    def test_same_script_gives_identical_results(self) -> None:
        self.assertEqual(self.run_once(), self.run_once())


class IdempotenceTests(unittest.TestCase):
    def test_poll_without_stream_and_after_close_is_a_noop(self) -> None:
        rig = Rig()
        self.assertIsNone(rig.manager.poll(CAM_A).state)
        rig.manager.open_stream(CAM_A)
        rig.manager.close_stream(CAM_A)
        self.assertIsNone(rig.manager.poll(CAM_A).state)
        self.assertEqual(rig.factory.sources[0].read_calls, 0)

    def test_new_streams_always_have_new_identities(self) -> None:
        rig = Rig()
        ids = []
        for _ in range(3):
            ids.append(rig.manager.open_stream(CAM_A).stream_id)
            rig.manager.close_stream(CAM_A)
            rig.advance(1)
        self.assertEqual(len(set(ids)), 3)

    def test_repr_is_free_of_device_data(self) -> None:
        rig = Rig()
        rig.manager.open_stream(CAM_A)
        self.assertNotIn(rig.connection.device.device_ref, repr(rig.manager))


if __name__ == "__main__":
    unittest.main()
