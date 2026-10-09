"""OpenCV/FFmpeg decoder behind the ``ImageDecoder`` interface.

Contract with the rest of DB-03: ``open(endpoint)``, ``read()`` and ``close()``
only. Anything that goes wrong becomes a ``DecoderError`` with a fixed category;
OpenCV's own messages (which may contain the stream address) are never kept.

What this adapter does:

* imports ``cv2`` lazily, on ``open``; a missing or broken import is
  ``opencv_unavailable``; OpenCV older than 4.8 is refused (the timeout
  properties used below are documented as available from 4.8), as is a version
  it cannot read;
* opens ``cv2.VideoCapture(address, cv2.CAP_FFMPEG, [open timeout, read
  timeout])``: the FFmpeg backend is requested explicitly and, after opening,
  confirmed with ``getBackendName()``; any other answer is a refusal. There is
  no fallback backend and no automatic reconnect;
* validates every frame before copying it (dtype, channel count, dimensions,
  byte size against ``ImageLimits``) and returns ``PreviewPixels`` in BGR8 or
  GRAY8 (OpenCV's native layouts);
* treats a failed read as a lost stream (``read_failed``): OpenCV does not tell
  end-of-stream, timeout and decode error apart, and a capture whose read was
  aborted by the interrupt callback is not trusted afterwards. Recovery is a new
  stream after new readiness evidence, never a retry here.

What it does NOT do: it gives no hard timeout. The timeouts are OpenCV's own
soft timeouts (an FFmpeg interrupt callback). They bound waits that FFmpeg
checks; they do not bound everything (for example a blocking name resolution or
a codec/driver stall), and a call that never returns blocks the calling thread.
The adapter never calls ``release()`` while another thread is inside ``open`` or
``read``; a ``close`` that arrives then is deferred until that call returns.
"""

from __future__ import annotations

import importlib
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from ..device_runtime.preview_models import ImageLimits, InvalidImage, PixelFormat, PreviewPixels
from ..seestar_preview import DecoderError, StreamEndpoint

__all__ = ["MIN_OPENCV_VERSION", "OpenCvDecoderConfig", "OpenCvImageDecoder", "make_opencv_decoder_factory"]

MIN_OPENCV_VERSION = (4, 8, 0)
MIN_TIMEOUT_MS = 100
MAX_TIMEOUT_MS = 60_000
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")


@dataclass(slots=True, frozen=True)
class OpenCvDecoderConfig:
    open_timeout_ms: int = 5000
    read_timeout_ms: int = 3000
    limits: ImageLimits = field(default_factory=ImageLimits)

    def __post_init__(self) -> None:
        for name in ("open_timeout_ms", "read_timeout_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not MIN_TIMEOUT_MS <= value <= MAX_TIMEOUT_MS:
                raise ValueError(f"{name} must be an integer between {MIN_TIMEOUT_MS} and {MAX_TIMEOUT_MS}.")
        if not isinstance(self.limits, ImageLimits):
            raise ValueError("limits must be ImageLimits.")


def _default_loader() -> Any:
    return importlib.import_module("cv2")


def _parse_version(module: Any) -> tuple[int, int, int] | None:
    text = getattr(module, "__version__", None)
    match = _VERSION_RE.match(text) if isinstance(text, str) else None
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))


def _decoded_to_pixels(frame: Any, limits: ImageLimits) -> PreviewPixels:
    """Validate shape, depth and size BEFORE any copy, then copy once."""
    shape = getattr(frame, "shape", None)
    if not isinstance(shape, tuple) or len(shape) not in (2, 3) or any(
        isinstance(n, bool) or not isinstance(n, int) or n < 1 for n in shape
    ):
        raise DecoderError("decoder_bad_output")
    if str(getattr(frame, "dtype", "")) != "uint8":
        raise DecoderError("unsupported_depth")
    if len(shape) == 2 or shape[2] == 1:
        pixel_format = PixelFormat.GRAY8
    elif shape[2] == 3:
        pixel_format = PixelFormat.BGR8
    else:
        raise DecoderError("unsupported_format")
    height, width = shape[0], shape[1]
    if width > limits.max_width or height > limits.max_height:
        raise DecoderError("dimensions_exceed_limits")
    if width * height * pixel_format.bytes_per_pixel > limits.max_image_bytes:
        raise DecoderError("image_exceeds_limits")
    try:
        data = frame.tobytes()
    except Exception:
        raise DecoderError("decoder_bad_output") from None
    if not isinstance(data, bytes):
        raise DecoderError("decoder_bad_output")
    try:
        pixels = PreviewPixels(width, height, pixel_format, data)
        pixels.check_limits(limits)
    except InvalidImage as exc:
        raise DecoderError(exc.category) from None
    return pixels


class OpenCvImageDecoder:
    """``ImageDecoder`` over ``cv2.VideoCapture`` with the FFmpeg backend. Single use; no reconnect."""

    simulated = False

    def __init__(self, config: OpenCvDecoderConfig | None = None, *, module_loader: Callable[[], Any] = _default_loader) -> None:
        self._config = config or OpenCvDecoderConfig()
        self._loader = module_loader
        self._lock = threading.Lock()
        self._capture: Any = None
        self._attempted = False
        self._released = False
        self._close_requested = False
        self._camera: str | None = None
        self.deferred_release_error: str | None = None

    def __repr__(self) -> str:
        return f"OpenCvImageDecoder(camera={self._camera!r}, <redacted>)"

    @property
    def release_pending(self) -> bool:
        """True while a ``close`` is deferred because another call is still inside OpenCV."""
        return self._close_requested and not self._released

    # --- internals ---------------------------------------------------------------------

    def _release_locked(self) -> None:
        """Release the capture exactly once. The caller holds the lock."""
        if self._released:
            return
        self._released = True
        capture, self._capture = self._capture, None
        if capture is None:
            return
        try:
            capture.release()
        except Exception:
            raise DecoderError("release_failed") from None

    def _drain_deferred(self) -> None:
        """Honor a ``close`` that arrived while a call was running.

        Called only after the lock has been released. Re-checking here closes the window in which
        ``close`` sets its flag after the running call looked at it but before it let go of the lock.
        If another call holds the lock, that call will drain on its way out.
        """
        while self._close_requested and not self._released:
            if not self._lock.acquire(blocking=False):
                return
            try:
                self._release_locked()
            except DecoderError as exc:
                self.deferred_release_error = exc.category
            finally:
                self._lock.release()

    def _load_module(self) -> Any:
        try:
            module = self._loader()
        except Exception:
            raise DecoderError("opencv_unavailable") from None
        version = _parse_version(module)
        if version is None:
            raise DecoderError("opencv_version_unknown")
        if version < MIN_OPENCV_VERSION:
            raise DecoderError("opencv_version_unsupported")
        try:
            module.CAP_FFMPEG, module.CAP_PROP_OPEN_TIMEOUT_MSEC, module.CAP_PROP_READ_TIMEOUT_MSEC
            module.VideoCapture
        except AttributeError:
            raise DecoderError("opencv_incompatible") from None
        return module

    # --- ImageDecoder ----------------------------------------------------------------------

    def open(self, endpoint: StreamEndpoint) -> None:
        if not self._lock.acquire(blocking=False):
            raise DecoderError("busy")
        try:
            if self._attempted:
                raise DecoderError("invalid_state")
            self._attempted = True  # a decoder is never reopened, whatever happens below
            address = getattr(endpoint, "address", None)
            self._camera = getattr(endpoint, "camera", None)
            if not isinstance(address, str) or not address.startswith("rtsp://"):
                raise DecoderError("invalid_endpoint")
            module = self._load_module()
            params = [
                module.CAP_PROP_OPEN_TIMEOUT_MSEC, self._config.open_timeout_ms,
                module.CAP_PROP_READ_TIMEOUT_MSEC, self._config.read_timeout_ms,
            ]
            try:
                self._capture = module.VideoCapture(address, module.CAP_FFMPEG, params)
            except Exception:
                raise DecoderError("open_failed") from None
            del address  # the only copy kept is inside OpenCV
            try:
                opened = bool(self._capture.isOpened())
            except Exception:
                opened = False
            if not opened:
                raise DecoderError("open_failed")
            try:
                backend = self._capture.getBackendName()
            except Exception:
                raise DecoderError("backend_unverified") from None
            if backend != "FFMPEG":
                raise DecoderError("backend_mismatch")
        except DecoderError:
            try:
                self._release_locked()
            except DecoderError:
                pass
            raise
        finally:
            self._lock.release()
            self._drain_deferred()

    def read(self):
        if not self._lock.acquire(blocking=False):
            raise DecoderError("busy")
        try:
            if self._capture is None or self._released or self._close_requested:
                raise DecoderError("not_open")
            try:
                ok, frame = self._capture.read()
            except Exception:
                raise DecoderError("read_failed") from None
            if not ok:
                raise DecoderError("read_failed")
            if frame is None:
                raise DecoderError("decoder_bad_output")
            return _decoded_to_pixels(frame, self._config.limits)
        finally:
            self._lock.release()
            self._drain_deferred()

    def close(self) -> None:
        if not self._lock.acquire(blocking=False):
            self._close_requested = True  # never release underneath a running call
            return
        try:
            self._release_locked()
        finally:
            self._lock.release()
            self._drain_deferred()


def make_opencv_decoder_factory(
    config: OpenCvDecoderConfig | None = None, *, module_loader: Callable[[], Any] = _default_loader
) -> Callable[[str], OpenCvImageDecoder]:
    """``decoder_factory`` for ``build_preview_manager``: one new decoder per stream, never shared."""

    def factory(camera: str) -> OpenCvImageDecoder:
        return OpenCvImageDecoder(config, module_loader=module_loader)

    return factory
