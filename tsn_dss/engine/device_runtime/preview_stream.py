"""Preview stream lifecycle, pixel buffer and source protocol (DB-03 Wave 1).

DSS-CTR-013 defines Preview as read-only runtime evidence but says nothing about
a stream lifecycle. The states and transitions below are local DB-03 semantics
(see docs/DB-03_PREVIEW_SEMANTICS.md); they are not contract requirements.

A stream receives images from a ``PreviewSource`` that is ALREADY delivering.
It never starts, stops or configures anything on a device: the source protocol
has exactly ``open``, ``read`` and ``close``, and no command of any kind.

Single-threaded and deterministic by design: there is no background thread and
no sleeping. Time comes only from the injected clock. A source's ``read`` must
return promptly (an image, or ``None`` for "nothing available now"); making a
blocking decoder honor that is the job of a concrete source adapter.

Pixels are held in memory only, in a bounded buffer, and are never written to
disk by this module.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import replace
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable

from .errors import InvalidTransition
from .lifecycle import TransitionRecord
from .models import ConnectionId, ProviderId
from .preview_freshness import ArrivalRecord, FreshnessPolicy, classify_image_freshness
from .preview_models import (
    ImageLimits,
    InvalidImage,
    PreviewImageEvidence,
    PreviewPixels,
    PreviewSourceError,
    SourceImage,
    StreamId,
    digest_of,
)
from .support import Clock, IdGenerator, random_id_generator, utc_now

__all__ = [
    "PIXEL_BUFFER_HARD_IMAGE_CEILING",
    "PREVIEW_STREAM_TRANSITIONS",
    "PixelBuffer",
    "PreviewSource",
    "PreviewStream",
    "PreviewStreamEvent",
    "PreviewStreamState",
    "preview_next_state",
]

PIXEL_BUFFER_HARD_IMAGE_CEILING = 16

_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class PreviewStreamState(Enum):
    IDLE = "idle"
    OPENING = "opening"
    RECEIVING = "receiving"
    STALLED = "stalled"
    LOST = "lost"
    CLOSED = "closed"


class PreviewStreamEvent(Enum):
    OPEN_REQUESTED = "open_requested"
    IMAGE_RECEIVED = "image_received"
    LIVENESS_EXCEEDED = "liveness_exceeded"
    SOURCE_LOST = "source_lost"
    CLOSE_REQUESTED = "close_requested"


S = PreviewStreamState
V = PreviewStreamEvent

# Explicit, complete transition table. Anything absent is invalid.
# ``lost`` and ``closed`` are never reactivated: reopening means a new stream with a new stream_id.
PREVIEW_STREAM_TRANSITIONS: dict[tuple[PreviewStreamState, PreviewStreamEvent], PreviewStreamState] = {
    (S.IDLE, V.OPEN_REQUESTED): S.OPENING,
    (S.OPENING, V.IMAGE_RECEIVED): S.RECEIVING,
    (S.OPENING, V.LIVENESS_EXCEEDED): S.STALLED,
    (S.RECEIVING, V.IMAGE_RECEIVED): S.RECEIVING,
    (S.RECEIVING, V.LIVENESS_EXCEEDED): S.STALLED,
    (S.STALLED, V.IMAGE_RECEIVED): S.RECEIVING,
    (S.OPENING, V.SOURCE_LOST): S.LOST,
    (S.RECEIVING, V.SOURCE_LOST): S.LOST,
    (S.STALLED, V.SOURCE_LOST): S.LOST,
    (S.IDLE, V.CLOSE_REQUESTED): S.CLOSED,
    (S.OPENING, V.CLOSE_REQUESTED): S.CLOSED,
    (S.RECEIVING, V.CLOSE_REQUESTED): S.CLOSED,
    (S.STALLED, V.CLOSE_REQUESTED): S.CLOSED,
    (S.LOST, V.CLOSE_REQUESTED): S.CLOSED,
    (S.CLOSED, V.CLOSE_REQUESTED): S.CLOSED,
}


def preview_next_state(state: PreviewStreamState, event: PreviewStreamEvent) -> PreviewStreamState:
    try:
        return PREVIEW_STREAM_TRANSITIONS[(state, event)]
    except KeyError:
        raise InvalidTransition("preview stream", state.value, event.value) from None


@runtime_checkable
class PreviewSource(Protocol):
    """A source of preview images that is already active. It has no command surface."""

    @property
    def simulated(self) -> bool: ...

    def open(self) -> None:
        """Attach to the source. Raise ``PreviewSourceError`` on failure."""
        ...

    def read(self) -> SourceImage | None:
        """Return the next image, ``None`` if none is available now, or raise ``PreviewSourceError``."""
        ...

    def close(self) -> None:
        """Release everything held. Must be safe to call after a fault."""
        ...


class PixelBuffer:
    """Bounded in-memory store of recent pixels, newest last. Oldest entries are evicted first."""

    def __init__(self, limits: ImageLimits) -> None:
        if limits.max_buffer_images > PIXEL_BUFFER_HARD_IMAGE_CEILING:
            raise ValueError("buffer image limit exceeds the hard ceiling.")
        self._limits = limits
        self._items: deque[tuple[int, PreviewPixels]] = deque()
        self._total = 0

    def __len__(self) -> int:
        return len(self._items)

    @property
    def total_bytes(self) -> int:
        return self._total

    @property
    def sequences(self) -> tuple[int, ...]:
        return tuple(sequence for sequence, _pixels in self._items)

    def add(self, sequence: int, pixels: PreviewPixels) -> tuple[int, ...]:
        """Store ``pixels``; return the sequences evicted to make room."""
        size = pixels.byte_length
        if size > self._limits.max_buffer_bytes:
            raise InvalidImage("image_exceeds_buffer")
        evicted: list[int] = []
        while self._items and (
            len(self._items) >= self._limits.max_buffer_images or self._total + size > self._limits.max_buffer_bytes
        ):
            old_sequence, old = self._items.popleft()
            self._total -= old.byte_length
            evicted.append(old_sequence)
        self._items.append((sequence, pixels))
        self._total += size
        return tuple(evicted)

    def latest(self) -> PreviewPixels | None:
        return self._items[-1][1] if self._items else None

    def get(self, sequence: int) -> PreviewPixels | None:
        return next((pixels for seq, pixels in self._items if seq == sequence), None)

    def clear(self) -> None:
        self._items.clear()
        self._total = 0


class PreviewStream:
    """One stream from one source of one Connection. Not thread-safe."""

    def __init__(
        self,
        *,
        provider_id: ProviderId,
        connection_id: ConnectionId,
        source_label: str,
        source: PreviewSource,
        clock: Clock = utc_now,
        id_generator: IdGenerator = random_id_generator,
        policy: FreshnessPolicy | None = None,
        limits: ImageLimits | None = None,
    ) -> None:
        self._provider_id = provider_id
        self._connection_id = connection_id
        self._source_label = source_label
        self._source = source
        self._clock = clock
        self._policy = policy or FreshnessPolicy()
        self._limits = limits or ImageLimits()
        self._stream_id = StreamId(id_generator("stream"))
        self._state = PreviewStreamState.IDLE
        self._history: deque[ArrivalRecord] = deque(maxlen=self._policy.history_limit)
        self._buffer = PixelBuffer(self._limits)
        self._transitions: list[TransitionRecord] = []
        self._sequence = 0
        self._latest: PreviewImageEvidence | None = None
        self._opened_at: datetime | None = None
        self._open_attempted = False
        self._source_released = False
        self._last_error: str | None = None
        self._close_error: str | None = None
        if not isinstance(source_label, str) or not _LABEL_RE.match(source_label):
            raise ValueError("source_label must match " + _LABEL_RE.pattern + ".")

    def __repr__(self) -> str:
        return f"PreviewStream(stream_id={self._stream_id!r}, source_label={self._source_label!r}, state={self._state.value})"

    # --- read-only properties ------------------------------------------------------

    @property
    def stream_id(self) -> StreamId:
        return self._stream_id

    @property
    def state(self) -> PreviewStreamState:
        return self._state

    @property
    def source_label(self) -> str:
        return self._source_label

    @property
    def simulated(self) -> bool:
        return bool(self._source.simulated)

    @property
    def transitions(self) -> tuple[TransitionRecord, ...]:
        return tuple(self._transitions)

    @property
    def last_error_category(self) -> str | None:
        return self._last_error

    @property
    def close_error_category(self) -> str | None:
        return self._close_error

    @property
    def buffer(self) -> PixelBuffer:
        return self._buffer

    # --- lifecycle --------------------------------------------------------------------

    def _apply(self, event: PreviewStreamEvent, at: datetime, evidence: str = "") -> bool:
        new_state = preview_next_state(self._state, event)
        changed = new_state is not self._state
        if changed:
            self._transitions.append(
                TransitionRecord(self._stream_id, self._state.value, event.value, new_state.value, at, evidence)
            )
        self._state = new_state
        return changed

    def _release_source(self, *, swallow: bool = False) -> None:
        """Release the source exactly once, whatever the outcome.

        ``swallow`` is used when a fault is already being handled, so that a failing
        ``close`` cannot mask the original error.
        """
        if self._source_released or not self._open_attempted:
            return
        self._source_released = True
        try:
            self._source.close()
        except PreviewSourceError as exc:
            self._close_error = exc.category
        except Exception:
            self._close_error = "source_error"
            if not swallow:
                raise

    def _lose(self, category: str) -> None:
        self._last_error = category
        self._apply(PreviewStreamEvent.SOURCE_LOST, self._clock(), category)
        self._release_source(swallow=True)

    def open(self) -> PreviewStreamState:
        """Attach to the source. A source fault makes the stream ``lost``; it does not raise."""
        at = self._clock()
        self._apply(PreviewStreamEvent.OPEN_REQUESTED, at)
        self._opened_at = at
        self._open_attempted = True
        try:
            self._source.open()
        except PreviewSourceError as exc:
            self._lose(exc.category)
        except Exception:
            self._lose("source_error")
            raise
        return self._state

    def poll(self) -> PreviewImageEvidence | None:
        """Read at most one image. Source faults become ``lost``; they do not raise."""
        if self._state not in (PreviewStreamState.OPENING, PreviewStreamState.RECEIVING, PreviewStreamState.STALLED):
            raise InvalidTransition("preview stream", self._state.value, "poll")
        try:
            item = self._source.read()
        except PreviewSourceError as exc:
            self._lose(exc.category)
            return None
        except Exception:
            self._lose("source_error")
            raise
        now = self._clock()
        if item is None:
            self.check_liveness(now)
            return None
        return self._ingest(item, now)

    def _ingest(self, item: SourceImage, now: datetime) -> PreviewImageEvidence | None:
        candidate = self._sequence + 1
        try:
            if not isinstance(item, SourceImage):
                raise InvalidImage("pixels_missing")
            item.pixels.check_limits(self._limits)
            self._buffer.add(candidate, item.pixels)
        except InvalidImage:
            self._lose("invalid_image")
            return None
        self._sequence = candidate
        digest = digest_of(item.pixels.data)
        self._history.append(ArrivalRecord(now, digest, item.provider_reported_at))
        self._apply(PreviewStreamEvent.IMAGE_RECEIVED, now)
        verdict = classify_image_freshness(tuple(self._history), now, self._policy, stream_receiving=True)
        evidence = PreviewImageEvidence(
            stream_id=self._stream_id,
            provider_id=self._provider_id,
            connection_id=self._connection_id,
            source_label=self._source_label,
            sequence=self._sequence,
            host_observed_at=now,
            width=item.pixels.width,
            height=item.pixels.height,
            pixel_format=item.pixels.pixel_format,
            byte_length=item.pixels.byte_length,
            content_digest=digest,
            freshness=verdict.freshness,
            freshness_reason=verdict.reason,
            simulated=self.simulated,
            provider_reported_at=item.provider_reported_at,
        )
        self._latest = evidence
        return evidence

    def check_liveness(self, now: datetime | None = None) -> PreviewStreamState:
        """Mark the stream ``stalled`` when nothing arrived within ``max_age``. Otherwise a no-op."""
        if self._state not in (PreviewStreamState.OPENING, PreviewStreamState.RECEIVING):
            return self._state
        now = now or self._clock()
        reference = self._history[-1].host_observed_at if self._history else self._opened_at
        if reference is not None and now - reference > self._policy.max_age:
            self._apply(PreviewStreamEvent.LIVENESS_EXCEEDED, now, "no_image_within_max_age")
        return self._state

    def close(self) -> PreviewStreamState:
        """Idempotent. Releases the source once, clears pixels, never raises for source faults."""
        if self._state is PreviewStreamState.CLOSED:
            return self._state
        self._apply(PreviewStreamEvent.CLOSE_REQUESTED, self._clock())
        try:
            self._release_source()
        finally:
            self._buffer.clear()
        return self._state

    def __enter__(self) -> "PreviewStream":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # --- evidence and pixel access ----------------------------------------------------------

    def latest_evidence(self, now: datetime | None = None) -> PreviewImageEvidence | None:
        """The latest image's metadata with freshness re-evaluated at ``now`` (default: clock)."""
        if self._latest is None:
            return None
        now = now or self._clock()
        receiving = self._state is PreviewStreamState.RECEIVING
        verdict = classify_image_freshness(tuple(self._history), now, self._policy, stream_receiving=receiving)
        return replace(self._latest, freshness=verdict.freshness, freshness_reason=verdict.reason)

    def latest_pixels(self) -> PreviewPixels | None:
        return self._buffer.latest()

    def pixels_for(self, sequence: int) -> PreviewPixels | None:
        return self._buffer.get(sequence)
