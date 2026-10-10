"""Deterministic offline fixtures for DB-02: fake transport, scripted TCP/UDP peers.

Nothing here opens a real socket or reads a real key.
"""

from __future__ import annotations

import copy
import json
import socket
from collections import deque
from pathlib import Path
from typing import Any, Sequence

from tsn_dss.engine.device_runtime import ManualClock, ProviderRuntime, SequentialIdGenerator
from tsn_dss.engine.seestar_provider import (
    SeestarAnnouncement,
    SeestarProvider,
    SeestarProviderConfig,
    TcpSeestarTransport,
)
from tsn_dss.engine.seestar_provider.protocol import RpcReply, parse_reply

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "seestar"
HOST = "192.0.2.10"  # documentation range (RFC 5737)
OTHER_HOST = "192.0.2.77"
KEY_PATH = "operator-supplied.pem"  # a path string only; never opened by these tests


def load_fixture(name: str) -> dict[str, Any]:
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    data.pop("_synthetic", None)
    return data


def make_config(**overrides: Any) -> SeestarProviderConfig:
    base: dict[str, Any] = dict(host=HOST, key_path=KEY_PATH)
    base.update(overrides)
    return SeestarProviderConfig(**base)


class StubAuthenticator:
    """Signs deterministically with no cryptography and no key."""

    def sign(self, challenge: str) -> str:
        return f"sig:{challenge}"


# --- fake transport (provider-level tests) ---------------------------------------


class FakeSeestarTransport:
    """Scriptable ``SeestarReadTransport``. Records every call; has no other methods."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.state_reply = load_fixture("device_state_full_unfiltered.json")
        self.app_reply = load_fixture("app_state_scenery_ready.json")
        self.announcements: list[SeestarAnnouncement] = []
        self.failures: dict[str, deque[Exception]] = {
            "device_state": deque(),
            "app_state": deque(),
            "test_connection": deque(),
            "udp": deque(),
        }
        self.honor_key_filter = False  # emulate firmware that returns the full payload
        self.addresses: dict[str, dict[str, Any]] = {}  # host -> device-state reply override

    def fail_next(self, kind: str, error: Exception, times: int = 1) -> None:
        for _ in range(times):
            self.failures[kind].append(error)

    def _maybe_fail(self, kind: str) -> None:
        if self.failures[kind]:
            raise self.failures[kind].popleft()

    def read_device_state(self, host: str, keys: Sequence[str]) -> RpcReply:
        self.calls.append(("read_device_state", host, ",".join(keys)))
        self._maybe_fail("device_state")
        frame = copy.deepcopy(self.addresses.get(host, self.state_reply))
        if self.honor_key_filter and isinstance(frame.get("result"), dict):
            frame["result"] = {k: v for k, v in frame["result"].items() if k in keys}
        return parse_reply(frame)

    def read_app_state(self, host: str) -> RpcReply:
        self.calls.append(("read_app_state", host))
        self._maybe_fail("app_state")
        return parse_reply(copy.deepcopy(self.app_reply))

    def test_connection(self, host: str) -> RpcReply:
        self.calls.append(("test_connection", host))
        self._maybe_fail("test_connection")
        return parse_reply({"method": "test_connection", "result": "ok", "code": 0, "id": 1})

    def discover_via_udp(self) -> Sequence[SeestarAnnouncement]:
        self.calls.append(("discover_via_udp",))
        self._maybe_fail("udp")
        return tuple(self.announcements)


def make_runtime(config: SeestarProviderConfig | None = None, transport: FakeSeestarTransport | None = None):
    config = config or make_config()
    transport = transport or FakeSeestarTransport()
    provider = SeestarProvider(config, transport, clock=ManualClock())
    runtime = ProviderRuntime(provider, clock=ManualClock(), id_generator=SequentialIdGenerator())
    return runtime, provider, transport


def connected(config: SeestarProviderConfig | None = None, transport: FakeSeestarTransport | None = None):
    runtime, provider, transport = make_runtime(config, transport)
    device = runtime.discover().devices[0]
    connection = runtime.connect(runtime.open_connection(device))
    return runtime, provider, transport, connection


# --- scripted TCP/UDP peers (wire-level tests) ---------------------------------------


class ScriptedSocket:
    """Socket-like peer wired to a ``ScriptedDevice``."""

    def __init__(self, device: "ScriptedDevice") -> None:
        self.device = device
        self.received: list[dict[str, Any]] = []
        self.closed = False
        self.timeout: float | None = None
        self._incoming = bytearray()

    def settimeout(self, value: float | None) -> None:
        self.timeout = value

    def sendall(self, payload: bytes) -> None:
        if self.device.fail_send is not None:
            raise self.device.fail_send
        for line in payload.split(b"\r\n"):
            if not line:
                continue
            message = json.loads(line)
            self.received.append(message)
            for frame in self.device.handle(message):
                self._incoming += json.dumps(frame).encode() + b"\r\n"

    def recv(self, size: int) -> bytes:
        if self.device.recv_error is not None:
            raise self.device.recv_error
        if not self._incoming:
            if self.device.close_when_empty:
                return b""
            raise socket.timeout("scripted silence")
        step = self.device.chunk_size or size
        chunk, self._incoming = bytes(self._incoming[:step]), self._incoming[step:]
        return chunk

    def close(self) -> None:
        self.closed = True


class ScriptedDevice:
    """Protocol-accurate fake Seestar. ``auth`` is 'none', 'absent' (code 103) or 'required'."""

    def __init__(self, auth: str = "none") -> None:
        self.auth = auth
        self.sockets: list[ScriptedSocket] = []
        self.connect_errors: deque[Exception] = deque()
        self.connect_addresses: list[tuple[str, int]] = []
        self.unexpected: list[str] = []
        self.state_reply = load_fixture("device_state_full_unfiltered.json")
        self.app_reply = load_fixture("app_state_scenery_ready.json")
        self.equ_reply: dict[str, Any] = {"method": "scope_get_equ_coord", "code": 0, "result": {"ra": 5.5, "dec": -5.25}}  # synthetic
        self.camera_reply: dict[str, Any] = {"method": "get_camera_state", "code": 0, "result": {"state": "idle", "name": "synthetic-camera"}}  # synthetic
        self.goto_calls: list[Any] = []
        self.goto_events: list[dict[str, Any]] = []  # frames the device sends right after it answers scope_goto
        self.events_before_reply = 0
        self.chunk_size: int | None = None
        self.close_when_empty = False
        self.recv_error: Exception | None = None
        self.fail_send: Exception | None = None
        self.accept_signature = True
        self.challenge = "synthetic-challenge"
        self._verified = False

    def connect(self, address: tuple[str, int], timeout: float) -> ScriptedSocket:
        self.connect_addresses.append(address)
        if self.connect_errors:
            raise self.connect_errors.popleft()
        sock = ScriptedSocket(self)
        self.sockets.append(sock)
        self._verified = False
        return sock

    @property
    def methods(self) -> list[str]:
        return [m["method"] for s in self.sockets for m in s.received]

    def handle(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        method, request_id = message.get("method"), message.get("id")
        frames: list[dict[str, Any]] = [
            {"Event": "PiStatus", "temp": 40.0} for _ in range(self.events_before_reply)
        ]
        if method == "get_verify_str":
            if self.auth == "absent":
                frames.append({"id": request_id, "code": 103, "result": None})
            else:
                frames.append({"id": request_id, "code": 0, "result": {"str": self.challenge}})
            return frames
        if method == "verify_client":
            ok = self.accept_signature and message["params"]["sign"] == f"sig:{self.challenge}"
            self._verified = ok
            frames.append({"id": request_id, "code": 0 if ok else 1, "result": None})
            return frames
        if method == "pi_is_verified":
            frames.append({"id": request_id, "code": 0, "result": None})
            return frames
        if self.auth == "required" and not self._verified:
            return frames  # a device that ignores unauthenticated requests: silence
        if method == "get_device_state":
            keys = message.get("params", {}).get("keys", [])
            reply = dict(self.state_reply)
            reply["id"] = request_id
            reply["result"] = {k: v for k, v in reply["result"].items() if k in keys}
            frames.append(reply)
        elif method == "iscope_get_app_state":
            frames.append(dict(self.app_reply, id=request_id))
        elif method == "test_connection":
            frames.append({"id": request_id, "method": method, "code": 0, "result": "ok"})
        elif method == "scope_get_equ_coord":
            frames.append(dict(self.equ_reply, id=request_id))
        elif method == "scope_goto":
            self.goto_calls.append(message.get("params"))
            frames.append({"id": request_id, "method": method, "code": 0, "result": 0})
            frames.extend(dict(event) for event in self.goto_events)
        elif method == "get_camera_state":
            frames.append(dict(self.camera_reply, id=request_id))
        else:
            self.unexpected.append(str(method))
        return frames


class ScriptedUdpSocket:
    def __init__(self, replies: list[tuple[bytes, tuple[str, int]]]) -> None:
        self.replies = list(replies)
        self.sent: list[tuple[bytes, tuple[str, int]]] = []
        self.options: list[tuple[int, int, int]] = []
        self.closed = False

    def setsockopt(self, level: int, name: int, value: int) -> None:
        self.options.append((level, name, value))

    def settimeout(self, value: float) -> None:
        pass

    def sendto(self, data: bytes, address: tuple[str, int]) -> None:
        self.sent.append((data, address))

    def recvfrom(self, size: int) -> tuple[bytes, tuple[str, int]]:
        if self.replies:
            return self.replies.pop(0)
        raise socket.timeout("scripted silence")

    def close(self) -> None:
        self.closed = True


class StepClock:
    """Monotonic clock advancing a fixed step per read, to exercise deadlines."""

    def __init__(self, step: float = 0.05) -> None:
        self.now = 0.0
        self.step = step

    def __call__(self) -> float:
        self.now += self.step
        return self.now


def make_transport(device: ScriptedDevice, config: SeestarProviderConfig | None = None, **kwargs: Any):
    return TcpSeestarTransport(config or make_config(), connect_factory=device.connect, **kwargs)


__all__ = [n for n in dir() if not n.startswith("_")]
