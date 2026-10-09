"""Optional OpenCV/FFmpeg ``ImageDecoder`` for the Seestar preview integration (DB-03 Wave 4).

OpenCV is an optional, lazily imported dependency. Importing this package does
not import OpenCV or NumPy and makes no network access; nothing here is needed
by DB-01, DB-02 or the rest of DB-03. The decoder implements the existing
``seestar_preview.ImageDecoder`` interface, forces the FFmpeg backend, and
offers soft timeouts only. It gives no hard containment of a hung capture; see
docs/DB-03_OPENCV_DECODER.md.
"""

from .decoder import OpenCvDecoderConfig, OpenCvImageDecoder, make_opencv_decoder_factory

__all__ = ["OpenCvDecoderConfig", "OpenCvImageDecoder", "make_opencv_decoder_factory"]
