"""Versioned binary control protocol between the parent and a decoder worker (DB-03 Wave 4B-1).

Pure codec: no I/O, no processes, no pickle. Every message is one ``send_bytes`` payload of at most
``MAX_MESSAGE_BYTES``. The parent never unpickles anything that comes from a worker.

Message = 8-byte header (``"TDW1"``, type u8, flags u8 = 0, reserved u16 = 0, network order) + a payload whose
length is fixed by the type (the ``OPEN`` address and the ``INIT`` segment name are length-prefixed). Anything
shorter, longer, mis-typed, with non-zero reserved bits or an out-of-range field is a ``ProtocolError``.

``status`` fields are unsigned 16-bit codes from a fixed table (``Status``), never text. The table is versioned
(``STATUS_TABLE_VERSION``) and append-only: a number is never reused. A code that is not in the table is an
``UnknownStatus``; the parent treats it as a protocol violation.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Union

__all__ = [
    "MAGIC", "MAX_ADDRESS_BYTES", "MAX_MESSAGE_BYTES", "MAX_TIMEOUT_MS", "MIN_TIMEOUT_MS", "PARENT_CATEGORIES",
    "PROTOCOL_VERSION", "STATUS_TABLE", "STATUS_TABLE_VERSION", "Close", "Hello", "Init", "Message",
    "MessageType", "Open", "Ping", "Pong", "ProtocolError", "Read", "Result", "Status", "UnknownStatus",
    "category_for_status", "decode", "encode", "status_for_category",
]

PROTOCOL_VERSION = 1
STATUS_TABLE_VERSION = 1
MAGIC = b"TDW1"
MAX_MESSAGE_BYTES = 4096
MAX_ADDRESS_BYTES = 512
MAX_SEGMENT_NAME_BYTES = 255
MIN_TIMEOUT_MS = 100
MAX_TIMEOUT_MS = 60_000
ABSOLUTE_MAX_DIMENSION = 16384          # same ceiling as device_runtime.preview_models
ABSOLUTE_MAX_SLOT_BYTES = 256 * 1024 * 1024

_HEADER = struct.Struct("!4sBBH")
_U32_MAX = 0xFFFFFFFF
_U64_MAX = 0xFFFFFFFFFFFFFFFF
_SEGMENT_NAME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-/")


class MessageType(IntEnum):
    INIT = 1
    HELLO = 2
    PING = 3
    PONG = 4
    OPEN = 5
    READ = 6
    CLOSE = 7
    RESULT = 8
    IMAGE_READY = 9


class Status(IntEnum):
    """Worker-originated status codes (decision D13). Append-only; the frozen test pins every value."""

    OK = 0
    OPENCV_UNAVAILABLE = 1
    OPENCV_VERSION_UNKNOWN = 2
    OPENCV_VERSION_UNSUPPORTED = 3
    OPENCV_INCOMPATIBLE = 4
    INVALID_ENDPOINT = 5
    OPEN_FAILED = 6
    BACKEND_UNVERIFIED = 7
    BACKEND_MISMATCH = 8
    READ_FAILED = 9
    DECODER_BAD_OUTPUT = 10
    UNSUPPORTED_DEPTH = 11
    UNSUPPORTED_FORMAT = 12
    DIMENSIONS_EXCEED_LIMITS = 13
    IMAGE_EXCEEDS_LIMITS = 14
    LENGTH_MISMATCH = 15
    RELEASE_FAILED = 16
    INVALID_STATE = 17
    BUSY = 18
    NOT_OPEN = 19
    PYTHON_UNSUPPORTED = 20
    SHM_UNAVAILABLE = 21
    WORKER_INTERNAL = 255


# category (the DecoderError category used by Wave 4) for every non-zero status
STATUS_TABLE = {status.value: status.name.lower() for status in Status if status is not Status.OK}

# Categories created by the parent only. They have no wire code and a worker cannot cause one by sending a code.
PARENT_CATEGORIES = frozenset({
    "worker_start_failed", "worker_handshake_timeout", "worker_protocol_error", "worker_crashed", "worker_ipc_lost",
    "open_deadline_exceeded", "read_deadline_exceeded", "close_deadline_exceeded", "containment_failed",
    "worker_limit", "readiness_revoked",
})
assert not (PARENT_CATEGORIES & set(STATUS_TABLE.values()))


class ProtocolError(Exception):
    """A malformed or out-of-range message. ``reason`` is a fixed token; no message data is ever included."""

    category = "worker_protocol_error"

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class UnknownStatus(ProtocolError):
    def __init__(self) -> None:
        super().__init__("unknown_status")


def category_for_status(code: int) -> str | None:
    """Category for a status code; ``None`` for OK. Raises ``UnknownStatus`` for any code not in the table."""
    if code == Status.OK:
        return None
    try:
        return STATUS_TABLE[code]
    except (KeyError, TypeError):
        raise UnknownStatus() from None


def status_for_category(category: str) -> int:
    """Worker side: the code for a Wave 4 category; anything not in the table becomes ``WORKER_INTERNAL``."""
    for code, name in STATUS_TABLE.items():
        if name == category:
            return code
    return int(Status.WORKER_INTERNAL)


# --- field validation ---------------------------------------------------------------------------


def _uint(name: str, value: object, maximum: int, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ProtocolError(f"{name}_range")
    return value


def _op_id(value: object) -> int:
    return _uint("op_id", value, _U32_MAX, 1)          # 0 is reserved


def _timeout(name: str, value: object) -> int:
    return _uint(name, value, MAX_TIMEOUT_MS, MIN_TIMEOUT_MS)


# --- messages -----------------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class Init:
    """Parent -> worker, first message. ``segment_name`` empty and ``slot_bytes`` 0 means "no shared memory" (4B-1)."""

    proto_version: int
    status_table_version: int
    nonce: int
    segment_name: str
    slot_bytes: int
    max_width: int
    max_height: int

    def __post_init__(self) -> None:
        _uint("proto_version", self.proto_version, 0xFFFF)
        _uint("table_version", self.status_table_version, 0xFFFF)
        _uint("nonce", self.nonce, _U64_MAX)
        if not isinstance(self.segment_name, str) or len(self.segment_name) > MAX_SEGMENT_NAME_BYTES or not (
            set(self.segment_name) <= _SEGMENT_NAME_CHARS
        ):
            raise ProtocolError("segment_name")
        _uint("slot_bytes", self.slot_bytes, ABSOLUTE_MAX_SLOT_BYTES)
        if bool(self.segment_name) != (self.slot_bytes > 0):
            raise ProtocolError("segment_slot_mismatch")
        _uint("max_width", self.max_width, ABSOLUTE_MAX_DIMENSION, 1)
        _uint("max_height", self.max_height, ABSOLUTE_MAX_DIMENSION, 1)


@dataclass(slots=True, frozen=True)
class Hello:
    proto_version: int
    status_table_version: int
    status: int
    nonce: int

    def __post_init__(self) -> None:
        _uint("proto_version", self.proto_version, 0xFFFF)
        _uint("table_version", self.status_table_version, 0xFFFF)
        _uint("status", self.status, 0xFFFF)          # unknown codes are decodable; classification is the parent's rule
        _uint("nonce", self.nonce, _U64_MAX)


@dataclass(slots=True, frozen=True)
class Ping:
    op_id: int

    def __post_init__(self) -> None:
        _op_id(self.op_id)


@dataclass(slots=True, frozen=True)
class Pong:
    op_id: int

    def __post_init__(self) -> None:
        _op_id(self.op_id)


@dataclass(slots=True, frozen=True)
class Open:
    op_id: int
    open_timeout_ms: int
    read_timeout_ms: int
    address: str          # carried over the private pipe only; never in argv, never logged, never repr'd

    def __post_init__(self) -> None:
        _op_id(self.op_id)
        _timeout("open_timeout", self.open_timeout_ms)
        _timeout("read_timeout", self.read_timeout_ms)
        if not isinstance(self.address, str):
            raise ProtocolError("address")
        try:
            raw = self.address.encode("utf-8")
        except UnicodeEncodeError:
            raise ProtocolError("address") from None
        if not 8 <= len(raw) <= MAX_ADDRESS_BYTES or not self.address.startswith("rtsp://") or any(
            ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in self.address
        ):
            raise ProtocolError("address")

    def __repr__(self) -> str:
        return f"Open(op_id={self.op_id}, open_timeout_ms={self.open_timeout_ms}, read_timeout_ms={self.read_timeout_ms}, address=<redacted>)"

    __str__ = __repr__


@dataclass(slots=True, frozen=True)
class Read:
    op_id: int

    def __post_init__(self) -> None:
        _op_id(self.op_id)


@dataclass(slots=True, frozen=True)
class Close:
    op_id: int

    def __post_init__(self) -> None:
        _op_id(self.op_id)


@dataclass(slots=True, frozen=True)
class Result:
    op_id: int
    status: int

    def __post_init__(self) -> None:
        _op_id(self.op_id)
        _uint("status", self.status, 0xFFFF)


@dataclass(slots=True, frozen=True)
class ImageReady:
    """Worker -> parent: an image is in the shared slot (wire name IMAGE_READY; 'frame' is a domain term in this project). ``decode_ns`` is diagnostic only (never a time of the scene)."""

    op_id: int
    seq: int
    width: int
    height: int
    pixel_format: int
    nbytes: int
    decode_ns: int

    def __post_init__(self) -> None:
        _op_id(self.op_id)
        _uint("seq", self.seq, _U32_MAX, 1)
        _uint("width", self.width, ABSOLUTE_MAX_DIMENSION, 1)
        _uint("height", self.height, ABSOLUTE_MAX_DIMENSION, 1)
        if self.pixel_format not in (1, 3):
            raise ProtocolError("pixel_format")
        _uint("nbytes", self.nbytes, ABSOLUTE_MAX_SLOT_BYTES, 1)
        _uint("decode_ns", self.decode_ns, _U64_MAX)


Message = Union[Init, Hello, Ping, Pong, Open, Read, Close, Result, ImageReady]

_INIT_FIXED = struct.Struct("!HHQB")
_INIT_TAIL = struct.Struct("!III")
_HELLO = struct.Struct("!HHHQ")
_OP = struct.Struct("!I")
_OPEN_FIXED = struct.Struct("!IIIH")
_RESULT = struct.Struct("!IH")
_IMAGE_READY = struct.Struct("!IIIIBIQ")


def _wrap(kind: MessageType, payload: bytes) -> bytes:
    raw = _HEADER.pack(MAGIC, int(kind), 0, 0) + payload
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ProtocolError("message_size")
    return raw


def encode(message: Message) -> bytes:
    if isinstance(message, Init):
        name = message.segment_name.encode("ascii")
        return _wrap(MessageType.INIT, _INIT_FIXED.pack(message.proto_version, message.status_table_version, message.nonce, len(name))
                      + name + _INIT_TAIL.pack(message.slot_bytes, message.max_width, message.max_height))
    if isinstance(message, Hello):
        return _wrap(MessageType.HELLO, _HELLO.pack(message.proto_version, message.status_table_version, message.status, message.nonce))
    if isinstance(message, Ping):
        return _wrap(MessageType.PING, _OP.pack(message.op_id))
    if isinstance(message, Pong):
        return _wrap(MessageType.PONG, _OP.pack(message.op_id))
    if isinstance(message, Open):
        address = message.address.encode("utf-8")
        return _wrap(MessageType.OPEN, _OPEN_FIXED.pack(message.op_id, message.open_timeout_ms, message.read_timeout_ms, len(address)) + address)
    if isinstance(message, Read):
        return _wrap(MessageType.READ, _OP.pack(message.op_id))
    if isinstance(message, Close):
        return _wrap(MessageType.CLOSE, _OP.pack(message.op_id))
    if isinstance(message, Result):
        return _wrap(MessageType.RESULT, _RESULT.pack(message.op_id, message.status))
    if isinstance(message, ImageReady):
        return _wrap(MessageType.IMAGE_READY, _IMAGE_READY.pack(message.op_id, message.seq, message.width, message.height,
                                                     message.pixel_format, message.nbytes, message.decode_ns))
    raise ProtocolError("message_type")


def _exact(payload: bytes, size: int) -> None:
    if len(payload) != size:
        raise ProtocolError("payload_length")


def decode(raw: object) -> Message:
    """Decode one message. Raises ``ProtocolError`` (never anything else) for any malformed input."""
    if not isinstance(raw, (bytes, bytearray)):
        raise ProtocolError("not_bytes")
    raw = bytes(raw)
    if len(raw) < _HEADER.size or len(raw) > MAX_MESSAGE_BYTES:
        raise ProtocolError("message_size")
    magic, kind, flags, reserved = _HEADER.unpack_from(raw)
    if magic != MAGIC:
        raise ProtocolError("magic")
    if flags != 0 or reserved != 0:
        raise ProtocolError("reserved_bits")
    try:
        kind = MessageType(kind)
    except ValueError:
        raise ProtocolError("message_type") from None
    payload = raw[_HEADER.size:]
    try:
        if kind is MessageType.INIT:
            if len(payload) < _INIT_FIXED.size + _INIT_TAIL.size:
                raise ProtocolError("payload_length")
            version, table, nonce, name_len = _INIT_FIXED.unpack_from(payload)
            _exact(payload, _INIT_FIXED.size + name_len + _INIT_TAIL.size)
            name_raw = payload[_INIT_FIXED.size:_INIT_FIXED.size + name_len]
            try:
                name = name_raw.decode("ascii")
            except UnicodeDecodeError:
                raise ProtocolError("segment_name") from None
            slot, width, height = _INIT_TAIL.unpack_from(payload, _INIT_FIXED.size + name_len)
            return Init(version, table, nonce, name, slot, width, height)
        if kind is MessageType.HELLO:
            _exact(payload, _HELLO.size)
            return Hello(*_HELLO.unpack(payload))
        if kind in (MessageType.PING, MessageType.PONG, MessageType.READ, MessageType.CLOSE):
            _exact(payload, _OP.size)
            (op,) = _OP.unpack(payload)
            return {MessageType.PING: Ping, MessageType.PONG: Pong, MessageType.READ: Read, MessageType.CLOSE: Close}[kind](op)
        if kind is MessageType.OPEN:
            if len(payload) < _OPEN_FIXED.size:
                raise ProtocolError("payload_length")
            op, open_ms, read_ms, addr_len = _OPEN_FIXED.unpack_from(payload)
            if addr_len > MAX_ADDRESS_BYTES:
                raise ProtocolError("address")
            _exact(payload, _OPEN_FIXED.size + addr_len)
            try:
                address = payload[_OPEN_FIXED.size:].decode("utf-8")
            except UnicodeDecodeError:
                raise ProtocolError("address") from None
            return Open(op, open_ms, read_ms, address)
        if kind is MessageType.RESULT:
            _exact(payload, _RESULT.size)
            return Result(*_RESULT.unpack(payload))
        _exact(payload, _IMAGE_READY.size)
        return ImageReady(*_IMAGE_READY.unpack(payload))
    except struct.error:
        raise ProtocolError("payload_length") from None
