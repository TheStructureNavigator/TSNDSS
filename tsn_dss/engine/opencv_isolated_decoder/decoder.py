"""Parent-side ``ImageDecoder`` over an isolated decoder worker (DB-03 Wave 4B-3b).

``ProcessIsolatedDecoder`` implements the existing ``seestar_preview.ImageDecoder`` contract (``open``/``read``/``close``,
nothing else) on top of ``WorkerProcess`` and the 4B-3a decoder worker. It changes no existing contract: the manager, the
gate, ``RtspPreviewSource`` and ``ImageDecoder`` are untouched.

Rules (owner decisions E1-E9 of the approved 4B-3 design):

* the constructor stores configuration only: no process, no segment, no import of OpenCV or NumPy;
* ``open`` checks the Python version (a worker is never created on an interpreter older than 3.13 -- the category
  ``python_unsupported`` reaches the stream), starts the worker (handshake), runs ``before_connect`` if one was given (``False``,
  or an exception, means ``readiness_revoked``; ``OPEN`` is then never sent) and sends ``OPEN`` with the configured timeouts;
* one ``ImageLimits`` object is authoritative: it defines the slot size and the worker limits, and the image is validated against
  it again in the parent;
* ``read`` returns a ``PreviewPixels`` built from the parent-owned copy; it never returns ``None`` and never a success it is not
  sure of: any deadline miss, lost pipe, protocol violation or inconsistent image ends the worker and raises ``DecoderError``;
* ``close`` is idempotent and safe from any thread. It never waits on a lock that a blocked ``read`` holds. If the process could
  not be confirmed gone it raises ``DecoderError("containment_failed")`` once; the slot then stays counted (decision D10);
* the address is read from the endpoint once, sent in ``OPEN`` and kept nowhere; errors carry fixed categories only;
* the real duration of start, ``before_connect``, open, every read and stop is measured by the parent's monotonic clock, including
  containment and cleanup (``timings``); the worker's ``decode_ns`` is a separate diagnostic;
* ``intermediate_launcher`` is a **diagnostic**: it says the process started by the launcher is not the process that wrote the
  shared header (the Windows venv redirector). It is not evidence that containment worked.

A decoder is single use, like the Wave 4 adapter: after any failure or ``close`` it is dead and a new stream needs a new decoder.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from ..device_runtime.errors import InvalidTransition
from ..device_runtime.preview_models import ImageLimits, InvalidImage, PixelFormat, PreviewPixels
from ..seestar_preview import DecoderError
from .process import IsolationUnsupported, StopReport, WorkerConfig, WorkerError, WorkerProcess
from .protocol import MAX_TIMEOUT_MS, MIN_TIMEOUT_MS

__all__ = ["IsolatedDecoderConfig", "OperationTimings", "ProcessIsolatedDecoder", "make_isolated_decoder_factory"]

_FORMATS = {1: PixelFormat.GRAY8, 3: PixelFormat.BGR8}


@dataclass(slots=True, frozen=True)
class IsolatedDecoderConfig:
    """Timeouts are passed to the worker's OpenCV adapter (soft); the parent's hard deadlines add the ``WorkerConfig`` margins (D12)."""

    open_timeout_ms: int = 5000
    read_timeout_ms: int = 3000
    limits: ImageLimits = field(default_factory=ImageLimits)
    worker: WorkerConfig = field(default_factory=WorkerConfig)

    def __post_init__(self) -> None:
        for name in ("open_timeout_ms", "read_timeout_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not MIN_TIMEOUT_MS <= value <= MAX_TIMEOUT_MS:
                raise ValueError(f"{name} must be an integer between {MIN_TIMEOUT_MS} and {MAX_TIMEOUT_MS}.")
        if not isinstance(self.limits, ImageLimits):
            raise ValueError("limits must be ImageLimits.")
        if not isinstance(self.worker, WorkerConfig):
            raise ValueError("worker must be a WorkerConfig.")

    def worker_config(self) -> WorkerConfig:
        """The worker's configuration with the slot and the dimension limits taken from ``limits`` (the single authority)."""
        return replace(self.worker, slot_bytes=self.limits.max_image_bytes, max_width=self.limits.max_width,
                       max_height=self.limits.max_height, decoder_worker=True)


@dataclass(slots=True, frozen=True)
class OperationTimings:
    """Real durations measured by the parent (seconds, monotonic clock). ``None``: that step never ran. Failed steps are included,
    and a step that failed includes the containment and cleanup it triggered."""

    start_s: float | None = None
    before_connect_s: float | None = None
    open_s: float | None = None
    open_total_s: float | None = None
    read_count: int = 0
    read_last_s: float | None = None
    read_max_s: float | None = None
    read_total_s: float = 0.0
    stop_s: float | None = None


class ProcessIsolatedDecoder:
    simulated = False

    def __init__(
        self,
        config: IsolatedDecoderConfig | None = None,
        *,
        camera: str | None = None,
        before_connect: Callable[[str | None], bool] | None = None,
        _worker_factory: Callable[..., WorkerProcess] = WorkerProcess,
        _python_version: Any = None,
    ) -> None:
        self._config = config or IsolatedDecoderConfig()
        self._worker_config = self._config.worker_config()
        self._camera = camera if isinstance(camera, str) else None
        self._before_connect = before_connect
        self._worker_factory = _worker_factory                  # test seam; the public factory never exposes it
        self._python_version = _python_version
        self._lock = threading.Lock()                           # guards the fields below; never held across a worker call
        self._op = threading.Lock()                             # one open/read at a time; taken without waiting
        self._worker: WorkerProcess | None = None
        self._attempted = False
        self._opened = False
        self._closed = False
        self._dead = False
        self._containment_reported = False
        self._intermediate = False
        self._timings = OperationTimings()
        self._last_decode_ns: int | None = None
        self._stop_report: StopReport | None = None

    def __repr__(self) -> str:
        state = "closed" if self._closed else "dead" if self._dead else "open" if self._opened else "new"
        return f"ProcessIsolatedDecoder(camera={self._camera!r}, {state}, <redacted>)"

    __str__ = __repr__

    # --- read-only diagnostics -----------------------------------------------------------------

    @property
    def timings(self) -> OperationTimings:
        with self._lock:
            return self._timings

    @property
    def last_decode_ns(self) -> int | None:
        """The worker's own duration of its last decode. A diagnostic; never a time of the scene, never a replacement for ``timings``."""
        return self._last_decode_ns

    @property
    def intermediate_launcher(self) -> bool:
        """Diagnostic only: the started process is not the one that wrote the shared header. Not a guarantee about containment."""
        return self._intermediate

    @property
    def last_stop_report(self) -> StopReport | None:
        return self._stop_report

    @property
    def uncertain(self) -> tuple:
        report = self._stop_report
        return report.uncertain if report is not None else ()

    # --- internals ---------------------------------------------------------------------------------

    def _note(self, **changes: Any) -> None:
        with self._lock:
            self._timings = replace(self._timings, **changes)

    def _end_worker(self, worker: WorkerProcess) -> None:
        try:
            worker.stop()
        except Exception:
            pass

    # --- ImageDecoder -------------------------------------------------------------------------------

    def open(self, endpoint: Any) -> None:
        if not self._op.acquire(blocking=False):
            raise DecoderError("busy")
        began = time.monotonic()
        started_here = False
        try:
            with self._lock:
                if self._attempted or self._closed:
                    raise DecoderError("invalid_state")
                self._attempted = True
            started_here = True
            address = getattr(endpoint, "address", None)
            camera = getattr(endpoint, "camera", None)
            if isinstance(camera, str):
                self._camera = camera
            if not isinstance(address, str) or not address.startswith("rtsp://"):
                raise DecoderError("invalid_endpoint")
            try:                                                  # the Python gate of WorkerProcess, at the open boundary (E3)
                worker = self._worker_factory(self._worker_config, python_version=self._python_version)
            except IsolationUnsupported:
                raise DecoderError("python_unsupported") from None
            except Exception:
                raise DecoderError("worker_start_failed") from None
            with self._lock:
                self._worker = worker
                closed = self._closed
            if closed:                                            # close() arrived while the worker was being created
                self._end_worker(worker)
                raise DecoderError("not_open")
            step = time.monotonic()
            try:
                worker.start()
            except WorkerError as exc:
                raise DecoderError(exc.category) from None
            finally:
                self._note(start_s=time.monotonic() - step)
            self._intermediate = worker.worker_pid is not None and worker.pid is not None and worker.worker_pid != worker.pid
            if self._before_connect is not None:
                step = time.monotonic()
                try:
                    allowed = bool(self._before_connect(self._camera))
                except Exception:
                    allowed = False                               # fail closed; the exception text is discarded
                finally:
                    self._note(before_connect_s=time.monotonic() - step)
                if not allowed:
                    self._end_worker(worker)
                    raise DecoderError("readiness_revoked")
            step = time.monotonic()
            try:
                worker.open(address, self._config.open_timeout_ms, self._config.read_timeout_ms)
            except WorkerError as exc:
                raise DecoderError(exc.category) from None
            except (ValueError, InvalidTransition):
                self._end_worker(worker)
                raise DecoderError("invalid_endpoint") from None
            finally:
                self._note(open_s=time.monotonic() - step)
                del address                                       # the only copy that was kept is inside the worker's OPEN message
            with self._lock:
                self._opened = True
        except DecoderError:
            if started_here:
                self._dead = True
            raise
        finally:
            if started_here:
                self._note(open_total_s=time.monotonic() - began)
            self._op.release()

    def read(self) -> PreviewPixels:
        if not self._op.acquire(blocking=False):
            raise DecoderError("busy")
        try:
            with self._lock:
                worker = self._worker
                usable = self._opened and not self._closed and not self._dead and worker is not None
            if not usable:
                raise DecoderError("not_open")
            step = time.monotonic()
            try:
                image = worker.read()
            except WorkerError as exc:
                self._dead = True
                raise DecoderError(exc.category) from None
            except InvalidTransition:
                self._dead = True
                raise DecoderError("invalid_state") from None
            finally:
                took = time.monotonic() - step
                with self._lock:
                    t = self._timings
                    self._timings = replace(t, read_count=t.read_count + 1, read_last_s=took,
                                            read_max_s=took if t.read_max_s is None else max(t.read_max_s, took),
                                            read_total_s=t.read_total_s + took)
            self._last_decode_ns = image.decode_ns
            try:
                pixel_format = _FORMATS[image.pixel_format]
                pixels = PreviewPixels(image.width, image.height, pixel_format, image.pixels)
                pixels.check_limits(self._config.limits)          # the parent validates again against the one authoritative object
            except (KeyError, InvalidImage) as exc:
                self._dead = True
                self._end_worker(worker)                          # a worker that delivered an inconsistent image is not trusted
                raise DecoderError(exc.category if isinstance(exc, InvalidImage) else "unsupported_format") from None
            return pixels
        finally:
            self._op.release()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            worker = self._worker
        if worker is None:
            return
        step = time.monotonic()
        try:
            report = worker.stop()
        except Exception:
            raise DecoderError("release_failed") from None
        took = time.monotonic() - step
        with self._lock:
            self._stop_report = report
            if self._timings.stop_s is None:
                self._timings = replace(self._timings, stop_s=took)
            report_it = report.containment_failed and not self._containment_reported
            if report_it:
                self._containment_reported = True
        if report_it:
            raise DecoderError("containment_failed")             # explicit uncertainty: the process was not confirmed gone


def make_isolated_decoder_factory(
    config: IsolatedDecoderConfig | None = None, *, before_connect: Callable[[str | None], bool] | None = None
) -> Callable[[str], ProcessIsolatedDecoder]:
    """``decoder_factory`` for the manager: one new, unstarted decoder per stream. Building one starts nothing."""

    def factory(camera: str) -> ProcessIsolatedDecoder:
        return ProcessIsolatedDecoder(config, camera=camera, before_connect=before_connect)

    return factory
