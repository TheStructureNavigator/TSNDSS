"""Neutral preview stream manager for one Connection (DB-03 Wave 2, local semantics).

The manager owns at most two ``PreviewStream`` instances for one Connection, at
most one active stream per configured camera, and opens a stream only through an
explicit ``open_stream`` call that passes the readiness gate. It never starts,
stops or configures a camera, never opens a stream on its own, performs no I/O,
starts no thread and sleeps never. Time comes only from the injected clock.

Isolation. Cameras are independent: a fault in one stream, a refusal for one
camera, or an exception while polling or closing one never prevents the other
from being served. Nothing that crosses the manager boundary carries device data
beyond fixed category tokens.

Recovery. A LOST stream is never reactivated. Opening that camera again builds a
new ``PreviewStream`` (new ``stream_id``) after a NEW positive gate decision whose
evidence is strictly newer than the end of the previous stream. Network recovery
alone therefore never re-opens anything.

Freshness. ``view`` re-runs the liveness check at read time and reports
``fresh=True`` only when the liveness check itself succeeded, the stream is
RECEIVING, and the Wave 1 classifier says FRESH. A failed clock or a failed
liveness check can only make the answer less fresh, never more. ``fresh_pixels``
returns pixels only under the same condition.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable, Mapping, Sequence

from .connection import Connection
from .models import ConnectionId, ConnectionState
from .preview_freshness import FreshnessPolicy
from .preview_models import (
    ImageFreshness,
    ImageLimits,
    PreviewImageEvidence,
    PreviewPixels,
    StreamId,
    _require_label,
)
from .preview_readiness import GateDecision, GateReason, ReadinessGate, ReadinessIdentity
from .preview_stream import PreviewSource, PreviewStream, PreviewStreamState
from .support import Clock, IdGenerator, random_id_generator, utc_now

__all__ = [
    "MAX_ACTIVE_STREAMS",
    "CloseReport",
    "OpenOutcome",
    "OpenRefusal",
    "PollResult",
    "PreviewStreamManager",
    "PreviewView",
]

MAX_ACTIVE_STREAMS = 2

_ACTIVE = (PreviewStreamState.OPENING, PreviewStreamState.RECEIVING, PreviewStreamState.STALLED)
_USABLE_CONNECTION = (
    ConnectionState.CONNECTED,
    ConnectionState.READY,
    ConnectionState.BUSY,
    ConnectionState.DEGRADED,
)


class OpenRefusal(Enum):
    UNKNOWN_CAMERA = "unknown_camera"
    CONNECTION_NOT_USABLE = "connection_not_usable"
    ALREADY_ACTIVE = "already_active"
    STREAM_LIMIT = "stream_limit"
    CLOCK_FAILURE = "clock_failure"
    GATE_DENIED = "gate_denied"
    SOURCE_FACTORY_FAILED = "source_factory_failed"
    OPEN_FAILED = "open_failed"


@dataclass(slots=True, frozen=True)
class OpenOutcome:
    source_label: str
    opened: bool
    refusal: OpenRefusal | None = None
    decision: GateDecision | None = None
    stream_id: StreamId | None = None
    error_category: str | None = None


@dataclass(slots=True, frozen=True)
class PollResult:
    source_label: str
    state: PreviewStreamState | None
    evidence: PreviewImageEvidence | None = None
    error_category: str | None = None


@dataclass(slots=True, frozen=True)
class PreviewView:
    """What a consumer may rely on right now. ``fresh`` is never optimistic."""

    source_label: str
    state: PreviewStreamState | None
    evidence: PreviewImageEvidence | None
    fresh: bool
    liveness_checked: bool
    reason: str


@dataclass(slots=True, frozen=True)
class CloseReport:
    closed: tuple[str, ...] = ()
    errors: tuple[tuple[str, str], ...] = ()


class PreviewStreamManager:
    """Not thread-safe. Bound to one Connection for its whole life."""

    def __init__(
        self,
        *,
        connection: Connection,
        gate: ReadinessGate,
        source_factory: Callable[[str], PreviewSource],
        cameras: Sequence[str],
        clock: Clock = utc_now,
        id_generator: IdGenerator = random_id_generator,
        policy: FreshnessPolicy | None = None,
        limits: ImageLimits | None = None,
        max_active_streams: int = MAX_ACTIVE_STREAMS,
    ) -> None:
        labels = tuple(cameras)
        if not 1 <= len(labels) <= MAX_ACTIVE_STREAMS or len(set(labels)) != len(labels):
            raise ValueError("cameras must be one or two distinct labels.")
        for label in labels:
            _require_label("camera label", label)
        if isinstance(max_active_streams, bool) or not isinstance(max_active_streams, int) or not 1 <= max_active_streams <= MAX_ACTIVE_STREAMS:
            raise ValueError("max_active_streams must be 1 or 2.")
        self._connection = connection
        self._gate = gate
        self._factory = source_factory
        self._cameras = labels
        self._clock = clock
        self._ids = id_generator
        self._policy = policy
        self._limits = limits
        self._max_active = max_active_streams
        self._identity = ReadinessIdentity(
            provider_id=connection.provider_id,
            connection_id=connection.connection_id,
            device_ref=connection.device.device_ref,
        )
        self._streams: dict[str, PreviewStream] = {}
        self._ended_at: dict[str, datetime] = {}

    # --- introspection -----------------------------------------------------------------

    @property
    def connection_id(self) -> ConnectionId:
        return self._identity.connection_id

    @property
    def cameras(self) -> tuple[str, ...]:
        return self._cameras

    @property
    def active_labels(self) -> tuple[str, ...]:
        return tuple(label for label in self._cameras if self._is_active(label))

    def states(self) -> Mapping[str, PreviewStreamState | None]:
        return {label: (self._streams[label].state if label in self._streams else None) for label in self._cameras}

    def stream_id(self, label: str) -> StreamId | None:
        stream = self._streams.get(label)
        return stream.stream_id if stream else None

    def __repr__(self) -> str:
        return f"PreviewStreamManager({self._identity.connection_id!r}, active={list(self.active_labels)})"

    # --- internals ---------------------------------------------------------------------

    def _is_active(self, label: str) -> bool:
        stream = self._streams.get(label)
        return stream is not None and stream.state in _ACTIVE

    def _connection_usable(self) -> bool:
        return self._connection.state in _USABLE_CONNECTION

    def _enforce_connection(self) -> bool:
        """Close everything if the Connection can no longer serve streams. True if usable."""
        if self._connection_usable():
            return True
        self.close_all()
        return False

    def _note_end(self, label: str, stream: PreviewStream, fallback: datetime | None = None) -> None:
        """Remember when this camera's stream ended: later evidence must be strictly newer."""
        ends = [t.at for t in stream.transitions if t.to_state in ("lost", "closed")]
        at = ends[0] if ends else fallback
        if at is None:
            try:
                at = self._clock()
            except Exception:
                return
        previous = self._ended_at.get(label)
        if previous is None or at > previous:
            self._ended_at[label] = at

    def _close_stream_object(self, label: str, stream: PreviewStream) -> str | None:
        error: str | None = None
        try:
            stream.close()
        except Exception:
            error = "unexpected_error"
        if error is None and stream.close_error_category is not None:
            error = stream.close_error_category
        self._note_end(label, stream)
        return error

    # --- opening -----------------------------------------------------------------------

    def open_stream(self, label: str) -> OpenOutcome:
        """Open one camera's stream if, and only if, the gate allows it right now. Never raises for refusals."""
        if label not in self._cameras:
            return OpenOutcome(str(label)[:64], False, OpenRefusal.UNKNOWN_CAMERA)
        if not self._enforce_connection():
            return OpenOutcome(label, False, OpenRefusal.CONNECTION_NOT_USABLE)
        if self._is_active(label):
            return OpenOutcome(label, False, OpenRefusal.ALREADY_ACTIVE, stream_id=self._streams[label].stream_id)
        if len(self.active_labels) >= self._max_active:
            return OpenOutcome(label, False, OpenRefusal.STREAM_LIMIT)
        try:
            now = self._clock()
        except Exception:
            return OpenOutcome(label, False, OpenRefusal.CLOCK_FAILURE)

        previous = self._streams.get(label)
        if previous is not None:
            self._note_end(label, previous)  # a LOST or CLOSED stream: evidence must postdate its end
        try:
            decision = self._gate.check(self._identity, label, now, not_before=self._ended_at.get(label))
        except Exception:
            decision = GateDecision(False, GateReason.EVIDENCE_ERROR, label)
        if not decision.allowed:
            return OpenOutcome(label, False, OpenRefusal.GATE_DENIED, decision)
        if previous is not None:  # retired only now; it is never reactivated
            del self._streams[label]
            self._close_stream_object(label, previous)

        try:
            source = self._factory(label)
            if not isinstance(source, PreviewSource):
                raise TypeError("source does not implement PreviewSource")
            stream = PreviewStream(
                provider_id=self._identity.provider_id,
                connection_id=self._identity.connection_id,
                source_label=label,
                source=source,
                clock=self._clock,
                id_generator=self._ids,
                policy=self._policy,
                limits=self._limits,
            )
        except Exception:
            return OpenOutcome(label, False, OpenRefusal.SOURCE_FACTORY_FAILED, decision, error_category="factory_error")

        self._streams[label] = stream
        category: str | None = None
        try:
            stream.open()
        except Exception:
            category = "unexpected_error"
        if stream.state is PreviewStreamState.LOST:
            self._note_end(label, stream, now)
            return OpenOutcome(
                label, False, OpenRefusal.OPEN_FAILED, decision, stream.stream_id, category or stream.last_error_category
            )
        return OpenOutcome(label, True, None, decision, stream.stream_id)

    # --- reading -----------------------------------------------------------------------

    def poll(self, label: str) -> PollResult:
        """Read at most one image from one camera. Faults are reported, never raised."""
        stream = self._streams.get(label)
        if stream is None or label not in self._cameras:
            return PollResult(str(label)[:64], None)
        if not self._enforce_connection():
            return PollResult(label, None)
        if stream.state not in _ACTIVE:
            return PollResult(label, stream.state)
        evidence: PreviewImageEvidence | None = None
        category: str | None = None
        try:
            evidence = stream.poll()
        except Exception:
            category = "unexpected_error"
        if stream.state is PreviewStreamState.LOST:
            category = category or stream.last_error_category
            self._note_end(label, stream)
        return PollResult(label, stream.state, evidence, category)

    def poll_all(self) -> tuple[PollResult, ...]:
        """Poll every camera independently, in configured order."""
        return tuple(self.poll(label) for label in self._cameras)

    def view(self, label: str) -> PreviewView:
        """Current state with a liveness check run now. ``fresh`` is True only on full confirmation."""
        stream = self._streams.get(label)
        if stream is None or label not in self._cameras:
            return PreviewView(str(label)[:64], None, None, False, False, "no_stream")
        if not self._enforce_connection():
            return PreviewView(label, None, None, False, False, "connection_not_usable")
        try:
            now = self._clock()
        except Exception:
            return PreviewView(label, stream.state, None, False, False, "clock_failure")
        try:
            stream.check_liveness(now)
        except Exception:
            return PreviewView(label, stream.state, None, False, False, "liveness_check_failed")
        if stream.state is PreviewStreamState.LOST:
            self._note_end(label, stream)
        try:
            evidence = stream.latest_evidence(now)
        except Exception:
            return PreviewView(label, stream.state, None, False, True, "evidence_unavailable")
        if evidence is None:
            return PreviewView(label, stream.state, None, False, True, "no_image_yet")
        classified_fresh = evidence.freshness is ImageFreshness.FRESH
        fresh = (
            classified_fresh
            and stream.state is PreviewStreamState.RECEIVING
            and evidence.connection_id == self._identity.connection_id
        )
        if fresh:
            reason = "fresh"
        elif classified_fresh:
            reason = f"stream_{stream.state.value}"
        else:
            reason = evidence.freshness_reason.value
        return PreviewView(label, stream.state, evidence, fresh, True, reason)

    def fresh_pixels(self, label: str) -> PreviewPixels | None:
        """Latest pixels, only while ``view`` confirms freshness. Old pixels are never offered as current."""
        view = self.view(label)
        if not view.fresh:
            return None
        stream = self._streams.get(label)
        return stream.latest_pixels() if stream else None

    # --- closing -----------------------------------------------------------------------

    def close_stream(self, label: str) -> CloseReport:
        """Close one camera's stream. Idempotent; never raises for source faults."""
        stream = self._streams.pop(label, None)
        if stream is None:
            return CloseReport()
        error = self._close_stream_object(label, stream)
        return CloseReport((label,), ((label, error),) if error else ())

    def close_all(self) -> CloseReport:
        """Close every stream, each independently. Call on disconnect. Idempotent."""
        closed: list[str] = []
        errors: list[tuple[str, str]] = []
        for label in self._cameras:
            stream = self._streams.pop(label, None)
            if stream is None:
                continue
            error = self._close_stream_object(label, stream)
            closed.append(label)
            if error:
                errors.append((label, error))
        return CloseReport(tuple(closed), tuple(errors))

    def __enter__(self) -> "PreviewStreamManager":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close_all()
