"""DB-03 Wave 3: RTSP preview source over a fake decoder seam."""

from __future__ import annotations

import unittest

from tsn_dss.engine.device_runtime.preview_models import PixelFormat, PreviewPixels, PreviewSourceError, SourceImage
from tsn_dss.engine.device_runtime.preview_simulator import make_pixels
from tsn_dss.engine.device_runtime.preview_stream import PreviewSource
from tsn_dss.engine.seestar_preview import DecoderError, ImageDecoder, SeestarPreviewConfig
from tsn_dss.engine.seestar_preview.source import RtspPreviewSource, make_source_factory  # internal building blocks
from tsn_dss.engine.seestar_provider.errors import SeestarConfigError

try:
    from seestar_preview_support import DecoderFactory, FakeDecoder
    from seestar_support import HOST
except ModuleNotFoundError:  # pragma: no cover
    from tests.seestar_preview_support import DecoderFactory, FakeDecoder
    from tests.seestar_support import HOST

ENDPOINT = SeestarPreviewConfig(host=HOST).endpoint("main")


def source_with(decoder) -> RtspPreviewSource:
    return RtspPreviewSource(ENDPOINT, decoder)


class ProtocolTests(unittest.TestCase):
    def test_source_and_decoder_satisfy_the_protocols(self) -> None:
        self.assertIsInstance(source_with(FakeDecoder("main")), PreviewSource)
        self.assertIsInstance(FakeDecoder("main"), ImageDecoder)

    def test_decoder_protocol_has_no_command_surface(self) -> None:
        self.assertEqual({n for n in dir(ImageDecoder) if not n.startswith("_")}, {"open", "read", "close"})
        self.assertEqual(
            {n for n in dir(RtspPreviewSource) if not n.startswith("_")}, {"simulated", "open", "read", "close"}
        )

    def test_construction_touches_nothing(self) -> None:
        decoder = FakeDecoder("main")
        source_with(decoder)
        self.assertEqual((decoder.open_calls, decoder.read_calls, decoder.close_calls), (0, 0, 0))


class LifecycleTests(unittest.TestCase):
    def test_open_read_close(self) -> None:
        decoder = FakeDecoder("main", [make_pixels(1), None, make_pixels(2)])
        source = source_with(decoder)
        source.open()
        self.assertEqual(decoder.endpoints, [ENDPOINT])
        first = source.read()
        self.assertIsInstance(first, SourceImage)
        self.assertEqual(first.pixels, make_pixels(1))
        self.assertIsNone(source.read())
        self.assertEqual(source.read().pixels, make_pixels(2))
        source.close()
        self.assertEqual((decoder.open_calls, decoder.close_calls), (1, 1))

    def test_no_time_is_ever_invented(self) -> None:
        source = source_with(FakeDecoder("main"))
        source.open()
        self.assertIsNone(source.read().provider_reported_at)

    def test_simulated_follows_the_decoder(self) -> None:
        self.assertTrue(source_with(FakeDecoder("main")).simulated)

        class Real:
            def open(self, endpoint): ...
            def read(self): return None
            def close(self): ...

        self.assertFalse(source_with(Real()).simulated)

    def test_close_is_idempotent_and_releases_once(self) -> None:
        decoder = FakeDecoder("main")
        source = source_with(decoder)
        source.open()
        source.close(); source.close()
        self.assertEqual(decoder.close_calls, 1)

    def test_close_without_open_never_touches_the_decoder(self) -> None:
        decoder = FakeDecoder("main")
        source_with(decoder).close()
        self.assertEqual(decoder.close_calls, 0)

    def test_state_errors(self) -> None:
        source = source_with(FakeDecoder("main"))
        with self.assertRaises(PreviewSourceError) as ctx:
            source.read()
        self.assertEqual(ctx.exception.category, "not_open")
        source.open()
        with self.assertRaises(PreviewSourceError) as ctx:
            source.open()
        self.assertEqual(ctx.exception.category, "invalid_state")
        source.close()
        with self.assertRaises(PreviewSourceError) as ctx:
            source.read()
        self.assertEqual(ctx.exception.category, "not_open")
        with self.assertRaises(PreviewSourceError):
            source.open()  # a source is never reopened


class DecoderFaultTests(unittest.TestCase):
    def test_open_failure_releases_the_decoder_once_and_keeps_only_the_category(self) -> None:
        decoder = FakeDecoder("main", open_error=DecoderError("connect_failed"))
        source = source_with(decoder)
        with self.assertRaises(PreviewSourceError) as ctx:
            source.open()
        self.assertEqual(ctx.exception.category, "connect_failed")
        self.assertEqual(decoder.close_calls, 1)
        source.close()
        self.assertEqual(decoder.close_calls, 1)

    def test_unexpected_open_exception_is_sanitised(self) -> None:
        decoder = FakeDecoder("main", open_error=RuntimeError(f"cannot reach {HOST}"))
        with self.assertRaises(PreviewSourceError) as ctx:
            source_with(decoder).open()
        self.assertEqual(ctx.exception.category, "decoder_error")
        self.assertNotIn(HOST, str(ctx.exception) + repr(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)
        self.assertEqual(decoder.close_calls, 1)

    def test_failing_release_during_failed_open_does_not_mask_the_open_error(self) -> None:
        decoder = FakeDecoder("main", open_error=DecoderError("connect_failed"), close_error=RuntimeError("x"))
        with self.assertRaises(PreviewSourceError) as ctx:
            source_with(decoder).open()
        self.assertEqual(ctx.exception.category, "connect_failed")

    def test_read_faults(self) -> None:
        for fault, category in (
            (DecoderError("stream_ended"), "stream_ended"),
            (RuntimeError(f"boom {HOST}"), "decoder_error"),
            (TimeoutError("t"), "decoder_error"),
        ):
            source = source_with(FakeDecoder("main", [fault]))
            source.open()
            with self.assertRaises(PreviewSourceError) as ctx:
                source.read()
            self.assertEqual(ctx.exception.category, category)
            self.assertNotIn(HOST, str(ctx.exception))

    def test_bad_decoder_output_is_rejected(self) -> None:
        for bad in (b"bytes", "pixels", 5, object(), SourceImage(make_pixels(1))):
            source = source_with(FakeDecoder("main", [bad]))
            source.open()
            with self.assertRaises(PreviewSourceError) as ctx:
                source.read()
            self.assertEqual(ctx.exception.category, "decoder_bad_output")

    def test_close_failure_is_reported_once_and_never_retried(self) -> None:
        for error, category in ((DecoderError("close_refused"), "close_refused"), (RuntimeError("x"), "decoder_close_failed")):
            decoder = FakeDecoder("main", close_error=error)
            source = source_with(decoder)
            source.open()
            with self.assertRaises(PreviewSourceError) as ctx:
                source.close()
            self.assertEqual(ctx.exception.category, category)
            source.close()
            self.assertEqual(decoder.close_calls, 1)

    def test_pixels_pass_through_unchanged(self) -> None:
        pixels = PreviewPixels(2, 2, PixelFormat.BGR8, bytes(range(12)))
        source = source_with(FakeDecoder("main", [pixels]))
        source.open()
        self.assertIs(source.read().pixels, pixels)


class RedactionTests(unittest.TestCase):
    def test_repr_hides_the_address(self) -> None:
        source = source_with(FakeDecoder("main"))
        self.assertNotIn(HOST, repr(source) + str(source))
        self.assertNotIn("rtsp://", repr(source))


class FactoryTests(unittest.TestCase):
    def test_factory_builds_a_source_without_opening_anything(self) -> None:
        decoders = DecoderFactory()
        factory = make_source_factory(SeestarPreviewConfig(host=HOST), decoders)
        source = factory("wide")
        self.assertIsInstance(source, PreviewSource)
        self.assertEqual(decoders.calls, ["wide"])
        self.assertEqual(decoders.decoders[0].open_calls, 0)
        source.open()
        self.assertTrue(decoders.decoders[0].endpoints[0].address.endswith(":4555/stream"))

    def test_factory_refuses_unconfigured_cameras_before_creating_a_decoder(self) -> None:
        decoders = DecoderFactory()
        factory = make_source_factory(SeestarPreviewConfig(host=HOST, cameras=("wide",)), decoders)
        for camera in ("main", "tele", ""):
            with self.assertRaises(SeestarConfigError):
                factory(camera)
        self.assertEqual(decoders.calls, [])


if __name__ == "__main__":
    unittest.main()
