"""Wire framing and the read-only method allow-list.

Protocol facts are taken from the committed research (docs/S30Pro_WZRD_research_*,
docs/research/S30Lab/) with seestarpy 0.7.1 consulted only as secondary
documentation of the wire format. No source code is copied.

Only the methods in ``READ_METHODS`` can ever be encoded. The authentication
handshake methods live in ``auth.py`` and are used only as a connection
prerequisite.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .errors import SeestarMethodNotAllowed, SeestarProtocolError

FRAME_TERMINATOR = b"\r\n"
MAX_FRAME_BYTES = 1_048_576
MAX_SKIPPED_FRAMES = 500
SCAN_PROBE = json.dumps({"id": 1, "method": "scan_iscope", "params": ""}).encode("utf-8") + b"\n\n"

# Top-level get_device_state keys the provider may request. Blocks that carry
# credentials, network identifiers or location are deliberately absent so they
# are never requested.
ALLOWED_STATE_KEYS = frozenset(
    {"device", "mount", "pi_status", "focuser", "second_focuser", "setting"}
)
FORBIDDEN_STATE_KEYS = frozenset({"ap", "station", "location_lon_lat", "storage", "cpuId"})


@dataclass(slots=True, frozen=True)
class ReadMethod:
    """An explicitly classified read operation."""

    name: str
    idempotent: bool
    build_params: Callable[[Mapping[str, Any]], dict[str, Any] | None]


def _device_state_params(args: Mapping[str, Any]) -> dict[str, Any]:
    keys = list(args.get("keys", ()))
    if not keys or any(k not in ALLOWED_STATE_KEYS for k in keys):
        raise SeestarMethodNotAllowed("state_key_not_allowed")
    return {"keys": keys}


def _no_params(args: Mapping[str, Any]) -> None:
    if args:
        raise SeestarMethodNotAllowed("unexpected_params")
    return None


READ_METHODS: Mapping[str, ReadMethod] = {
    "get_device_state": ReadMethod("get_device_state", True, _device_state_params),
    "iscope_get_app_state": ReadMethod("iscope_get_app_state", True, _no_params),
    "test_connection": ReadMethod("test_connection", True, _no_params),
    # Current mount RA/Dec. Read-only diagnostic (DB-05 follow-up); its units and epoch are not verified on hardware.
    "scope_get_equ_coord": ReadMethod("scope_get_equ_coord", True, _no_params),
}


def encode_read_request(request_id: int, method: str, args: Mapping[str, Any] | None = None) -> bytes:
    """Encode an allow-listed read request. Anything else raises ``SeestarMethodNotAllowed``."""
    spec = READ_METHODS.get(method)
    if spec is None:
        raise SeestarMethodNotAllowed("method_not_allowed")
    params = spec.build_params(args or {})
    message: dict[str, Any] = {"id": request_id, "verify": True, "method": spec.name}
    if params is not None:
        message["params"] = params
    return json.dumps(message).encode("utf-8") + FRAME_TERMINATOR


@dataclass(slots=True, frozen=True)
class RpcReply:
    method: str
    code: int | None
    result: Any = field(repr=False)  # may carry credentials or location on some firmware
    device_timestamp: str | None


def parse_reply(frame: Mapping[str, Any]) -> RpcReply:
    code = frame.get("code")
    stamp = frame.get("Timestamp")
    return RpcReply(
        method=str(frame.get("method", "")),
        code=code if isinstance(code, int) and not isinstance(code, bool) else None,
        result=frame.get("result"),
        device_timestamp=stamp if isinstance(stamp, str) else None,
    )


def split_frames(buffer: bytes) -> tuple[list[bytes], bytes]:
    """Split ``buffer`` on CRLF. Returns complete frames and the remainder."""
    if FRAME_TERMINATOR not in buffer:
        if len(buffer) > MAX_FRAME_BYTES:
            raise SeestarProtocolError("frame_too_large")
        return [], buffer
    *frames, rest = buffer.split(FRAME_TERMINATOR)
    if len(rest) > MAX_FRAME_BYTES or any(len(f) > MAX_FRAME_BYTES for f in frames):
        raise SeestarProtocolError("frame_too_large")
    return [f for f in frames if f], rest


def decode_frame(raw: bytes) -> Mapping[str, Any] | None:
    """Decode one frame; return ``None`` for non-JSON or non-object frames."""
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


@dataclass(slots=True, frozen=True)
class SeestarAnnouncement:
    """One UDP discovery reply: serial, model and the address it answered from."""

    sn: str = field(repr=False)
    model: str
    ip: str = field(repr=False)


def parse_scan_reply(data: bytes, source_ip: str) -> SeestarAnnouncement | None:
    frame = decode_frame(data.strip())
    if frame is None:
        return None
    result = frame.get("result")
    if not isinstance(result, dict):
        return None
    sn = result.get("sn")
    if not isinstance(sn, str) or not sn.strip():
        return None
    model = result.get("product_model") or result.get("model") or ""
    return SeestarAnnouncement(sn=sn.strip(), model=str(model), ip=source_ip)


def device_ref_for(model: str, sn: str) -> str:
    """Stable runtime reference ``<model-slug>_<serial>``; never derived from an address."""
    slug = model.replace("Seestar", "").replace(" ", "").lower()
    return f"{slug}_{sn}" if slug else sn

