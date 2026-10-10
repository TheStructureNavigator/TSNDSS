"""Short-lived, read-only TCP/UDP transport.

Public surface: three typed reads, one opt-in UDP discovery. There is no
generic call, send or execute method; the only way to put bytes on the wire is
``protocol.encode_read_request`` (allow-list) plus the handshake prerequisite.

Every read opens a fresh connection, authenticates (if an authenticator is
configured), exchanges one request and closes. Automatic retries apply only to
methods classified ``idempotent`` in ``protocol.READ_METHODS``.

Importing this module performs no network access. Sockets are created lazily
inside methods through injectable factories.
"""

from __future__ import annotations

import json
import socket
import time
from typing import Any, Callable, Mapping, Protocol, Sequence

from .auth import Authenticator, perform_handshake, require_mapping
from .config import SeestarProviderConfig
from .errors import (
    SeestarConfigError,
    SeestarError,
    SeestarProtocolError,
    SeestarTimeout,
    SeestarUnreachable,
)
from .protocol import (
    MAX_SKIPPED_FRAMES,
    READ_METHODS,
    SCAN_PROBE,
    RpcReply,
    SeestarAnnouncement,
    decode_frame,
    encode_read_request,
    parse_reply,
    parse_scan_reply,
    split_frames,
)

ConnectFactory = Callable[[tuple[str, int], float], Any]
UdpFactory = Callable[[], Any]


class SeestarReadTransport(Protocol):
    """The only door to the device. Reads and opt-in discovery; nothing else."""

    def read_device_state(self, host: str, keys: Sequence[str]) -> RpcReply: ...

    def read_app_state(self, host: str) -> RpcReply: ...

    def test_connection(self, host: str) -> RpcReply: ...

    def discover_via_udp(self) -> Sequence[SeestarAnnouncement]: ...


class TcpSeestarTransport:
    def __init__(
        self,
        config: SeestarProviderConfig,
        *,
        authenticator: Authenticator | None = None,
        connect_factory: ConnectFactory | None = None,
        udp_factory: UdpFactory | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._authenticator = authenticator
        self._connect = connect_factory or (lambda addr, timeout: socket.create_connection(addr, timeout))
        self._udp = udp_factory or (lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
        self._monotonic = monotonic

    def __repr__(self) -> str:
        return "TcpSeestarTransport(<redacted>)"

    # --- typed reads ---------------------------------------------------------

    def read_device_state(self, host: str, keys: Sequence[str]) -> RpcReply:
        return self._exchange(host, "get_device_state", {"keys": list(keys)})

    def read_app_state(self, host: str) -> RpcReply:
        return self._exchange(host, "iscope_get_app_state", {})

    def test_connection(self, host: str) -> RpcReply:
        return self._exchange(host, "test_connection", {})

    def read_equ_coord(self, host: str) -> RpcReply:
        """The mount's reported equatorial coordinates. Read-only; the reply is returned as the device sent it."""
        return self._exchange(host, "scope_get_equ_coord", {})

    # --- opt-in UDP discovery ----------------------------------------------------

    def discover_via_udp(self) -> Sequence[SeestarAnnouncement]:
        if not self._config.allow_udp_discovery:
            raise SeestarConfigError("udp_discovery_disabled")
        sock = self._udp()
        found: dict[str, SeestarAnnouncement] = {}
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(0.15)
            deadline = self._monotonic() + self._config.discovery_timeout_s
            next_probe = 0.0
            while True:
                now = self._monotonic()
                if now >= deadline:
                    break
                if now >= next_probe:
                    for target in self._config.broadcast_targets:
                        try:
                            sock.sendto(SCAN_PROBE, (target, self._config.udp_port))
                        except OSError:
                            pass
                    next_probe = now + 0.5
                try:
                    data, address = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    continue
                announcement = parse_scan_reply(data, address[0])
                if announcement is not None:
                    found[announcement.sn] = announcement  # last reply wins
        finally:
            try:
                sock.close()
            except OSError:
                pass
        return tuple(found[sn] for sn in sorted(found))

    # --- internals -----------------------------------------------------------------

    def _exchange(self, host: str, method: str, args: Mapping[str, Any]) -> RpcReply:
        # Validates the allow-list before any socket exists.
        spec = READ_METHODS.get(method)
        encode_read_request(1, method, args)
        attempts = 1 + (self._config.read_retries if spec is not None and spec.idempotent else 0)
        last: SeestarError | None = None
        for _ in range(attempts):
            try:
                return self._exchange_once(host, method, args)
            except (SeestarTimeout, SeestarUnreachable) as exc:
                last = exc
        assert last is not None
        raise last

    def _exchange_once(self, host: str, method: str, args: Mapping[str, Any]) -> RpcReply:
        deadline = self._monotonic() + self._config.read_timeout_s
        try:
            sock = self._connect((host, self._config.tcp_port), self._config.connect_timeout_s)
        except socket.timeout:
            raise SeestarTimeout("connect_timeout") from None
        except OSError:
            raise SeestarUnreachable("connect_failed") from None
        try:
            sock.settimeout(self._config.read_timeout_s)
            state = {"buffer": b"", "skipped": 0}

            def exchange(message: Mapping[str, Any]) -> Mapping[str, Any]:
                self._send(sock, json.dumps(message).encode("utf-8") + b"\r\n")
                return self._read_frame(sock, message["id"], state, deadline)

            if self._authenticator is not None:
                perform_handshake(exchange, self._authenticator)
            request_id = 1
            self._send(sock, encode_read_request(request_id, method, args))
            frame = self._read_frame(sock, request_id, state, deadline)
            return parse_reply(frame)
        finally:
            try:
                sock.close()
            except OSError:
                pass

    @staticmethod
    def _send(sock: Any, payload: bytes) -> None:
        try:
            sock.sendall(payload)
        except socket.timeout:
            raise SeestarTimeout("send_timeout") from None
        except OSError:
            raise SeestarUnreachable("connection_lost") from None

    def _read_frame(
        self, sock: Any, want_id: object, state: dict[str, Any], deadline: float
    ) -> Mapping[str, Any]:
        while True:
            frames, state["buffer"] = split_frames(state["buffer"])
            for raw in frames:
                frame = decode_frame(raw)
                if frame is None:
                    continue
                frame_id = frame.get("id")
                if frame_id == want_id and not isinstance(frame_id, bool):
                    return require_mapping(frame)
                state["skipped"] += 1  # unsolicited event interleaved by the device
                if state["skipped"] > MAX_SKIPPED_FRAMES:
                    raise SeestarProtocolError("too_many_events")
            if self._monotonic() >= deadline:
                raise SeestarTimeout("read_timeout")
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                raise SeestarTimeout("read_timeout") from None
            except OSError:
                raise SeestarUnreachable("connection_lost") from None
            if not chunk:
                raise SeestarUnreachable("connection_closed")
            state["buffer"] += chunk
