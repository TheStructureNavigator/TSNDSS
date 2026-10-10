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
from ..seestar_provider.errors import SeestarConfigError
from ..seestar_provider.protocol import FRAME_TERMINATOR, RpcReply, parse_reply
from ..seestar_provider.transport import ConnectFactory, TcpSeestarTransport
from .commands import COMMANDS, wire_message
from .errors import ControlPostSendError, ControlPreSendError

__all__ = ["SeestarControlTransport"]

_COMMAND_ID_START = 2000  # distinct from the handshake ids (1001-1003)


class SeestarControlTransport(TcpSeestarTransport):
    def __init__(
        self,
        config: SeestarProviderConfig,
        authenticator: Authenticator,
        *,
        connect_factory: ConnectFactory | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
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
        with self._lock:
            self._command_id += 1
            message = wire_message(command, self._command_id)
            started = False
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
                return parse_reply(self._read_frame(sock, message["id"], state, deadline))
            except (ControlPreSendError, ControlPostSendError):
                raise
            except Exception as exc:
                category = getattr(exc, "category", None)
                token = category if isinstance(category, str) else "control_failed"
                if started:
                    raise ControlPostSendError(token) from None
                raise ControlPreSendError(token) from None
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
