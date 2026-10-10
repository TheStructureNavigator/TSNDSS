"""The issuing connection of one GoTo, kept open until the firmware reports its end (DB-05b v2).

Not an event bus: one object per GoTo, owned by the command provider, reading ONLY the socket that carried the ``scope_goto``
request (the third-party protocol reference says the end of the operation is announced as an unsolicited ``ScopeGoto`` event on
that connection, and that the firmware drops an idle connection after roughly 15 s, which is why a keepalive read is sent).

Event handling is deliberately strict and conservative (envelope per the third-party reference, not vendor-confirmed):

* an event is a JSON object with ``Event`` (or ``event``) equal to ``ScopeGoto`` and a string ``state``;
* ``working`` / ``start`` are progress only;
* ``complete`` / ``fail`` / ``cancel`` count as the END only for an *outer* frame: no ``page`` key and a ``route`` or ``lapse_ms``
  key. A terminal state on any other frame is only provisional, is never used as an end, and is reported as such;
* anything else (other events, non-JSON lines, wrong types, unknown states) is ignored;
* a closed or failing connection, or an oversized frame, ends the watch as ``lost``. Nothing is inferred from a loss.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from ..seestar_provider.protocol import decode_frame, encode_read_request, split_frames

__all__ = ["GotoCompletion", "GotoWatch", "TERMINAL_STATES"]

TERMINAL_STATES = ("complete", "fail", "cancel")
_PROGRESS_STATES = ("working", "start")
_WORD = re.compile(r"^[A-Za-z0-9_.:-]{1,40}$")
_MAX_RECV_PER_PUMP = 64


@dataclass(slots=True, frozen=True)
class GotoCompletion:
    """The firmware-reported end of a GoTo, and when this process saw it. Provider-reported evidence only."""

    state: str  # complete | fail | cancel
    observed_at: datetime
    error: str | None = None
    code: int | None = None
    cur_ra_dec: tuple[float, float] | None = None  # as reported, in the device's own frame; never converted
    dist_deg: float | None = None
    lapse_ms: float | None = None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _pair(value: Any) -> tuple[float, float] | None:
    if isinstance(value, (list, tuple)) and len(value) == 2:
        a, b = _number(value[0]), _number(value[1])
        if a is not None and b is not None:
            return (a, b)
    return None


class GotoWatch:
    def __init__(
        self,
        sock: Any,
        *,
        buffer: bytes,
        clock: Callable[[], datetime],
        monotonic: Callable[[], float],
        next_id: Callable[[], int],
        keepalive_s: float | None,
        recv_slice_s: float = 0.05,
    ) -> None:
        self._sock, self._buffer = sock, buffer
        self._clock, self._monotonic, self._next_id = clock, monotonic, next_id
        self._keepalive_s, self._slice = keepalive_s, recv_slice_s
        self._last_sent = monotonic()
        self.state = "waiting"  # waiting | complete | fail | cancel | lost
        self.completion: GotoCompletion | None = None
        self.progress_seen = False
        self.provisional_state: str | None = None  # a terminal state on a non-outer frame: never an end
        self.lost_reason: str | None = None

    def __repr__(self) -> str:
        return f"GotoWatch(state={self.state!r})"

    # --- reading ---------------------------------------------------------------------------

    def pump(self) -> str:
        """Read what the connection has right now (bounded), keep it alive, and return the state."""
        if self.state != "waiting":
            return self.state
        try:
            self._keepalive()
            self._sock.settimeout(self._slice)
            self._consume()  # bytes already buffered (for example right behind the command reply) are events too
            if self.state != "waiting":
                return self.state
            for _ in range(_MAX_RECV_PER_PUMP):
                try:
                    chunk = self._sock.recv(4096)
                except TimeoutError:  # socket.timeout is this error; no data right now
                    break
                if not chunk:
                    return self._lose("connection_closed")
                self._buffer += chunk
                self._consume()
                if self.state != "waiting":
                    return self.state
        except Exception as exc:  # OSError, a frame that is too large, anything unexpected: the watch is over and nothing is inferred
            category = getattr(exc, "category", None)
            return self._lose(category if isinstance(category, str) else type(exc).__name__)
        return self.state

    def _consume(self) -> None:
        frames, self._buffer = split_frames(self._buffer)
        for raw in frames:
            frame = decode_frame(raw)
            if frame is not None:
                self._handle(frame)
            if self.state != "waiting":
                return

    def _keepalive(self) -> None:
        if self._keepalive_s is None:
            return
        now = self._monotonic()
        if now - self._last_sent >= self._keepalive_s:
            self._sock.sendall(encode_read_request(self._next_id(), "test_connection"))  # the allow-listed read, never a command
            self._last_sent = now

    def _handle(self, frame: dict) -> None:
        name = frame.get("Event", frame.get("event"))
        state = frame.get("state")
        if name != "ScopeGoto" or not isinstance(state, str):
            return
        if state in _PROGRESS_STATES:
            self.progress_seen = True
            return
        if state not in TERMINAL_STATES:
            return
        outer = "page" not in frame and ("route" in frame or "lapse_ms" in frame)
        if not outer:
            self.provisional_state = state
            return
        error = frame.get("error")
        code = frame.get("code")
        self.completion = GotoCompletion(
            state=state,
            observed_at=self._clock(),
            error=error if isinstance(error, str) and _WORD.match(error) else None,
            code=code if isinstance(code, int) and not isinstance(code, bool) else None,
            cur_ra_dec=_pair(frame.get("cur_ra_dec")),
            dist_deg=_number(frame.get("dist_deg")),
            lapse_ms=_number(frame.get("lapse_ms")),
        )
        self.state = state
        self.close()

    def _lose(self, reason: str) -> str:
        self.state, self.lost_reason = "lost", reason
        self.close()
        return self.state

    def close(self) -> None:
        try:
            self._sock.close()
        except Exception:
            pass
