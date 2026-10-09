"""Shared-memory segment header codec and image validation (DB-03 Wave 4B-1; the segment itself arrives in 4B-2).

Pure functions over ``bytes``/``memoryview``: no shared memory is created or attached here. Layout (design section 7.1),
64-byte header, little-endian, fixed-width, followed by the pixel area:

    0  magic "TDS1" · 4 layout_version u16 · 6 slot_bytes u32 · 12 handshake_nonce u64 · 20 worker_pid u32
    24 begin_seq u32 · 28 end_seq u32 · 32 width u32 · 36 height u32 · 40 pixel_format u8 · 44 nbytes u32 · 48..63 zero

The header fields ``begin_seq``/``end_seq`` are verification fences, not synchronization: ownership of the slot is
decided by the message order (design section 7.3). ``validate_image`` is the parent-side check of section 7.4 step 3.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .protocol import ABSOLUTE_MAX_DIMENSION, ABSOLUTE_MAX_SLOT_BYTES, ImageReady, ProtocolError

__all__ = [
    "HEADER_BYTES", "LAYOUT_VERSION", "SEGMENT_MAGIC", "SegmentHeader", "bytes_per_pixel", "fence_is_stable",
    "pack_header", "segment_size", "unpack_header", "validate_image",
]

SEGMENT_MAGIC = b"TDS1"
LAYOUT_VERSION = 1
HEADER_BYTES = 64
_LAYOUT = struct.Struct("<4sHIxxQIIIIIB3xI16x")
assert _LAYOUT.size == HEADER_BYTES

_BYTES_PER_PIXEL = {1: 1, 3: 3}          # pixel_format code -> bytes per pixel (1 GRAY8, 3 BGR8)


def bytes_per_pixel(pixel_format: int) -> int:
    try:
        return _BYTES_PER_PIXEL[pixel_format]
    except (KeyError, TypeError):
        raise ProtocolError("pixel_format") from None


def segment_size(slot_bytes: int) -> int:
    if isinstance(slot_bytes, bool) or not isinstance(slot_bytes, int) or not 1 <= slot_bytes <= ABSOLUTE_MAX_SLOT_BYTES:
        raise ProtocolError("slot_bytes_range")
    return HEADER_BYTES + slot_bytes


@dataclass(slots=True, frozen=True)
class SegmentHeader:
    slot_bytes: int
    handshake_nonce: int = 0
    worker_pid: int = 0
    begin_seq: int = 0
    end_seq: int = 0
    width: int = 0
    height: int = 0
    pixel_format: int = 0
    nbytes: int = 0
    layout_version: int = LAYOUT_VERSION


def pack_header(header: SegmentHeader) -> bytes:
    segment_size(header.slot_bytes)
    try:
        return _LAYOUT.pack(SEGMENT_MAGIC, header.layout_version, header.slot_bytes, header.handshake_nonce,
                            header.worker_pid, header.begin_seq, header.end_seq, header.width, header.height,
                            header.pixel_format, header.nbytes)
    except struct.error:
        raise ProtocolError("header_field_range") from None


def unpack_header(buffer: object, *, expected_slot_bytes: int | None = None) -> SegmentHeader:
    """Parse and validate a header. Raises ``ProtocolError`` for a short buffer, wrong magic/version, non-zero reserved bytes or an
    unexpected slot size."""
    view = memoryview(buffer)
    if len(view) < HEADER_BYTES:
        raise ProtocolError("header_short")
    fields = _LAYOUT.unpack(view[:HEADER_BYTES])
    magic, version, slot, nonce, pid, begin, end, width, height, fmt, nbytes = fields
    if magic != SEGMENT_MAGIC:
        raise ProtocolError("header_magic")
    if version != LAYOUT_VERSION:
        raise ProtocolError("header_version")
    if bytes(view[48:HEADER_BYTES]) != bytes(16) or bytes(view[41:44]) != bytes(3) or bytes(view[10:12]) != bytes(2):
        raise ProtocolError("header_reserved")
    segment_size(slot)
    if expected_slot_bytes is not None and slot != expected_slot_bytes:
        raise ProtocolError("header_slot_mismatch")
    if len(view) < HEADER_BYTES + slot:
        raise ProtocolError("header_segment_short")
    return SegmentHeader(slot, nonce, pid, begin, end, width, height, fmt, nbytes, version)


def fence_is_stable(header: SegmentHeader, seq: int) -> bool:
    """True when both write fences equal the image sequence: the worker finished writing image ``seq``."""
    return header.begin_seq == header.end_seq == seq


def validate_image(message: ImageReady, header: SegmentHeader, *, max_width: int, max_height: int, max_image_bytes: int) -> None:
    """Parent-side check of a IMAGE_READY message against the segment header and the configured limits (design 7.4, step 3).

    Raises ``ProtocolError`` unless the fences equal the sequence, the header repeats the message exactly,
    ``nbytes == width * height * bytes_per_pixel``, and size and dimensions fit the slot and the limits.
    """
    if not fence_is_stable(header, message.seq):
        raise ProtocolError("image_fence")
    if (header.width, header.height, header.pixel_format, header.nbytes) != (
        message.width, message.height, message.pixel_format, message.nbytes
    ):
        raise ProtocolError("image_metadata_mismatch")
    if message.nbytes != message.width * message.height * bytes_per_pixel(message.pixel_format):
        raise ProtocolError("image_size_mismatch")
    if message.nbytes > header.slot_bytes:
        raise ProtocolError("image_exceeds_slot")
    if message.nbytes > max_image_bytes or message.width > max_width or message.height > max_height:
        raise ProtocolError("image_exceeds_limits")
    if max_width > ABSOLUTE_MAX_DIMENSION or max_height > ABSOLUTE_MAX_DIMENSION:
        raise ProtocolError("limits_range")
