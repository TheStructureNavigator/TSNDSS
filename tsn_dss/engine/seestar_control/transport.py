"""Control transport: four allow-listed commands over short-lived, authenticated TCP connections (DB-05 Slice 1).

Reuses the DB-02 primitives: the handshake (``auth.perform_handshake``), the framing (``protocol.split_frames`` /
``decode_frame``) and the bounded frame reader and sender of ``TcpSeestarTransport``, which this class extends without
modifying it. The only way to put a command on the wire is ``send_command(host, kind_id)``: the wire method and parameters come
from the fixed table in ``commands``; there is no generic send, no caller parameters and no unauthenticated mode.

Failure phases (what the caller may conclude):

* ``ControlPreSendError``: raised for anything that fails before the command frame is handed to the socket (unknown kind,
  connect failure, any handshake failure, any error before the sending flag is set). The command was structurally not sent.
* ``ControlPostSendError``: raised for anything from the moment the command frame is about to be written: a send error or
  timeout, a missing or late reply, a closed connection, a malformed or unmatched reply. The device may have acted.
* A reply is returned (``RpcReply``) when one arrives; whether its ``code`` is zero is NOT a statement about physical effects.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

from ..seestar_provider.auth import Authenticator, perform_handshake, require_mapping
from ..seestar_provider.config import SeestarProviderConfig
from ..seestar_provider.errors import SeestarConfigError, SeestarProtocolError, SeestarTimeout, SeestarUnreachable
from ..seestar_provider.protocol import FRAME_TERMINATOR, MAX_SKIPPED_FRAMES, RpcReply, decode_frame, parse_reply, split_frames
from ..seestar_provider.transport import ConnectFactory, TcpSeestarTransport
from .commands import COMMANDS, GotoTarget, goto_wire_message, wire_message
from .goto_watch import GotoWatch
from ..device_runtime.support import utc_now
from .errors import ControlPostSendError, ControlPreSendError

__all__ = ["SeestarControlTransport"]

# Keepalive for the connection kept open during a GoTo. A protocol parameter, not a safety window: the third-party reference
# pings every 5 s and measured an idle drop after about 15.5 s on firmware 8.89; neither is verified on 9.31. None disables it.
GOTO_KEEPALIVE_S = 5.0
_COMMAND_ID_START = 2000  # distinct from the handshake ids (1001-1003)


class SeestarControlTransport(TcpSeestarTransport):
    def __init__(
        self,
        config: SeestarProviderConfig,
        authenticator: Authenticator,
        *,
        connect_factory: ConnectFactory | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], Any] = utc_now,
        goto_keepalive_s: float | None = GOTO_KEEPALIVE_S,
    ) -> None:
        self._clock, self._goto_keepalive_s = clock, goto_keepalive_s
        if authenticator is None:
            raise SeestarConfigError("authenticator_required")
        super().__init__(config, authenticator=authenticator, connect_factory=connect_factory, monotonic=monotonic)
        self._lock = threading.Lock()
        self._command_id = _COMMAND_ID_START

    def __repr__(self) -> str:
        return "SeestarControlTransport(<redacted>)"

    def send_command(self, host: str, kind_id: str) -> RpcReply:
        """Send one allow-listed command and return the device's reply. Raises a phase-tagged error otherwise."""
        command = COMMANDS.get(kind_id)
        if command is None:
            raise ControlPreSendError("command_not_allowed")
        return self._send_wire(host, lambda message_id: wire_message(command, message_id))

    def open_goto(self, host: str, target: GotoTarget) -> tuple[RpcReply, GotoWatch]:
        """Send one GoTo for an already validated target and KEEP the issuing connection open in a ``GotoWatch`` (the end of the slew is
        announced on that connection). Nothing else can be sent with a caller-supplied value. Failure phases are those of send_command."""
        if not isinstance(target, GotoTarget):
            raise ControlPreSendError("target_invalid")
        reply, sock, leftover = self._send_wire(host, lambda message_id: goto_wire_message(target, message_id), keep=True)
        watch = GotoWatch(sock, buffer=leftover, clock=self._clock, monotonic=self._monotonic, next_id=self._next_wire_id,
                          keepalive_s=self._goto_keepalive_s)
        return reply, watch

    def _read_reply_keeping(self, sock: Any, want_id: int, state: dict, deadline: float):
        """Like the base frame reader, but frames that arrive in the same chunk BEHIND the reply stay in ``state['buffer']`` (they may be
        the first events of the GoTo) instead of being dropped."""
        while True:
            frames, state["buffer"] = split_frames(state["buffer"])
            for index, raw in enumerate(frames):
                frame = decode_frame(raw)
                if frame is None:
                    continue
                frame_id = frame.get("id")
                if frame_id == want_id and not isinstance(frame_id, bool):
                    state["buffer"] = b"".join(f + FRAME_TERMINATOR for f in frames[index + 1:]) + state["buffer"]
                    return require_mapping(frame)
                state["skipped"] += 1
                if state["skipped"] > MAX_SKIPPED_FRAMES:
                    raise SeestarProtocolError("too_many_events")
            if self._monotonic() >= deadline:
                raise SeestarTimeout("read_timeout")
            try:
                chunk = sock.recv(4096)
            except TimeoutError:
                raise SeestarTimeout("read_timeout") from None
            except OSError:
                raise SeestarUnreachable("connection_lost") from None
            if not chunk:
                raise SeestarUnreachable("connection_closed")
            state["buffer"] += chunk

    def _next_wire_id(self) -> int:
        with self._lock:
            self._command_id += 1
            return self._command_id

    def _send_wire(self, host: str, build, *, keep: bool = False):
        with self._lock:
            self._command_id += 1
            message = build(self._command_id)
            started = False
            kept = False
            sock: Any = None
            try:
                deadline = self._monotonic() + self._config.read_timeout_s
                try:
                    sock = self._connect((host, self._config.tcp_port), self._config.connect_timeout_s)
                except TimeoutError:
                    raise ControlPreSendError("connect_timeout") from None
                except OSError:
                    raise ControlPreSendError("connect_failed") from None
                sock.settimeout(self._config.read_timeout_s)
                state = {"buffer": b"", "skipped": 0}

                def exchange(frame: dict) -> Any:
                    self._send(sock, json.dumps(frame).encode("utf-8") + FRAME_TERMINATOR)
                    return require_mapping(self._read_frame(sock, frame["id"], state, deadline))

                perform_handshake(exchange, self._authenticator)  # any failure here is before the command frame
                payload = json.dumps(message).encode("utf-8") + FRAME_TERMINATOR
                started = True  # set BEFORE the write: from here on the frame may have left the process
                self._send(sock, payload)
                reply = parse_reply(
                    self._read_reply_keeping(sock, message["id"], state, deadline) if keep
                    else self._read_frame(sock, message["id"], state, deadline))
                if keep:
                    kept = True
                    return reply, sock, state["buffer"]
                return reply
            except (ControlPreSendError, ControlPostSendError):
                raise
            except Exception as exc:
                category = getattr(exc, "category", None)
                token = category if isinstance(category, str) else "control_failed"
                if started:
                    raise ControlPostSendError(token) from None
                raise ControlPreSendError(token) from None
            finally:
                if sock is not None and not kept:
                    try:
                        sock.close()
                    except OSError:
                        pass
