"""Worker-side command handler with a real decoder (DB-03 Wave 4B-3a).

``DecoderHandler`` extends the handshake-only ``ProductionHandler`` with ``OPEN``, ``READ`` and ``CLOSE`` over the Wave 4
OpenCV adapter. It is the only worker code that couples to the Wave 3/4 packages (owner decision E2); OpenCV itself is still
imported lazily by that adapter on the first ``OPEN``, never during the handshake, and this module imports neither
``cv2`` nor NumPy.

Rules:

* the address arrives only inside ``OPEN``, goes straight to the adapter and is not kept; no text of any kind is sent back,
  only status codes from the frozen table (D13) -- a category outside the table is ``WORKER_INTERNAL``;
* an image is written to the shared slot only through ``WorkerSegment.publish`` and announced with ``IMAGE_READY``;
* limits come from ``INIT`` (width, height, slot size = maximum image bytes) and the timeouts from ``OPEN``;
* a decoder is single use: after any error status every later ``OPEN``/``READ`` is ``INVALID_STATE``; the parent ends the worker;
* an unexpected exception becomes ``WORKER_INTERNAL`` instead of a traceback.

The adapter is injected through ``decoder_factory`` so that a test entry point can supply a fake ``cv2``; production
(``decoder_main``) uses the default.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable

from . import protocol as P
from .worker_main import EXIT_PROTOCOL, ProductionHandler

__all__ = ["DecoderHandler", "default_decoder_factory"]

_FORMAT_CODES = {"gray8": 1, "bgr8": 3}          # PixelFormat.value -> wire code; rgb8 cannot come from OpenCV


def default_decoder_factory(config: Any) -> Any:
    """The real adapter, imported only when the first ``OPEN`` arrives."""
    from ..opencv_preview_decoder import OpenCvImageDecoder
    return OpenCvImageDecoder(config)


def _status_of(exc: BaseException) -> int:
    category = getattr(exc, "category", None)
    return P.status_for_category(category) if isinstance(category, str) else int(P.Status.WORKER_INTERNAL)


class DecoderHandler(ProductionHandler):
    def __init__(self, decoder_factory: Callable[[Any], Any] = default_decoder_factory) -> None:
        super().__init__()
        self._factory = decoder_factory
        self._decoder: Any = None
        self._limits: tuple[int, int, int] | None = None     # max_width, max_height, slot_bytes (from INIT)
        self._opened = False
        self._dead = False                                   # single use: set after any error status
        self._seq = 0

    def __repr__(self) -> str:
        return f"DecoderHandler(opened={self._opened}, dead={self._dead})"

    # --- dispatch ---------------------------------------------------------------------------------

    def handle(self, message: P.Message) -> tuple[list[bytes], bool]:
        try:
            if isinstance(message, P.Init):
                replies, done = super().handle(message)
                if self.slot is not None:
                    self._limits = (message.max_width, message.max_height, message.slot_bytes)
                return replies, done
            if not self.initialised:
                os._exit(EXIT_PROTOCOL)
            if isinstance(message, P.Open):
                return self._open(message)
            if isinstance(message, P.Read):
                return self._read(message)
            if isinstance(message, P.Close):
                return self._close(message)
            return super().handle(message)                   # PING, and the protocol-violation exits
        except Exception:
            self._dead = True
            op = getattr(message, "op_id", 0)
            return [P.encode(P.Result(op, int(P.Status.WORKER_INTERNAL)))], False

    def _fail(self, op_id: int, status: int) -> tuple[list[bytes], bool]:
        self._dead = True
        return [P.encode(P.Result(op_id, int(status)))], False

    # --- commands -----------------------------------------------------------------------------------

    def _open(self, message: P.Open) -> tuple[list[bytes], bool]:
        if self._dead or self._opened or self._decoder is not None or self.slot is None or self._limits is None:
            return self._fail(message.op_id, P.Status.INVALID_STATE)
        try:
            from ..device_runtime.preview_models import ImageLimits
            from ..opencv_preview_decoder import OpenCvDecoderConfig
            from ..seestar_preview import StreamEndpoint
            width, height, slot = self._limits
            config = OpenCvDecoderConfig(
                open_timeout_ms=message.open_timeout_ms,
                read_timeout_ms=message.read_timeout_ms,
                limits=ImageLimits(max_width=width, max_height=height, max_image_bytes=slot, max_buffer_bytes=slot),
            )
            self._decoder = self._factory(config)
            self._decoder.open(StreamEndpoint("worker", message.address))
        except Exception as exc:
            return self._fail(message.op_id, _status_of(exc))
        self._opened = True
        return [P.encode(P.Result(message.op_id, int(P.Status.OK)))], False

    def _read(self, message: P.Read) -> tuple[list[bytes], bool]:
        if self._dead or not self._opened:
            return self._fail(message.op_id, P.Status.NOT_OPEN if not self._opened and not self._dead else P.Status.INVALID_STATE)
        started = time.monotonic_ns()
        try:
            pixels = self._decoder.read()
            decode_ns = max(0, time.monotonic_ns() - started)
            if pixels is None:
                return self._fail(message.op_id, P.Status.READ_FAILED)
            code = _FORMAT_CODES.get(getattr(getattr(pixels, "pixel_format", None), "value", None))
            data = getattr(pixels, "data", None)
            if code is None:
                return self._fail(message.op_id, P.Status.UNSUPPORTED_FORMAT)
            if not isinstance(data, bytes):
                return self._fail(message.op_id, P.Status.DECODER_BAD_OUTPUT)
            if len(data) > self.slot.slot_bytes:
                return self._fail(message.op_id, P.Status.IMAGE_EXCEEDS_LIMITS)
            seq = self._seq + 1
            try:
                self.slot.publish(seq, pixels.width, pixels.height, code, data)
            except P.ProtocolError:
                return self._fail(message.op_id, P.Status.DECODER_BAD_OUTPUT)
            self._seq = seq
            reply = P.ImageReady(message.op_id, seq, pixels.width, pixels.height, code, len(data), decode_ns)
        except Exception as exc:
            return self._fail(message.op_id, _status_of(exc))
        return [P.encode(reply)], False

    def _close(self, message: P.Close) -> tuple[list[bytes], bool]:
        status = P.Status.OK
        decoder, self._decoder = self._decoder, None
        self._opened = False
        if decoder is not None:
            try:
                decoder.close()
            except Exception:
                status = P.Status.RELEASE_FAILED
        return [P.encode(P.Result(message.op_id, int(status)))], True
