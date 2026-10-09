"""DB-03 Wave 4: the OpenCV decoder adapter against a fake cv2 module (no OpenCV, no network)."""

from __future__ import annotations

import sys
import threading
import unittest

from tsn_dss.engine.device_runtime.preview_models import ImageLimits, PixelFormat, PreviewSourceError
from tsn_dss.engine.opencv_preview_decoder import OpenCvDecoderConfig, OpenCvImageDecoder, make_opencv_decoder_factory
from tsn_dss.engine.opencv_preview_decoder.decoder import MIN_OPENCV_VERSION
from tsn_dss.engine.seestar_preview import DecoderError, ImageDecoder, SeestarPreviewConfig
from tsn_dss.engine.seestar_preview.source import RtspPreviewSource

try:
    from opencv_fakes import FakeCapture, FakeCv2, FakeFrame, bgr, loader
    from seestar_support import HOST
except ModuleNotFoundError:  # pragma: no cover
    from tests.opencv_fakes import FakeCapture, FakeCv2, FakeFrame, bgr, loader
    from tests.seestar_support import HOST

ENDPOINT = SeestarPreviewConfig(host=HOST).endpoint("main")
SMALL = ImageLimits(max_width=8, max_height=8, max_image_bytes=64, max_buffer_images=2, max_buffer_bytes=128)


def decoder(config=None):
    return OpenCvImageDecoder(config, module_loader=loader)


def category_of(call) -> str:
    try:
        call()
    except DecoderError as exc:
        return exc.category
    raise AssertionError("DecoderError not raised")


class ConfigTests(unittest.TestCase):
    def test_defaults_are_tighter_than_opencv_defaults(self) -> None:
        config = OpenCvDecoderConfig()
        self.assertLess(config.open_timeout_ms, 30000)
        self.assertLess(config.read_timeout_ms, 30000)

    def test_validation(self) -> None:
        for kw in ({"open_timeout_ms": 99}, {"read_timeout_ms": 60001}, {"open_timeout_ms": True}, {"read_timeout_ms": "3000"},
                   {"limits": object()}):
            with self.assertRaises(ValueError, msg=str(kw)):
                OpenCvDecoderConfig(**kw)
        OpenCvDecoderConfig(open_timeout_ms=100, read_timeout_ms=60000)

    def test_implements_the_existing_interface(self) -> None:
        self.assertIsInstance(decoder(), ImageDecoder)
        self.assertEqual({n for n in dir(ImageDecoder) if not n.startswith("_")}, {"open", "read", "close"})
        self.assertFalse(decoder().simulated)


class ImportTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeCv2.reset()

    def test_construction_and_import_of_the_package_load_no_opencv(self) -> None:
        self.assertNotIn("cv2", sys.modules)
        dec = decoder()
        self.assertEqual(FakeCapture.instances, [])
        self.assertIsNotNone(dec)

    def test_missing_or_broken_opencv_is_a_category_not_a_traceback(self) -> None:
        for error in (ImportError(f"no module named cv2 near {HOST}"), OSError("libGL"), RuntimeError("x")):
            def broken(error=error):
                raise error
            dec = OpenCvImageDecoder(module_loader=broken)
            exc = None
            try:
                dec.open(ENDPOINT)
            except DecoderError as caught:
                exc = caught
            self.assertEqual(exc.category, "opencv_unavailable")
            self.assertNotIn(HOST, str(exc) + repr(exc))
            self.assertIsNone(exc.__cause__)

    def test_default_loader_without_opencv_installed_fails_cleanly(self) -> None:
        try:
            import cv2  # noqa: F401
        except ImportError:
            self.assertEqual(category_of(lambda: OpenCvImageDecoder().open(ENDPOINT)), "opencv_unavailable")
        else:  # pragma: no cover - the repository has no OpenCV dependency; do not exercise a real one here
            self.skipTest("OpenCV happens to be installed; tests never open a real stream")

    def test_version_gate(self) -> None:
        for version, expected in (("4.7.0", "opencv_version_unsupported"), ("3.4.13", "opencv_version_unsupported"),
                                  ("garbage", "opencv_version_unknown"), (None, "opencv_version_unknown")):
            FakeCv2.reset(__version__=version)
            self.assertEqual(category_of(lambda: decoder().open(ENDPOINT)), expected, version)
            self.assertEqual(FakeCapture.instances, [])  # nothing was opened on an unsupported version
        for version in ("4.8.0", "4.10.0-dev", "5.0.0", "4.12"):
            FakeCv2.reset(__version__=version)
            decoder().open(ENDPOINT)
        self.assertEqual(MIN_OPENCV_VERSION, (4, 8, 0))

    def test_incompatible_module_is_refused(self) -> None:
        class Partial:
            __version__ = "4.10.0"
            CAP_FFMPEG = 1900

        self.assertEqual(category_of(lambda: OpenCvImageDecoder(module_loader=lambda: Partial).open(ENDPOINT)), "opencv_incompatible")


class OpenTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeCv2.reset()

    def test_ffmpeg_backend_is_forced_with_both_timeouts(self) -> None:
        decoder(OpenCvDecoderConfig(open_timeout_ms=1234, read_timeout_ms=2345)).open(ENDPOINT)
        capture = FakeCapture.instances[0]
        self.assertEqual(capture.api, FakeCv2.CAP_FFMPEG)
        self.assertEqual(capture.params, [FakeCv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 1234, FakeCv2.CAP_PROP_READ_TIMEOUT_MSEC, 2345])
        self.assertEqual(capture.address, ENDPOINT.address)
        self.assertEqual(len(FakeCapture.instances), 1)  # no second attempt, no other backend

    def test_other_backend_is_never_accepted(self) -> None:
        for name in ("GSTREAMER", "ANY", "", "ffmpeg"):
            FakeCv2.reset(backend_name=name)
            self.assertEqual(category_of(lambda: decoder().open(ENDPOINT)), "backend_mismatch", name)
            self.assertEqual(FakeCapture.instances[0].release_calls, 1)

    def test_unverifiable_backend_is_refused(self) -> None:
        FakeCv2.reset(backend_error=AttributeError("old opencv"))
        self.assertEqual(category_of(lambda: decoder().open(ENDPOINT)), "backend_unverified")
        self.assertEqual(FakeCapture.instances[0].release_calls, 1)

    def test_open_failures(self) -> None:
        for knobs in ({"open_result": False}, {"construct_error": RuntimeError(f"cannot open rtsp://{HOST}:4554/stream")},
                      {"isopened_error": RuntimeError("x")}):
            FakeCv2.reset(**knobs)
            dec = decoder()
            exc = None
            try:
                dec.open(ENDPOINT)
            except DecoderError as caught:
                exc = caught
            self.assertEqual(exc.category, "open_failed", knobs)
            self.assertNotIn(HOST, str(exc) + repr(exc))
            self.assertNotIn("rtsp://", str(exc) + repr(exc))
            for capture in FakeCapture.instances:  # whatever was created is released exactly once
                self.assertEqual(capture.release_calls, 1)

    def test_a_decoder_is_never_reopened(self) -> None:
        dec = decoder()
        dec.open(ENDPOINT)
        self.assertEqual(category_of(lambda: dec.open(ENDPOINT)), "invalid_state")
        dec.close()
        self.assertEqual(category_of(lambda: dec.open(ENDPOINT)), "invalid_state")
        failed = decoder()
        FakeCv2.reset(open_result=False)
        self.assertEqual(category_of(lambda: failed.open(ENDPOINT)), "open_failed")
        self.assertEqual(category_of(lambda: failed.open(ENDPOINT)), "invalid_state")  # no automatic retry

    def test_invalid_endpoints_are_refused_before_loading_opencv(self) -> None:
        loads = []
        dec = OpenCvImageDecoder(module_loader=lambda: loads.append(1) or FakeCv2)
        for bad in (object(), None, "rtsp://x"):
            self.assertEqual(category_of(lambda: OpenCvImageDecoder(module_loader=lambda: loads.append(1) or FakeCv2).open(bad)), "invalid_endpoint")
        self.assertEqual(loads, [])
        self.assertIsNotNone(dec)


class ReadTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeCv2.reset()

    def opened(self, *script, config=None):
        FakeCv2.script = list(script)
        dec = decoder(config)
        dec.open(ENDPOINT)
        return dec, FakeCapture.instances[-1]

    def test_color_and_gray_frames(self) -> None:
        dec, _ = self.opened((True, bgr(4, 2, 9)), (True, FakeFrame((2, 3), fill=5)), (True, FakeFrame((2, 3, 1), fill=6)))
        color = dec.read()
        self.assertEqual((color.width, color.height, color.pixel_format, color.byte_length), (4, 2, PixelFormat.BGR8, 24))
        self.assertEqual(color.data, bytes([9]) * 24)
        gray = dec.read()
        self.assertEqual((gray.width, gray.height, gray.pixel_format), (3, 2, PixelFormat.GRAY8))
        self.assertEqual(dec.read().pixel_format, PixelFormat.GRAY8)

    def test_unsupported_formats_and_depths(self) -> None:
        cases = {
            "unsupported_format": [FakeFrame((2, 2, 4)), FakeFrame((2, 2, 2))],
            "unsupported_depth": [FakeFrame((2, 2, 3), dtype="uint16"), FakeFrame((2, 2, 3), dtype="float32"), FakeFrame((2, 2), dtype="int8")],
            "decoder_bad_output": [FakeFrame((0, 2)), FakeFrame((2,)), FakeFrame((1, 2, 3, 4)), object(), "frame", b"bytes"],
        }
        for expected, frames in cases.items():
            for frame in frames:
                dec, _ = self.opened((True, frame))
                self.assertEqual(category_of(dec.read), expected, f"{expected}: {frame!r}")

    def test_oversize_frames_are_rejected_before_any_copy(self) -> None:
        config = OpenCvDecoderConfig(limits=SMALL)
        cases = {
            "dimensions_exceed_limits": [FakeFrame((9, 4, 3)), FakeFrame((4, 9, 3)), FakeFrame((9, 9))],
            "image_exceeds_limits": [FakeFrame((8, 8, 3))],  # 192 bytes > 64
        }
        for expected, frames in cases.items():
            for frame in frames:
                dec, _ = self.opened((True, frame), config=config)
                self.assertEqual(category_of(dec.read), expected)
                self.assertEqual(frame.tobytes_calls, 0, "no copy of an oversize frame")
        dec, _ = self.opened((True, FakeFrame((4, 4, 3))), config=config)  # 48 bytes: within the limits
        self.assertEqual(dec.read().byte_length, 48)

    def test_inconsistent_data_length_is_rejected(self) -> None:
        dec, _ = self.opened((True, FakeFrame((2, 2, 3), data=b"short")))
        self.assertEqual(category_of(dec.read), "length_mismatch")
        dec, _ = self.opened((True, FakeFrame((2, 2, 3), data=bytearray(12))))
        self.assertEqual(category_of(dec.read), "decoder_bad_output")

    def test_failed_reads_are_a_lost_stream_not_a_retry(self) -> None:
        for item in ((False, None), (False, bgr()), RuntimeError(f"timeout reading rtsp://{HOST}"), (True, None)):
            dec, capture = self.opened(item, (True, bgr()))
            expected = "decoder_bad_output" if item == (True, None) else "read_failed"
            exc = None
            try:
                dec.read()
            except DecoderError as caught:
                exc = caught
            self.assertEqual(exc.category, expected)
            self.assertNotIn(HOST, str(exc) + repr(exc))
            self.assertEqual(capture.read_calls, 1)  # the adapter itself never retries or reconnects
            self.assertEqual(len(FakeCapture.instances), FakeCapture.instances.index(capture) + 1)

    def test_read_before_open_and_after_close(self) -> None:
        self.assertEqual(category_of(decoder().read), "not_open")
        dec, capture = self.opened((True, bgr()))
        dec.close()
        self.assertEqual(category_of(dec.read), "not_open")
        self.assertEqual(capture.read_calls, 0)


class ReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeCv2.reset()

    def test_close_releases_exactly_once_and_is_idempotent(self) -> None:
        dec = decoder()
        dec.open(ENDPOINT)
        dec.close(); dec.close()
        self.assertEqual(FakeCapture.instances[0].release_calls, 1)

    def test_close_without_open_touches_nothing(self) -> None:
        decoder().close()
        self.assertEqual(FakeCapture.instances, [])

    def test_release_failure_is_a_category_and_is_not_retried(self) -> None:
        FakeCv2.reset(release_error=RuntimeError(f"release failed for {HOST}"))
        dec = decoder()
        dec.open(ENDPOINT)
        exc = None
        try:
            dec.close()
        except DecoderError as caught:
            exc = caught
        self.assertEqual(exc.category, "release_failed")
        self.assertNotIn(HOST, str(exc) + repr(exc))
        dec.close()
        self.assertEqual(FakeCapture.instances[0].release_calls, 1)

    def test_release_failure_during_a_failed_open_does_not_mask_the_open_error(self) -> None:
        FakeCv2.reset(open_result=False, release_error=RuntimeError("x"))
        self.assertEqual(category_of(lambda: decoder().open(ENDPOINT)), "open_failed")


class RedactionTests(unittest.TestCase):
    def test_repr_hides_the_address(self) -> None:
        FakeCv2.reset()
        dec = decoder()
        dec.open(ENDPOINT)
        for text in (repr(dec), str(dec)):
            self.assertNotIn(HOST, text)
            self.assertNotIn("rtsp://", text)
        self.assertNotIn(HOST, repr(vars(dec)))


class SourceIntegrationTests(unittest.TestCase):
    def test_through_the_wave3_source_categories_survive_and_nothing_leaks(self) -> None:
        FakeCv2.reset(script=[(True, bgr()), RuntimeError(f"boom rtsp://{HOST}")])
        source = RtspPreviewSource(ENDPOINT, decoder())
        source.open()
        image = source.read()
        self.assertEqual(image.pixels.pixel_format, PixelFormat.BGR8)
        self.assertIsNone(image.provider_reported_at)
        with self.assertRaises(PreviewSourceError) as ctx:
            source.read()
        self.assertEqual(ctx.exception.category, "read_failed")
        self.assertNotIn(HOST, str(ctx.exception) + repr(ctx.exception))
        source.close()
        self.assertEqual(FakeCapture.instances[0].release_calls, 1)
        self.assertFalse(source.simulated)

    def test_factory_makes_a_new_decoder_per_call(self) -> None:
        FakeCv2.reset()
        factory = make_opencv_decoder_factory(module_loader=loader)
        self.assertIsNot(factory("main"), factory("main"))
        self.assertEqual(FakeCapture.instances, [])


class UninterruptibleCallTests(unittest.TestCase):
    """What cannot be interrupted is not pretended to be: a blocked read blocks its caller."""

    def setUp(self) -> None:
        FakeCv2.reset()

    def test_a_hung_read_blocks_the_calling_thread_and_the_adapter_claims_no_timeout_of_its_own(self) -> None:
        gate = threading.Event()
        FakeCv2.reset(script=[(True, bgr())], read_gate=gate)
        dec = decoder()
        dec.open(ENDPOINT)
        outcome = {}
        worker = threading.Thread(target=lambda: outcome.update(pixels=dec.read()), daemon=True)
        worker.start()
        self.assertTrue(FakeCapture.instances[0].read_entered.wait(2))
        worker.join(0.3)
        self.assertTrue(worker.is_alive(), "the adapter has no hard timeout: the call is still blocked")
        gate.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome["pixels"].byte_length, 24)

    def test_close_during_a_blocked_read_is_deferred_never_concurrent(self) -> None:
        gate = threading.Event()
        FakeCv2.reset(script=[(True, bgr())], read_gate=gate)
        dec = decoder()
        dec.open(ENDPOINT)
        worker = threading.Thread(target=lambda: dec.read(), daemon=True)
        worker.start()
        self.assertTrue(FakeCapture.instances[0].read_entered.wait(2))
        dec.close()  # from another thread, while OpenCV is inside read()
        self.assertEqual(FakeCapture.instances[0].release_calls, 0, "no release underneath a running read")
        self.assertTrue(dec.release_pending)
        gate.set()
        worker.join(2)
        self.assertEqual(FakeCapture.instances[0].release_calls, 1)
        self.assertFalse(dec.release_pending)
        dec.close()
        self.assertEqual(FakeCapture.instances[0].release_calls, 1)

    def test_a_second_concurrent_call_is_refused(self) -> None:
        gate = threading.Event()
        FakeCv2.reset(script=[(True, bgr())], read_gate=gate)
        dec = decoder()
        dec.open(ENDPOINT)
        worker = threading.Thread(target=lambda: dec.read(), daemon=True)
        worker.start()
        self.assertTrue(FakeCapture.instances[0].read_entered.wait(2))
        self.assertEqual(category_of(dec.read), "busy")
        gate.set()
        worker.join(2)

    def test_a_close_that_never_gets_its_turn_is_reported_as_pending(self) -> None:
        gate = threading.Event()
        FakeCv2.reset(script=[(True, bgr())], read_gate=gate)
        dec = decoder()
        dec.open(ENDPOINT)
        worker = threading.Thread(target=lambda: dec.read(), daemon=True)
        worker.start()
        FakeCapture.instances[0].read_entered.wait(2)
        dec.close()
        self.assertTrue(dec.release_pending)  # the caller can see that resources are still held
        gate.set()
        worker.join(2)


class DeferredCloseRaceTests(unittest.TestCase):
    """A close that lands between a call's last look at the flag and its release of the lock is not lost."""

    class HookedLock:
        def __init__(self, on_release):
            self._lock = threading.Lock()
            self._on_release = on_release

        def acquire(self, blocking=True):
            return self._lock.acquire(blocking)

        def release(self):
            hook, self._on_release = self._on_release, None
            if hook is not None:
                hook()  # runs while the lock is still held, as a concurrent close() would see it
            self._lock.release()

    def test_close_arriving_just_before_the_lock_is_released_is_still_honored(self) -> None:
        FakeCv2.reset(script=[(True, bgr())])
        dec = decoder()
        dec.open(ENDPOINT)
        dec._lock = self.HookedLock(lambda: dec.close())
        dec.read()
        capture = FakeCapture.instances[0]
        self.assertEqual(capture.release_calls, 1)
        self.assertFalse(dec.release_pending)
        dec.close()
        self.assertEqual(capture.release_calls, 1)

    def test_the_same_window_during_open_and_close_paths(self) -> None:
        FakeCv2.reset()
        dec = decoder()
        dec._lock = self.HookedLock(lambda: dec.close())
        dec.open(ENDPOINT)
        self.assertEqual(FakeCapture.instances[0].release_calls, 1)
        dec2 = decoder()
        FakeCv2.reset()
        dec2.open(ENDPOINT)
        dec2._lock = self.HookedLock(lambda: dec2.close())
        dec2.close()
        self.assertEqual(FakeCapture.instances[0].release_calls, 1)

    def test_release_is_only_ever_called_with_the_lock_held(self) -> None:
        FakeCv2.reset(script=[(True, bgr())])
        dec = decoder()
        dec.open(ENDPOINT)
        held = []
        capture = FakeCapture.instances[0]
        original = capture.release
        capture.release = lambda: (held.append(dec._lock.locked()), original())[1]
        gate = threading.Event()
        capture.read_gate = gate
        worker = threading.Thread(target=lambda: dec.read(), daemon=True)
        worker.start()
        capture.read_entered.wait(2)
        dec.close()
        gate.set()
        worker.join(2)
        self.assertEqual(held, [True])


if __name__ == "__main__":
    unittest.main()
