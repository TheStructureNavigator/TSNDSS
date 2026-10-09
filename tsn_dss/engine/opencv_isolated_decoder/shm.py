"""Shared-memory segment of one isolated worker (DB-03 Wave 4B-2): the parent's owner side and the worker's attach side.

Ownership (design section 7, owner decisions 4B-2):

* the **parent** creates the segment, is the only side that ever unlinks it, and never hands out a view of it: pixels are
  copied into a ``bytes`` object that belongs to the parent before anything is reported;
* the **worker** only attaches (``track=False``, Python 3.13+, so that the worker's own resource tracker cannot destroy
  the segment when the worker exits) and never unlinks.

Who may touch the slot, and when, is decided by the message order (design 7.3), not by this module. The header fences
and the CRC are verification fields. They detect corruption, torn writes and a worker that wrote while the parent owned
the slot; they do not defend against a worker that is hostile.

This module is the only place with ``multiprocessing.shared_memory`` and ``zlib``.
"""

from __future__ import annotations

import secrets
import struct
import zlib
from multiprocessing import shared_memory

from .protocol import ProtocolError
from .segment import HEADER_BYTES, SegmentHeader, bytes_per_pixel, pack_header, segment_size, unpack_header

__all__ = ["ParentSegment", "SegmentClosed", "SegmentUnavailable", "WorkerSegment", "crc32_of"]


class SegmentUnavailable(Exception):
    """The segment could not be created or attached. Carries no name, path or OS message."""


class SegmentClosed(Exception):
    """The segment was used after it was closed."""


def crc32_of(data) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


class ParentSegment:
    """Created, read and unlinked by the parent only."""

    def __init__(self, slot_bytes: int) -> None:
        size = segment_size(slot_bytes)
        self.slot_bytes = slot_bytes
        self._shm = None
        self._unlinked = False
        try:
            shm = shared_memory.SharedMemory(name=secrets.token_hex(16), create=True, size=size)
        except (OSError, ValueError):
            raise SegmentUnavailable() from None
        self._shm = shm
        try:
            with memoryview(shm.buf) as buf:
                buf[:HEADER_BYTES] = pack_header(SegmentHeader(slot_bytes))
                if len(buf) < size:
                    raise SegmentUnavailable()
        except BaseException:
            self.close()
            self.unlink_name()
            raise
        self.name = shm.name

    def __repr__(self) -> str:
        return f"ParentSegment(slot_bytes={self.slot_bytes}, {'closed' if self._shm is None else 'open'})"

    @property
    def closed(self) -> bool:
        return self._shm is None

    @property
    def name_unlinked(self) -> bool:
        return self._unlinked

    def read_header(self) -> SegmentHeader:
        """A validated copy of the header. Raises ``ProtocolError`` for a wrong magic, version, slot size or reserved bytes."""
        shm = self._shm
        if shm is None:
            raise SegmentClosed()
        with memoryview(shm.buf) as buf, buf[:HEADER_BYTES] as part:
            raw = bytes(part)
        return unpack_header(raw, expected_slot_bytes=self.slot_bytes, segment_length=HEADER_BYTES + self.slot_bytes)

    def copy_pixels(self, nbytes: int) -> bytes:
        """The parent's own copy of the first ``nbytes`` of the pixel area. No view of the segment escapes."""
        shm = self._shm
        if shm is None:
            raise SegmentClosed()
        if isinstance(nbytes, bool) or not isinstance(nbytes, int) or not 1 <= nbytes <= self.slot_bytes:
            raise ProtocolError("copy_range")
        with memoryview(shm.buf) as buf, buf[HEADER_BYTES:HEADER_BYTES + nbytes] as part:
            return bytes(part)

    def unlink_name(self) -> None:
        """Remove the segment's name (POSIX). Idempotent; a no-op on Windows, where the mapping lives until every handle is closed."""
        shm = self._shm
        if shm is None or self._unlinked:
            self._unlinked = True
            return
        self._unlinked = True
        try:
            shm.unlink()
        except (FileNotFoundError, OSError):
            pass

    def close(self) -> bool:
        """Unlink the name if that has not happened, then close the parent's mapping. Idempotent. False if a view still exists."""
        shm = self._shm
        if shm is None:
            return True
        self.unlink_name()
        try:
            shm.close()
        except BufferError:
            return False
        self._shm = None
        return True


class WorkerSegment:
    """The worker's side: attach, record the handshake, publish an image. Never creates, never unlinks."""

    def __init__(self, shm, slot_bytes: int) -> None:
        self._shm = shm
        self.slot_bytes = slot_bytes

    @classmethod
    def attach(cls, name: str, slot_bytes: int) -> "WorkerSegment":
        try:
            shm = shared_memory.SharedMemory(name=name, create=False, track=False)
        except (OSError, ValueError, TypeError):
            raise SegmentUnavailable() from None
        try:
            with memoryview(shm.buf) as buf, buf[:HEADER_BYTES] as part:
                raw = bytes(part)
            unpack_header(raw, expected_slot_bytes=slot_bytes, segment_length=shm.size)
        except BaseException:
            try:
                shm.close()
            except BufferError:
                pass
            raise SegmentUnavailable() from None
        return cls(shm, slot_bytes)

    @property
    def buffer(self) -> memoryview:
        """Raw access for test fixtures that must write a deliberately broken slot. Production code uses ``publish``."""
        return self._shm.buf

    def record_handshake(self, nonce: int, pid: int) -> None:
        struct.pack_into("<Q", self._shm.buf, 12, nonce)
        struct.pack_into("<I", self._shm.buf, 20, pid)

    def publish(self, seq: int, width: int, height: int, pixel_format: int, pixels) -> None:
        """Write one image: begin fence, metadata, pixels, CRC, end fence (design 7.4). The caller sends IMAGE_READY afterwards."""
        nbytes = len(pixels)
        if nbytes != width * height * bytes_per_pixel(pixel_format) or not 1 <= nbytes <= self.slot_bytes:
            raise ProtocolError("publish_size")
        buf = self._shm.buf
        struct.pack_into("<I", buf, 24, seq)                                   # begin fence
        struct.pack_into("<IIBxxxI", buf, 32, width, height, pixel_format, nbytes)
        buf[HEADER_BYTES:HEADER_BYTES + nbytes] = pixels
        struct.pack_into("<I", buf, 48, crc32_of(pixels))
        struct.pack_into("<I", buf, 28, seq)                                   # end fence

    def close(self) -> None:
        try:
            self._shm.close()
        except BufferError:
            pass
