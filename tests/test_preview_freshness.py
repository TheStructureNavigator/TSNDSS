"""DB-03 Wave 1: pure freshness classifier, including boundaries."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from tsn_dss.engine.device_runtime.preview_freshness import (
    ArrivalRecord,
    FreshnessPolicy,
    classify_image_freshness,
)
from tsn_dss.engine.device_runtime.preview_models import FreshnessReason as R
from tsn_dss.engine.device_runtime.preview_models import ImageFreshness as F

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
POLICY = FreshnessPolicy(max_age=timedelta(seconds=5), repeat_threshold=3, repeat_span=timedelta(seconds=4))


def sec(n: float) -> datetime:
    return T0 + timedelta(seconds=n)


def rec(t: float, digest: str = "a", provider: float | None = None) -> ArrivalRecord:
    return ArrivalRecord(sec(t), digest, None if provider is None else sec(provider))


class FreshnessTests(unittest.TestCase):
    def verdict(self, history, now, **kw):
        return classify_image_freshness(history, sec(now), POLICY, **kw)

    def test_no_image_is_unknown(self) -> None:
        v = self.verdict([], 0)
        self.assertEqual((v.freshness, v.reason), (F.UNKNOWN, R.NO_IMAGE_YET))
        self.assertIsNone(v.age)

    def test_age_boundary(self) -> None:
        h = [rec(0)]
        self.assertEqual(self.verdict(h, 5).freshness, F.FRESH)
        v = self.verdict(h, 5.001)
        self.assertEqual((v.freshness, v.reason), (F.STALE, R.EXCEEDS_MAX_AGE))
        self.assertEqual(self.verdict(h, 0).freshness, F.FRESH)

    def test_stream_not_receiving_is_stale(self) -> None:
        v = self.verdict([rec(0)], 1, stream_receiving=False)
        self.assertEqual((v.freshness, v.reason), (F.STALE, R.STREAM_NOT_RECEIVING))

    def test_clock_regression_is_unknown(self) -> None:
        self.assertEqual(self.verdict([rec(10)], 5).reason, R.CLOCK_REGRESSION)
        self.assertEqual(self.verdict([rec(3), rec(2)], 4).reason, R.CLOCK_REGRESSION)

    def test_distinct_content_is_fresh(self) -> None:
        h = [rec(i, str(i)) for i in range(10)]
        self.assertEqual(self.verdict(h, 10).reason, R.WITHIN_MAX_AGE)

    def test_repeated_sha_alone_is_never_stale(self) -> None:
        h = [rec(i, "same") for i in range(8)]
        v = self.verdict(h, 8)
        self.assertEqual((v.freshness, v.reason), (F.UNKNOWN, R.REPEATED_CONTENT_UNCORROBORATED))
        self.assertEqual(v.repeat_count, 8)
        self.assertNotEqual(v.freshness, F.STALE)

    def test_repeat_below_threshold_or_span_is_fresh(self) -> None:
        self.assertEqual(self.verdict([rec(0, "s"), rec(1, "s")], 1).freshness, F.FRESH)
        self.assertEqual(self.verdict([rec(0, "s"), rec(1, "s"), rec(2, "s")], 2).freshness, F.FRESH)  # span 2 < 4
        self.assertEqual(self.verdict([rec(0, "s"), rec(2, "s"), rec(4, "s")], 4).reason, R.REPEATED_CONTENT_UNCORROBORATED)  # span == 4

    def test_repeat_with_frozen_source_time_is_stale(self) -> None:
        h = [rec(i, "s", provider=0) for i in range(6)]
        v = self.verdict(h, 6)
        self.assertEqual((v.freshness, v.reason), (F.STALE, R.STALL_CORROBORATED))

    def test_repeat_with_advancing_source_time_is_fresh(self) -> None:
        h = [rec(i, "s", provider=i) for i in range(6)]
        self.assertEqual(self.verdict(h, 6).freshness, F.FRESH)

    def test_partial_source_time_is_not_corroboration(self) -> None:
        h = [rec(0, "s"), rec(1, "s", provider=0), rec(2, "s", provider=0), rec(3, "s", provider=0), rec(4, "s", provider=0)]
        self.assertEqual(self.verdict(h, 5).freshness, F.UNKNOWN)

    def test_new_content_resets_the_run(self) -> None:
        h = [rec(i, "s") for i in range(6)] + [rec(6, "new")]
        self.assertEqual(self.verdict(h, 6).freshness, F.FRESH)

    def test_precedence_age_over_repetition(self) -> None:
        h = [rec(i, "s", provider=0) for i in range(6)]
        self.assertEqual(self.verdict(h, 50).reason, R.EXCEEDS_MAX_AGE)

    def test_naive_now_rejected(self) -> None:
        with self.assertRaises(ValueError):
            classify_image_freshness([rec(0)], datetime(2026, 1, 1), POLICY)

    def test_policy_validation_and_history_limit(self) -> None:
        for kw in ({"max_age": timedelta(0)}, {"repeat_threshold": 1}, {"repeat_threshold": True}, {"repeat_span": timedelta(seconds=-1)}):
            with self.assertRaises(ValueError):
                FreshnessPolicy(**kw)
        self.assertEqual(FreshnessPolicy().history_limit, 8)
        self.assertEqual(FreshnessPolicy(repeat_threshold=20).history_limit, 22)

    def test_classifier_is_pure(self) -> None:
        h = (rec(0), rec(1))
        self.assertEqual(self.verdict(h, 2), self.verdict(h, 2))


if __name__ == "__main__":
    unittest.main()
