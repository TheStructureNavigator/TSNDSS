"""Pure, deterministic image freshness classifier (DB-03 Wave 1, local semantics).

This is a DB-03 definition, not a DSS-CTR-013 requirement and not a
reinterpretation of REQ-058 (which concerns Telemetry samples). It answers one
question: can the latest preview image be treated as the current view?

Verdicts, in precedence order:

1. No image received yet                         -> UNKNOWN (no_image_yet)
2. Host clock is inconsistent with the history   -> UNKNOWN (clock_regression)
3. The stream is known not to be receiving       -> STALE   (stream_not_receiving)
4. Age since host receipt exceeds ``max_age``    -> STALE   (exceeds_max_age)
5. Repeated identical content, see below
6. Otherwise                                     -> FRESH   (within_max_age)

Repeated content. An identical content digest is NOT, by itself, evidence of a
stall: a static or dark scene can legitimately decode to identical bytes.
Repetition only becomes suspicious when the trailing run of identical digests
has at least ``repeat_threshold`` images AND spans at least ``repeat_span`` of
host time. Even then:

* if every image in the run carries a source-reported time and that time does
  not advance, the stall is corroborated -> STALE (stall_corroborated);
* if the source-reported time advances, the source is producing new exposures
  of an unchanged scene -> FRESH;
* if the source reports no time, repetition cannot be corroborated ->
  UNKNOWN (repeated_content_uncorroborated). It is never promoted to STALE.

Boundary convention: an age exactly equal to ``max_age`` is still FRESH.

The defaults below are provisional starting points. They must be calibrated on
real hardware before they are relied on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence

from .preview_models import FreshnessReason, ImageFreshness

__all__ = ["ArrivalRecord", "FreshnessPolicy", "FreshnessVerdict", "classify_image_freshness"]

_ZERO = timedelta(0)


@dataclass(slots=True, frozen=True)
class FreshnessPolicy:
    max_age: timedelta = timedelta(seconds=5)
    repeat_threshold: int = 5
    repeat_span: timedelta = timedelta(seconds=10)

    def __post_init__(self) -> None:
        if not isinstance(self.max_age, timedelta) or self.max_age <= _ZERO:
            raise ValueError("max_age must be a positive timedelta.")
        if isinstance(self.repeat_threshold, bool) or not isinstance(self.repeat_threshold, int) or self.repeat_threshold < 2:
            raise ValueError("repeat_threshold must be an integer of at least 2.")
        if not isinstance(self.repeat_span, timedelta) or self.repeat_span < _ZERO:
            raise ValueError("repeat_span must be a non-negative timedelta.")

    @property
    def history_limit(self) -> int:
        """How many arrivals a stream must retain for the classifier to decide."""
        return max(8, self.repeat_threshold + 2)


@dataclass(slots=True, frozen=True)
class ArrivalRecord:
    """One received image, reduced to what the classifier needs."""

    host_observed_at: datetime
    content_digest: str
    provider_reported_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class FreshnessVerdict:
    freshness: ImageFreshness
    reason: FreshnessReason
    age: timedelta | None
    repeat_count: int
    repeat_span: timedelta


def _trailing_run(records: Sequence[ArrivalRecord]) -> tuple[int, timedelta, Sequence[ArrivalRecord]]:
    last = records[-1].content_digest
    start = len(records) - 1
    while start > 0 and records[start - 1].content_digest == last:
        start -= 1
    run = records[start:]
    return len(run), run[-1].host_observed_at - run[0].host_observed_at, run


def classify_image_freshness(
    history: Sequence[ArrivalRecord],
    now: datetime,
    policy: FreshnessPolicy,
    *,
    stream_receiving: bool = True,
) -> FreshnessVerdict:
    """Classify the latest image of ``history`` (oldest first) at host time ``now``."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware.")
    records = tuple(history)
    if not records:
        return FreshnessVerdict(ImageFreshness.UNKNOWN, FreshnessReason.NO_IMAGE_YET, None, 0, _ZERO)

    times = [record.host_observed_at for record in records]
    if any(later < earlier for earlier, later in zip(times, times[1:])) or times[-1] > now:
        return FreshnessVerdict(ImageFreshness.UNKNOWN, FreshnessReason.CLOCK_REGRESSION, None, 0, _ZERO)

    age = now - times[-1]
    count, span, run = _trailing_run(records)

    if not stream_receiving:
        return FreshnessVerdict(ImageFreshness.STALE, FreshnessReason.STREAM_NOT_RECEIVING, age, count, span)
    if age > policy.max_age:
        return FreshnessVerdict(ImageFreshness.STALE, FreshnessReason.EXCEEDS_MAX_AGE, age, count, span)

    if count >= policy.repeat_threshold and span >= policy.repeat_span:
        source_times = [record.provider_reported_at for record in run]
        if all(stamp is not None for stamp in source_times):
            if max(source_times) == min(source_times):
                return FreshnessVerdict(ImageFreshness.STALE, FreshnessReason.STALL_CORROBORATED, age, count, span)
            return FreshnessVerdict(ImageFreshness.FRESH, FreshnessReason.WITHIN_MAX_AGE, age, count, span)
        return FreshnessVerdict(
            ImageFreshness.UNKNOWN, FreshnessReason.REPEATED_CONTENT_UNCORROBORATED, age, count, span
        )

    return FreshnessVerdict(ImageFreshness.FRESH, FreshnessReason.WITHIN_MAX_AGE, age, count, span)
