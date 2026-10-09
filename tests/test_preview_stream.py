"""DB-03 Wave 1: stream state model, buffer, and simulator scenarios."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from tsn_dss.engine.device_runtime.errors import InvalidTransition
from tsn_dss.engine.device_runtime.preview_freshness import FreshnessPolicy
from tsn_dss.engine.device_runtime.preview_models import (
    FreshnessReason as R,
    ImageFreshness as F,
    ImageLimits,
    InvalidImage,
    PixelFormat,
    PreviewSourceError,
)
from tsn_dss.engine.device_runtime.preview_simulator import (
    ImageStep,
    ScriptSegment,
    SimulatedPreviewSource,
    make_pixels,
    scenario_fresh,
    scenario_loss,
    scenario_reopen,
    scenario_repeat,
    scenario_stall,
)
from tsn_dss.engine.device_runtime.preview_stream import (
    PREVIEW_STREAM_TRANSITIONS,
    PixelBuffer,
    PreviewSource,
    PreviewStream,
    PreviewStreamEvent as V,
    PreviewStreamState as S,
    preview_next_state,
)
from tsn_dss.engine.device_runtime.support import ManualClock, SequentialIdGenerator

POLICY = FreshnessPolicy(max_age=timedelta(seconds=5), repeat_threshold=3, repeat_span=timedelta(seconds=4))
ORIGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_stream(source, *, step=1.0, limits=None, policy=POLICY, label="cam_a", ids=None):
    clock = ManualClock(step=timedelta(seconds=step))
    stream = PreviewStream(
        provider_id="prov", connection_id="conn-1", source_label=label, source=source,
        clock=clock, id_generator=ids or SequentialIdGenerator(), policy=policy, limits=limits,
    )
    return stream, clock


class TransitionTableTests(unittest.TestCase):
    def test_table_is_exactly_the_documented_set(self) -> None:
        self.assertEqual(len(PREVIEW_STREAM_TRANSITIONS), 15)

    def test_every_pair_is_either_allowed_or_invalid(self) -> None:
        for state in S:
            for event in V:
                if (state, event) in PREVIEW_STREAM_TRANSITIONS:
                    preview_next_state(state, event)
                else:
                    with self.assertRaises(InvalidTransition):
                        preview_next_state(state, event)

    def test_lost_and_closed_are_never_reactivated(self) -> None:
        for terminal in (S.LOST, S.CLOSED):
            for event in (V.OPEN_REQUESTED, V.IMAGE_RECEIVED, V.LIVENESS_EXCEEDED):
                self.assertNotIn((terminal, event), PREVIEW_STREAM_TRANSITIONS)
        self.assertNotIn((S.CLOSED, V.SOURCE_LOST), PREVIEW_STREAM_TRANSITIONS)

    def test_idle_cannot_receive(self) -> None:
        self.assertNotIn((S.IDLE, V.IMAGE_RECEIVED), PREVIEW_STREAM_TRANSITIONS)


class SourceProtocolTests(unittest.TestCase):
    def test_protocol_has_no_command_surface(self) -> None:
        public = {n for n in dir(PreviewSource) if not n.startswith("_")}
        self.assertEqual(public, {"simulated", "open", "read", "close"})

    def test_simulator_satisfies_protocol_and_is_simulated(self) -> None:
        src = scenario_fresh()
        self.assertIsInstance(src, PreviewSource)
        self.assertTrue(src.simulated)


class StreamScenarioTests(unittest.TestCase):
    def test_fresh_images(self) -> None:
        stream, _ = make_stream(scenario_fresh(3))
        self.assertIs(stream.state, S.IDLE)
        self.assertIs(stream.open(), S.OPENING)
        evs = [stream.poll() for _ in range(3)]
        self.assertIs(stream.state, S.RECEIVING)
        self.assertEqual([e.sequence for e in evs], [1, 2, 3])
        self.assertTrue(all(e.freshness is F.FRESH and e.simulated for e in evs))
        self.assertEqual(len({e.content_digest for e in evs}), 3)
        self.assertEqual(stream.latest_pixels(), make_pixels(2))
        self.assertIsNone(stream.pixels_for(99))

    def test_evidence_time_is_host_receipt(self) -> None:
        stream, clock = make_stream(scenario_fresh(1, with_provider_time=True, origin=ORIGIN - timedelta(days=1)))
        stream.open()
        ev = stream.poll()
        self.assertGreater(ev.host_observed_at, ORIGIN)
        self.assertEqual(ev.provider_reported_at, ORIGIN - timedelta(days=1))
        self.assertEqual(ev.time_basis, "host_receipt")

    def test_repeat_without_source_time_is_never_stale(self) -> None:
        stream, _ = make_stream(scenario_repeat(8))
        stream.open()
        evs = [stream.poll() for _ in range(8)]
        self.assertTrue(all(e.freshness is not F.STALE for e in evs))
        self.assertIs(evs[-1].freshness, F.UNKNOWN)
        self.assertIs(evs[-1].freshness_reason, R.REPEATED_CONTENT_UNCORROBORATED)

    def test_repeat_with_frozen_source_time_is_stale(self) -> None:
        stream, _ = make_stream(scenario_repeat(8, provider_time=ORIGIN))
        stream.open()
        evs = [stream.poll() for _ in range(8)]
        self.assertIs(evs[-1].freshness, F.STALE)
        self.assertIs(evs[-1].freshness_reason, R.STALL_CORROBORATED)
        self.assertIs(stream.state, S.RECEIVING)

    def test_repeat_with_advancing_source_time_is_fresh(self) -> None:
        stream, _ = make_stream(scenario_repeat(8, provider_time=ORIGIN, advancing=True))
        stream.open()
        evs = [stream.poll() for _ in range(8)]
        self.assertIs(evs[-1].freshness, F.FRESH)

    def test_stall_by_silence_then_stale(self) -> None:
        stream, clock = make_stream(scenario_stall(2), step=0.0)
        stream.open()
        stream.poll(); stream.poll()
        self.assertIs(stream.state, S.RECEIVING)
        clock.advance(timedelta(seconds=5))
        self.assertIsNone(stream.poll())
        self.assertIs(stream.state, S.RECEIVING)  # exactly max_age: not yet stalled
        clock.advance(timedelta(seconds=0.5))
        self.assertIsNone(stream.poll())
        self.assertIs(stream.state, S.STALLED)
        ev = stream.latest_evidence()
        self.assertEqual((ev.freshness, ev.freshness_reason), (F.STALE, R.STREAM_NOT_RECEIVING))
        self.assertEqual(stream.latest_pixels(), make_pixels(1))  # last pixels remain, marked stale

    def test_stalled_stream_recovers_on_new_image(self) -> None:
        src = SimulatedPreviewSource([ScriptSegment(steps=(ImageStep(1),))])
        stream, clock = make_stream(src, step=0.0)
        stream.open(); stream.poll()
        clock.advance(timedelta(seconds=9))
        stream.poll()
        self.assertIs(stream.state, S.STALLED)
        src._segments = (ScriptSegment(steps=(ImageStep(1), ImageStep(2))),)  # script a late image
        src._cursor = 1
        ev = stream.poll()
        self.assertIs(stream.state, S.RECEIVING)
        self.assertIs(ev.freshness, F.FRESH)

    def test_silence_before_first_image_stalls_opening(self) -> None:
        stream, clock = make_stream(SimulatedPreviewSource([ScriptSegment()]), step=0.0)
        stream.open()
        clock.advance(timedelta(seconds=6))
        stream.poll()
        self.assertIs(stream.state, S.STALLED)
        self.assertIsNone(stream.latest_evidence())

    def test_loss_releases_source_once_and_never_raises(self) -> None:
        src = scenario_loss(2, "link_down")
        stream, _ = make_stream(src)
        stream.open(); stream.poll(); stream.poll()
        self.assertIsNone(stream.poll())
        self.assertIs(stream.state, S.LOST)
        self.assertEqual(stream.last_error_category, "link_down")
        self.assertEqual(src.close_calls, 1)
        ev = stream.latest_evidence()
        self.assertEqual((ev.freshness, ev.freshness_reason), (F.STALE, R.STREAM_NOT_RECEIVING))
        with self.assertRaises(InvalidTransition):
            stream.poll()
        stream.close()
        self.assertIs(stream.state, S.CLOSED)
        self.assertEqual(src.close_calls, 1)

    def test_reopen_is_a_new_stream(self) -> None:
        src = scenario_reopen(1, 2)
        ids = SequentialIdGenerator()
        first, _ = make_stream(src, ids=ids)
        first.open(); first.poll(); first.poll()
        self.assertIs(first.state, S.LOST)
        first.close()
        second, _ = make_stream(src, ids=ids)
        second.open()
        self.assertNotEqual(first.stream_id, second.stream_id)
        evs = [second.poll(), second.poll()]
        self.assertIs(second.state, S.RECEIVING)
        self.assertEqual([e.sequence for e in evs], [1, 2])
        self.assertEqual(src.open_calls, 2)
        with self.assertRaises(InvalidTransition):
            first.open()

    def test_stream_ids_distinct_with_shared_generator(self) -> None:
        ids = SequentialIdGenerator()
        a = PreviewStream(provider_id="p", connection_id="c", source_label="a", source=scenario_fresh(), id_generator=ids)
        b = PreviewStream(provider_id="p", connection_id="c", source_label="a", source=scenario_fresh(), id_generator=ids)
        self.assertNotEqual(a.stream_id, b.stream_id)

    def test_open_failure_is_lost_not_raised(self) -> None:
        src = SimulatedPreviewSource([ScriptSegment(open_failure="unreachable")])
        stream, _ = make_stream(src)
        self.assertIs(stream.open(), S.LOST)
        self.assertEqual(stream.last_error_category, "unreachable")
        self.assertEqual(src.close_calls, 1)

    def test_unexpected_source_exception_marks_lost_and_propagates(self) -> None:
        class Boom(SimulatedPreviewSource):
            def read(self):
                raise RuntimeError("secret detail")
        stream, _ = make_stream(Boom([ScriptSegment()]))
        stream.open()
        with self.assertRaises(RuntimeError):
            stream.poll()
        self.assertIs(stream.state, S.LOST)
        self.assertEqual(stream.last_error_category, "source_error")
        self.assertNotIn("secret", repr(stream))

    def test_close_is_idempotent_and_clears_pixels(self) -> None:
        src = scenario_fresh(2)
        stream, _ = make_stream(src)
        stream.open(); stream.poll()
        self.assertIsNotNone(stream.latest_pixels())
        stream.close(); stream.close()
        self.assertIs(stream.state, S.CLOSED)
        self.assertEqual(src.close_calls, 1)
        self.assertIsNone(stream.latest_pixels())
        self.assertEqual(len(stream.buffer), 0)

    def test_close_without_open_never_touches_source(self) -> None:
        src = scenario_fresh()
        stream, _ = make_stream(src)
        stream.close()
        self.assertEqual((src.open_calls, src.close_calls), (0, 0))

    def test_context_manager_closes(self) -> None:
        src = scenario_fresh(1)
        stream, _ = make_stream(src)
        with stream:
            stream.open(); stream.poll()
        self.assertIs(stream.state, S.CLOSED)
        self.assertEqual(src.close_calls, 1)

    def test_poll_requires_open(self) -> None:
        stream, _ = make_stream(scenario_fresh())
        with self.assertRaises(InvalidTransition):
            stream.poll()

    def test_double_open_rejected(self) -> None:
        stream, _ = make_stream(scenario_fresh())
        stream.open()
        with self.assertRaises(InvalidTransition):
            stream.open()

    def test_transition_log_records_changes_only(self) -> None:
        stream, _ = make_stream(scenario_fresh(3))
        stream.open()
        for _ in range(3):
            stream.poll()
        stream.close()
        self.assertEqual([t.to_state for t in stream.transitions], ["opening", "receiving", "closed"])

    def test_invalid_label_rejected(self) -> None:
        with self.assertRaises(ValueError):
            make_stream(scenario_fresh(), label="rtsp://x/y")

    def test_history_is_bounded(self) -> None:
        stream, _ = make_stream(scenario_fresh(40))
        stream.open()
        for _ in range(40):
            stream.poll()
        self.assertLessEqual(len(stream._history), POLICY.history_limit)


class ExceptionSafetyTests(unittest.TestCase):
    """Every exception path leaves one unambiguous state and releases the source exactly once."""

    @staticmethod
    def faulty(read=None, close=None, open_=None, images=1):
        class Faulty(SimulatedPreviewSource):
            def open(self):
                if open_ is not None:
                    self.open_calls += 1
                    self._is_open = True
                    raise open_
                super().open()

            def read(self):
                if read is not None and self.read_calls >= images:
                    self.read_calls += 1
                    raise read
                return super().read()

            def close(self):
                super().close()
                if close is not None:
                    raise close

        return Faulty([ScriptSegment(steps=tuple(ImageStep(i) for i in range(5)))])

    def test_unexpected_read_exception_is_lost_released_once_and_propagates(self) -> None:
        src = self.faulty(read=RuntimeError("x"))
        stream, _ = make_stream(src)
        stream.open(); stream.poll()
        with self.assertRaises(RuntimeError):
            stream.poll()
        self.assertIs(stream.state, S.LOST)
        self.assertEqual(src.close_calls, 1)
        with self.assertRaises(InvalidTransition):
            stream.poll()
        self.assertEqual(src.read_calls, 2)
        stream.close(); stream.close()
        self.assertIs(stream.state, S.CLOSED)
        self.assertEqual(src.close_calls, 1)

    def test_failing_close_does_not_mask_the_original_exception(self) -> None:
        src = self.faulty(read=RuntimeError("original"), close=ValueError("masking"))
        stream, _ = make_stream(src)
        stream.open(); stream.poll()
        with self.assertRaises(RuntimeError):
            stream.poll()
        self.assertIs(stream.state, S.LOST)
        self.assertEqual(stream.close_error_category, "source_error")
        self.assertEqual(src.close_calls, 1)
        stream.close()
        self.assertEqual(src.close_calls, 1)

    def test_source_error_with_failing_close_records_both_categories(self) -> None:
        src = self.faulty(read=PreviewSourceError("link_down"), close=PreviewSourceError("close_failed"))
        stream, _ = make_stream(src)
        stream.open(); stream.poll()
        self.assertIsNone(stream.poll())
        self.assertIs(stream.state, S.LOST)
        self.assertEqual((stream.last_error_category, stream.close_error_category), ("link_down", "close_failed"))
        self.assertEqual(src.close_calls, 1)

    def test_close_with_unexpected_close_exception_still_closes_and_clears(self) -> None:
        src = self.faulty(close=RuntimeError("x"))
        stream, _ = make_stream(src)
        stream.open(); stream.poll()
        with self.assertRaises(RuntimeError):
            stream.close()
        self.assertIs(stream.state, S.CLOSED)
        self.assertEqual(len(stream.buffer), 0)
        stream.close()
        self.assertEqual(src.close_calls, 1)

    def test_close_with_source_error_does_not_raise(self) -> None:
        src = self.faulty(close=PreviewSourceError("close_failed"))
        stream, _ = make_stream(src)
        stream.open()
        self.assertIs(stream.close(), S.CLOSED)
        self.assertEqual(stream.close_error_category, "close_failed")

    def test_unexpected_open_exception_is_lost_released_once(self) -> None:
        src = self.faulty(open_=RuntimeError("x"))
        stream, _ = make_stream(src)
        with self.assertRaises(RuntimeError):
            stream.open()
        self.assertIs(stream.state, S.LOST)
        self.assertEqual(src.close_calls, 1)
        stream.close()
        self.assertEqual(src.close_calls, 1)

    def test_clock_failure_in_poll_leaves_state_and_source_releasable(self) -> None:
        calls = {"n": 0}

        def clock():
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("clock")
            return ORIGIN

        src = scenario_fresh(3)
        stream = PreviewStream(provider_id="p", connection_id="c", source_label="a", source=src, clock=clock,
                               id_generator=SequentialIdGenerator(), policy=POLICY)
        stream.open()
        self.assertIs(stream.state, S.OPENING)
        with self.assertRaises(RuntimeError):
            stream.poll()
        self.assertIs(stream.state, S.OPENING)
        self.assertEqual(len(stream.buffer), 0)
        self.assertEqual(src.close_calls, 0)
        with self.assertRaises(RuntimeError):
            stream.close()  # clock still failing; nothing was released or corrupted
        self.assertEqual(src.close_calls, 0)
        calls["n"] = 0
        stream.close()
        self.assertIs(stream.state, S.CLOSED)
        self.assertEqual(src.close_calls, 1)

    def test_invalid_image_kinds_each_make_the_stream_lost_once(self) -> None:
        limits = ImageLimits(max_width=4, max_height=4, max_image_bytes=16, max_buffer_images=2, max_buffer_bytes=16)
        cases = {
            "wrong_type": object(),
            "too_wide": ImageStep(1, 5, 1).image(),
            "too_many_bytes": ImageStep(1, 4, 2, PixelFormat.RGB8).image(),
        }
        for name, bad in cases.items():
            with self.subTest(name):
                class Bad(SimulatedPreviewSource):
                    def read(self):
                        return bad
                src = Bad([ScriptSegment()])
                stream, _ = make_stream(src, limits=limits)
                stream.open()
                self.assertIsNone(stream.poll())
                self.assertIs(stream.state, S.LOST)
                self.assertEqual(stream.last_error_category, "invalid_image")
                self.assertEqual((len(stream.buffer), src.close_calls), (0, 1))
                self.assertIsNone(stream.latest_evidence())


class LimitAndBufferTests(unittest.TestCase):
    LIMITS = ImageLimits(max_width=4, max_height=4, max_image_bytes=16, max_buffer_images=2, max_buffer_bytes=32)

    def test_oversize_image_makes_stream_lost_and_is_not_stored(self) -> None:
        src = SimulatedPreviewSource([ScriptSegment(steps=(ImageStep(1, 4, 4), ImageStep(2, 5, 5)))])
        stream, _ = make_stream(src, limits=self.LIMITS)
        stream.open()
        self.assertEqual(stream.poll().sequence, 1)
        self.assertIsNone(stream.poll())
        self.assertIs(stream.state, S.LOST)
        self.assertEqual(stream.last_error_category, "invalid_image")
        self.assertEqual(stream.buffer.sequences, (1,))
        self.assertEqual(src.close_calls, 1)

    def test_sequence_is_not_consumed_by_rejected_image(self) -> None:
        stream, _ = make_stream(SimulatedPreviewSource([ScriptSegment(steps=(ImageStep(1, 4, 4), ImageStep(2, 9, 9)))]), limits=self.LIMITS)
        stream.open(); stream.poll(); stream.poll()
        self.assertEqual(stream._sequence, 1)

    def test_non_source_image_object_is_rejected(self) -> None:
        class Weird(SimulatedPreviewSource):
            def read(self):
                return object()
        stream, _ = make_stream(Weird([ScriptSegment()]))
        stream.open()
        self.assertIsNone(stream.poll())
        self.assertEqual(stream.last_error_category, "invalid_image")

    def test_buffer_evicts_oldest_by_count(self) -> None:
        buf = PixelBuffer(self.LIMITS)
        for seq in (1, 2):
            self.assertEqual(buf.add(seq, make_pixels(seq, 2, 2)), ())
        self.assertEqual(buf.add(3, make_pixels(3, 2, 2)), (1,))
        self.assertEqual(buf.sequences, (2, 3))
        self.assertIsNone(buf.get(1))
        self.assertEqual(buf.total_bytes, 8)

    def test_buffer_evicts_by_bytes(self) -> None:
        limits = ImageLimits(max_width=8, max_height=8, max_image_bytes=20, max_buffer_images=8, max_buffer_bytes=32)
        buf = PixelBuffer(limits)
        buf.add(1, make_pixels(1, 4, 4)); 
        buf.add(2, make_pixels(2, 4, 4))
        self.assertEqual(buf.total_bytes, 32)
        self.assertEqual(buf.add(3, make_pixels(3, 4, 4)), (1,))
        self.assertLessEqual(buf.total_bytes, 32)

    def test_buffer_rejects_image_larger_than_buffer(self) -> None:
        small = ImageLimits(max_width=8, max_height=8, max_image_bytes=16, max_buffer_images=2, max_buffer_bytes=16)
        buf = PixelBuffer(small)
        with self.assertRaises(InvalidImage):
            buf.add(1, make_pixels(0, 5, 5))
        self.assertEqual(len(buf), 0)

    def test_pixels_are_not_written_to_disk(self) -> None:
        import builtins
        opened = []
        real_open = builtins.open
        builtins.open = lambda *a, **k: (opened.append(a), real_open(*a, **k))[1]  # type: ignore[assignment]
        try:
            stream, _ = make_stream(scenario_fresh(3))
            stream.open()
            for _ in range(3):
                stream.poll()
            stream.close()
        finally:
            builtins.open = real_open  # type: ignore[assignment]
        self.assertEqual(opened, [])


if __name__ == "__main__":
    unittest.main()
