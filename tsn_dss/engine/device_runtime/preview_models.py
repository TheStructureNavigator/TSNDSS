"""Provider-neutral preview image models (DB-03 Wave 1).

Preview images are read-only runtime evidence (DSS-CTR-013 REQ-016, REQ-018,
REQ-033). Nothing here is, or becomes, a canonical Capture or Frame record, and
no pixel data is ever written to disk by this package.

Metadata and pixels are separate objects:

* ``PreviewImageEvidence`` carries metadata only (identity of the stream and
  source, host receipt time, shape, content digest, freshness). It is cheap to
  keep, log and compare.
* ``PreviewPixels`` carries the decoded bytes, in memory, for later analysis.

Time semantics: ``host_observed_at`` is when the host received the image. It is
NOT the physical exposure time, which is unknown unless the source reports one.
A source-reported time is an unverified claim by the source.

Pixel layout: row-major, tightly packed (no row padding), top row first.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import NewType

from .errors import DeviceRuntimeError
from .models import ConnectionId, ProviderId

StreamId = NewType("StreamId", str)

ABSOLUTE_MAX_DIMENSION = 16384
ABSOLUTE_MAX_IMAGE_BYTES = 256 * 1024 * 1024

_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_CATEGORY_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")

__all__ = [
    "ABSOLUTE_MAX_DIMENSION",
    "ABSOLUTE_MAX_IMAGE_BYTES",
    "ExposureTimeStatus",
    "FreshnessReason",
    "ImageFreshness",
    "ImageLimits",
    "InvalidImage",
    "PixelFormat",
    "PreviewError",
    "PreviewImageEvidence",
    "PreviewPixels",
    "PreviewSourceError",
    "SourceImage",
    "StreamId",
    "digest_of",
]


# --- errors -----------------------------------------------------------------------


class PreviewError(DeviceRuntimeError):
    """Base class for preview runtime errors."""


class InvalidImage(PreviewError):
    """An image failed shape, size or limit validation. The message is a fixed category."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class PreviewSourceError(PreviewError):
    """A source reported a fault. ``category`` is a short fixed token, never device data."""

    def __init__(self, category: str) -> None:
        if not isinstance(category, str) or not _CATEGORY_RE.match(category):
            category = "source_error"
        super().__init__(category)
        self.category = category


# --- enumerations -----------------------------------------------------------------


class PixelFormat(Enum):
    GRAY8 = "gray8"
    RGB8 = "rgb8"
    BGR8 = "bgr8"

    @property
    def bytes_per_pixel(self) -> int:
        return 1 if self is PixelFormat.GRAY8 else 3


class ImageFreshness(Enum):
    """Whether the latest image can be treated as current. Local DB-03 semantics."""

    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


class FreshnessReason(Enum):
    WITHIN_MAX_AGE = "within_max_age"
    EXCEEDS_MAX_AGE = "exceeds_max_age"
    STALL_CORROBORATED = "stall_corroborated"
    REPEATED_CONTENT_UNCORROBORATED = "repeated_content_uncorroborated"
    NO_IMAGE_YET = "no_image_yet"
    STREAM_NOT_RECEIVING = "stream_not_receiving"
    CLOCK_REGRESSION = "clock_regression"


class ExposureTimeStatus(Enum):
    """The physical exposure time is unknown unless the source reports one (unverified)."""

    UNKNOWN = "unknown"
    PROVIDER_REPORTED = "provider_reported"


# --- helpers ----------------------------------------------------------------------


def _require_label(name: str, value: str) -> None:
    if not isinstance(value, str) or not _LABEL_RE.match(value):
        raise ValueError(f"{name} must match {_LABEL_RE.pattern}.")


def _require_aware(name: str, value: datetime | None, *, optional: bool = False) -> None:
    if value is None:
        if optional:
            return
        raise ValueError(f"{name} is required.")
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime.")


def _require_positive_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer.")


def digest_of(data: bytes) -> str:
    """Lower-case SHA-256 hex digest of decoded pixel bytes."""
    return hashlib.sha256(data).hexdigest()


# --- limits -----------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ImageLimits:
    """Per-stream bounds on accepted images and on the in-memory pixel buffer."""

    max_width: int = 4096
    max_height: int = 4096
    max_image_bytes: int = 32 * 1024 * 1024
    max_buffer_images: int = 4
    max_buffer_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        for name in ("max_width", "max_height", "max_image_bytes", "max_buffer_images", "max_buffer_bytes"):
            _require_positive_int(name, getattr(self, name))
        if self.max_width > ABSOLUTE_MAX_DIMENSION or self.max_height > ABSOLUTE_MAX_DIMENSION:
            raise ValueError("dimension limit exceeds the absolute ceiling.")
        if self.max_image_bytes > ABSOLUTE_MAX_IMAGE_BYTES or self.max_buffer_bytes > ABSOLUTE_MAX_IMAGE_BYTES * 4:
            raise ValueError("byte limit exceeds the absolute ceiling.")
        if self.max_buffer_images > 16:
            raise ValueError("buffer image count exceeds the ceiling of 16.")
        if self.max_buffer_bytes < self.max_image_bytes:
            raise ValueError("the buffer must be able to hold at least one maximum-size image.")


# --- pixel data -------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class PreviewPixels:
    """Decoded pixels held in memory. Immutable ``bytes``; never persisted by this package."""

    width: int
    height: int
    pixel_format: PixelFormat
    data: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _require_positive_int("width", self.width)
        _require_positive_int("height", self.height)
        if self.width > ABSOLUTE_MAX_DIMENSION or self.height > ABSOLUTE_MAX_DIMENSION:
            raise InvalidImage("dimensions_out_of_range")
        if not isinstance(self.pixel_format, PixelFormat):
            raise ValueError("pixel_format must be a PixelFormat.")
        if not isinstance(self.data, bytes):
            raise InvalidImage("data_not_bytes")
        expected = self.width * self.height * self.pixel_format.bytes_per_pixel
        if expected > ABSOLUTE_MAX_IMAGE_BYTES:
            raise InvalidImage("image_too_large")
        if len(self.data) != expected:
            raise InvalidImage("length_mismatch")

    @property
    def byte_length(self) -> int:
        return len(self.data)

    def check_limits(self, limits: ImageLimits) -> None:
        if self.width > limits.max_width or self.height > limits.max_height:
            raise InvalidImage("dimensions_exceed_limits")
        if len(self.data) > limits.max_image_bytes:
            raise InvalidImage("image_exceeds_limits")

    def memory(self) -> memoryview:
        """Zero-copy read-only view, for later analysis (for example wrapping in an array)."""
        return memoryview(self.data)

    def digest(self) -> str:
        return digest_of(self.data)


@dataclass(slots=True, frozen=True)
class SourceImage:
    """What a ``PreviewSource`` hands to a stream: pixels plus an optional source-claimed time."""

    pixels: PreviewPixels
    provider_reported_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.pixels, PreviewPixels):
            raise InvalidImage("pixels_missing")
        _require_aware("provider_reported_at", self.provider_reported_at, optional=True)


# --- evidence ---------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class PreviewImageEvidence:
    """Metadata about one received image. Runtime evidence only; carries no pixels."""

    stream_id: StreamId
    provider_id: ProviderId
    connection_id: ConnectionId
    source_label: str
    sequence: int
    host_observed_at: datetime
    width: int
    height: int
    pixel_format: PixelFormat
    byte_length: int
    content_digest: str
    freshness: ImageFreshness
    freshness_reason: FreshnessReason
    simulated: bool
    provider_reported_at: datetime | None = None

    def __post_init__(self) -> None:
        for name in ("stream_id", "provider_id", "connection_id"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} is required.")
        _require_label("source_label", self.source_label)
        _require_positive_int("sequence", self.sequence)
        _require_aware("host_observed_at", self.host_observed_at)
        _require_aware("provider_reported_at", self.provider_reported_at, optional=True)
        _require_positive_int("width", self.width)
        _require_positive_int("height", self.height)
        _require_positive_int("byte_length", self.byte_length)
        if self.byte_length != self.width * self.height * self.pixel_format.bytes_per_pixel:
            raise ValueError("byte_length does not match the image shape.")
        if not _DIGEST_RE.match(self.content_digest):
            raise ValueError("content_digest must be a lower-case SHA-256 hex digest.")

    @property
    def exposure_time_status(self) -> ExposureTimeStatus:
        """UNKNOWN unless the source reported a time; even then it is an unverified claim."""
        return ExposureTimeStatus.UNKNOWN if self.provider_reported_at is None else ExposureTimeStatus.PROVIDER_REPORTED

    @property
    def time_basis(self) -> str:
        """``host_observed_at`` is the host receipt time, not the physical exposure time."""
        return "host_receipt"

    @property
    def is_runtime_evidence_only(self) -> bool:
        return True

    @property
    def is_canonical_record(self) -> bool:
        return False
