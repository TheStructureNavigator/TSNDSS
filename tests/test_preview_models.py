"""DB-03 Wave 1: preview image models, limits and pixel validation."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from tsn_dss.engine.device_runtime.preview_models import (
    ABSOLUTE_MAX_DIMENSION,
    ExposureTimeStatus,
    FreshnessReason,
    ImageFreshness,
    ImageLimits,
    InvalidImage,
    PixelFormat,
    PreviewImageEvidence,
    PreviewPixels,
    PreviewSourceError,
    SourceImage,
    digest_of,
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def evidence(**overrides):
    values = dict(
        stream_id="stream-0001", provider_id="prov", connection_id="conn-1", source_label="cam_a",
        sequence=1, host_observed_at=T0, width=2, height=2, pixel_format=PixelFormat.GRAY8, byte_length=4,
        content_digest=digest_of(b"abcd"), freshness=ImageFreshness.FRESH,
        freshness_reason=FreshnessReason.WITHIN_MAX_AGE, simulated=True,
    )
    values.update(overrides)
    return PreviewImageEvidence(**values)


class PixelTests(unittest.TestCase):
    def test_valid_pixels_per_format(self) -> None:
        for fmt, bpp in ((PixelFormat.GRAY8, 1), (PixelFormat.RGB8, 3), (PixelFormat.BGR8, 3)):
            px = PreviewPixels(3, 2, fmt, bytes(3 * 2 * bpp))
            self.assertEqual(px.byte_length, 6 * bpp)

    def test_length_mismatch_rejected(self) -> None:
        for size in (0, 5, 7):
            with self.assertRaises(InvalidImage) as ctx:
                PreviewPixels(2, 3, PixelFormat.GRAY8, bytes(size))
            self.assertEqual(ctx.exception.category, "length_mismatch")

    def test_non_bytes_rejected(self) -> None:
        with self.assertRaises(InvalidImage):
            PreviewPixels(1, 1, PixelFormat.GRAY8, bytearray(1))  # type: ignore[arg-type]

    def test_dimension_boundaries(self) -> None:
        for bad in (0, -1, True):
            with self.assertRaises(ValueError):
                PreviewPixels(bad, 1, PixelFormat.GRAY8, b"x")  # type: ignore[arg-type]
        with self.assertRaises(InvalidImage) as ctx:
            PreviewPixels(ABSOLUTE_MAX_DIMENSION + 1, 1, PixelFormat.GRAY8, b"x")
        self.assertEqual(ctx.exception.category, "dimensions_out_of_range")

    def test_absolute_size_ceiling_checked_before_allocation_mismatch(self) -> None:
        with self.assertRaises(InvalidImage) as ctx:
            PreviewPixels(ABSOLUTE_MAX_DIMENSION, ABSOLUTE_MAX_DIMENSION, PixelFormat.RGB8, b"")
        self.assertEqual(ctx.exception.category, "image_too_large")

    def test_limits_boundary(self) -> None:
        limits = ImageLimits(max_width=4, max_height=4, max_image_bytes=16, max_buffer_images=2, max_buffer_bytes=32)
        PreviewPixels(4, 4, PixelFormat.GRAY8, bytes(16)).check_limits(limits)
        with self.assertRaises(InvalidImage) as ctx:
            PreviewPixels(5, 1, PixelFormat.GRAY8, bytes(5)).check_limits(limits)
        self.assertEqual(ctx.exception.category, "dimensions_exceed_limits")
        with self.assertRaises(InvalidImage) as ctx:
            PreviewPixels(2, 4, PixelFormat.RGB8, bytes(24)).check_limits(limits)
        self.assertEqual(ctx.exception.category, "image_exceeds_limits")

    def test_limits_validation(self) -> None:
        with self.assertRaises(ValueError):
            ImageLimits(max_width=ABSOLUTE_MAX_DIMENSION + 1)
        with self.assertRaises(ValueError):
            ImageLimits(max_buffer_images=17)
        with self.assertRaises(ValueError):
            ImageLimits(max_image_bytes=100, max_buffer_bytes=99)
        with self.assertRaises(ValueError):
            ImageLimits(max_width=0)

    def test_pixels_repr_hides_data_and_memory_is_readonly_view(self) -> None:
        px = PreviewPixels(2, 1, PixelFormat.GRAY8, b"\x01\x02")
        self.assertNotIn("x01", repr(px))
        view = px.memory()
        self.assertTrue(view.readonly)
        self.assertEqual(bytes(view), b"\x01\x02")
        self.assertEqual(px.digest(), digest_of(b"\x01\x02"))

    def test_source_image_requires_pixels_and_aware_time(self) -> None:
        with self.assertRaises(InvalidImage):
            SourceImage(None)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            SourceImage(PreviewPixels(1, 1, PixelFormat.GRAY8, b"x"), datetime(2026, 1, 1))


class EvidenceTests(unittest.TestCase):
    def test_evidence_carries_no_pixels_and_is_not_canonical(self) -> None:
        ev = evidence()
        self.assertFalse(hasattr(ev, "data"))
        self.assertFalse(ev.is_canonical_record)
        self.assertTrue(ev.is_runtime_evidence_only)

    def test_time_basis_is_host_receipt_and_exposure_unknown(self) -> None:
        ev = evidence()
        self.assertEqual(ev.time_basis, "host_receipt")
        self.assertIs(ev.exposure_time_status, ExposureTimeStatus.UNKNOWN)
        claimed = evidence(provider_reported_at=T0 - timedelta(seconds=1))
        self.assertIs(claimed.exposure_time_status, ExposureTimeStatus.PROVIDER_REPORTED)
        self.assertEqual(claimed.time_basis, "host_receipt")

    def test_evidence_validation(self) -> None:
        for override in (
            {"host_observed_at": datetime(2026, 1, 1)},
            {"sequence": 0},
            {"byte_length": 5},
            {"content_digest": "ABC"},
            {"source_label": "bad label"},
            {"stream_id": " "},
        ):
            with self.assertRaises(ValueError, msg=str(override)):
                evidence(**override)

    def test_source_error_category_sanitized(self) -> None:
        self.assertEqual(PreviewSourceError("link_down").category, "link_down")
        self.assertEqual(PreviewSourceError("rtsp://10.0.0.1/x").category, "source_error")
        self.assertEqual(PreviewSourceError("").category, "source_error")


if __name__ == "__main__":
    unittest.main()
