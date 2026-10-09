"""INTERNAL: RTSP preview source over an injected decoder seam.

Not part of the package's public API. A stream is opened through
``build_preview_manager`` and ``PreviewStreamManager.open_stream`` only, which
consult the readiness gate first; this module has no gate of its own. Tests and
``integration`` import it directly. Only ``DecoderError`` and ``ImageDecoder``
are re-exported by the package, because adapters implement them.

``RtspPreviewSource`` implements the neutral ``PreviewSource`` protocol. It does
not decode anything itself: a decoder object is injected and is the only thing
that would ever touch a network (a Wave 4 adapter; tests use fakes). The source
adds no command of any kind: ``open`` attaches the decoder to an endpoint that
the readiness gate already found to be serving, ``read`` returns pixels, and
``close`` releases the decoder exactly once.

Rules:
* every decoder fault becomes a ``PreviewSourceError`` with a fixed category;
  decoder messages (which may contain addresses) are never kept;
* the source never invents a time: images carry no source-reported timestamp;
* a decoder's ``read`` must return promptly (pixels, or ``None`` for "nothing
  now"); making a blocking library honor that is the adapter's job.
"""

from __future__ import annotations

from typing import Callable, Protocol, runtime_checkable

from ..device_runtime.preview_models import PreviewPixels, PreviewSourceError, SourceImage
from ..device_runtime.preview_stream import PreviewSource
from .config import SeestarPreviewConfig, StreamEndpoint

__all__ = ["DecoderError", "ImageDecoder", "RtspPreviewSource", "make_source_factory"]


class DecoderError(Exception):
    """A decoder fault. ``category`` is a short fixed token; the message is discarded."""

    def __init__(self, category: str = "decoder_error") -> None:
        super().__init__(category)
        self.category = category


@runtime_checkable
class ImageDecoder(Protocol):
    """Decodes one stream into pixels. It has no command surface."""

    def open(self, endpoint: StreamEndpoint) -> None: ...

    def read(self) -> PreviewPixels | None: ...

    def close(self) -> None: ...


def _category(exc: Exception, default: str) -> str:
    return exc.category if isinstance(exc, (DecoderError, PreviewSourceError)) else default


class RtspPreviewSource:
    def __init__(self, endpoint: StreamEndpoint, decoder: ImageDecoder) -> None:
        self._endpoint = endpoint
        self._decoder = decoder
        self._attempted = False
        self._opened = False
        self._released = False

    def __repr__(self) -> str:
        return f"RtspPreviewSource(camera={self._endpoint.camera!r}, <redacted>)"

    @property
    def simulated(self) -> bool:
        """True only when the injected decoder declares itself simulated."""
        return bool(getattr(self._decoder, "simulated", False))

    def _release(self) -> None:
        if self._released or not self._attempted:
            return
        self._released = True
        self._decoder.close()

    def open(self) -> None:
        if self._attempted:
            raise PreviewSourceError("invalid_state")
        self._attempted = True
        try:
            self._decoder.open(self._endpoint)
        except Exception as exc:
            category = _category(exc, "decoder_error")
            try:
                self._release()
            except Exception:
                pass
            raise PreviewSourceError(category) from None
        self._opened = True

    def read(self) -> SourceImage | None:
        if not self._opened or self._released:
            raise PreviewSourceError("not_open")
        try:
            pixels = self._decoder.read()
        except Exception as exc:
            raise PreviewSourceError(_category(exc, "decoder_error")) from None
        if pixels is None:
            return None
        if not isinstance(pixels, PreviewPixels):
            raise PreviewSourceError("decoder_bad_output")
        return SourceImage(pixels, None)

    def close(self) -> None:
        try:
            self._release()
        except Exception as exc:
            raise PreviewSourceError(_category(exc, "decoder_close_failed")) from None


def make_source_factory(
    config: SeestarPreviewConfig, decoder_factory: Callable[[str], ImageDecoder]
) -> Callable[[str], PreviewSource]:
    """Factory for the manager. Called only after the gate allowed; building a source opens nothing."""

    def factory(camera: str) -> PreviewSource:
        endpoint = config.endpoint(camera)  # raises for an unconfigured camera
        return RtspPreviewSource(endpoint, decoder_factory(camera))

    return factory
