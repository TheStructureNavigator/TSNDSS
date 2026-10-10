"""Parent-side launcher and lifecycle of one isolated decoder worker (DB-03 Wave 4B-1).

What this module does: start a dedicated worker module with ``subprocess.Popen``, hand it one end of a
``multiprocessing`` ``Pipe`` through handle inheritance, create the shared segment (parent-owned), run the readiness
handshake, probe it with PING, run ``open``/``read`` under parent-enforced hard deadlines, and end the worker with bounded
containment. It also counts live workers (decision D10). What it does not do (4B-3): decode anything or touch OpenCV.

Rules kept here (design sections 2, 4, 6, 10):

* the parent's process-global state is never modified: no edit of ``sys.modules['__main__']``, ``os.environ``,
  ``sys.path`` or signal handlers. The child gets an explicit, minimal environment dictionary of its own;
* nothing but two numbers (pipe handle, protocol version) travels in argv; stdin, stdout and stderr of the child are the
  null device from the moment it is created; the child's working directory is the temp directory and ``-P`` keeps the
  working directory off its ``sys.path``;
* every wait is a bounded ``poll``; a failed or stuck worker is ended by terminate -> bounded wait -> kill -> bounded wait,
  never by an unbounded wait;
* handle ownership (no private CPython API): the child's process handle lives inside the ``Popen`` object. After the
  child has been reaped the only reference to that ``Popen`` is dropped, which closes the handle by reference counting in
  CPython (``subprocess.Handle`` is finalized with its ``Popen``). A child that could not be reaped keeps its ``Popen``
  alive in a module-level registry, because its handle is still needed and the leak is then deliberately visible.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from multiprocessing.connection import Pipe

from ..device_runtime.errors import InvalidTransition
from ..device_runtime.lifecycle import TransitionRecord
from . import protocol as P
from .segment import fence_is_stable, validate_image
from .shm import ParentSegment, SegmentClosed, SegmentUnavailable, crc32_of
from .states import WorkerEvent, WorkerState, worker_next_state

__all__ = [
    "DECODER_ENTRY", "MAX_LIVE_WORKERS", "MIN_PYTHON", "PRODUCTION_ENTRY", "IsolationUnsupported", "StopReport", "WorkerConfig",
    "WorkerError", "WorkerImage", "WorkerProcess", "abandoned_worker_count", "child_environment", "live_worker_count",
    "reclaim_abandoned_workers", "require_supported_python", "stop_all_workers",
]

MIN_PYTHON = (3, 13)
PRODUCTION_ENTRY = "tsn_dss.engine.opencv_isolated_decoder.worker_main"
DECODER_ENTRY = "tsn_dss.engine.opencv_isolated_decoder.decoder_main"      # 4B-3a: the same protocol with a real decoder behind it
_REPO_ROOT = Path(__file__).resolve().parents[3]          # the directory that contains the ``tsn_dss`` package
_ENV_KEYS = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "PATHEXT", "COMSPEC", "LANG", "LC_ALL")
_POLL_SLICE_S = 0.05
_SPAWN_LOCK = threading.Lock()
MAX_LIVE_WORKERS = 2                                       # decision D10: hard ceiling for the whole host process
_ABANDONED: list = []                                      # Popen objects of workers that could not be reaped (they keep their slot)
_ABANDONED_LOCK = threading.Lock()                         # guards _LIVE, _ABANDONED and _ATEXIT_REGISTERED; never held across I/O
_LIVE: dict = {}                                           # id(worker) -> worker, from reservation until the process is reaped
_ATEXIT_REGISTERED = False


class WorkerError(Exception):
    """A failure with a fixed ``category`` (a parent-originated category or a decoded worker status). No data."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class IsolationUnsupported(WorkerError):
    """The isolated decoder cannot be created in this interpreter (Python older than 3.13)."""


def require_supported_python(version_info=None) -> None:
    """Checked when a worker is created, never at import time (the rest of TSN DSS keeps its own minimum version)."""
    version = tuple(version_info if version_info is not None else sys.version_info)[:2]
    if version < MIN_PYTHON:
        raise IsolationUnsupported("python_unsupported")


@dataclass(slots=True, frozen=True)
class WorkerConfig:
    """Deadlines in seconds. Initial design assumptions (decision D12): placeholders until measured on hardware."""

    start_deadline_s: float = 10.0
    ping_deadline_s: float = 2.0
    close_graceful_s: float = 0.5
    terminate_wait_s: float = 1.0
    kill_wait_s: float = 1.0
    open_margin_s: float = 1.5            # hard OPEN deadline = open_timeout_ms + this margin (D12)
    read_margin_s: float = 1.0            # hard READ deadline = read_timeout_ms + this margin (D12)
    slot_bytes: int = 0                   # 0: no shared segment (4B-1 behaviour); otherwise the pixel area size (D4)
    max_width: int = 4096
    max_height: int = 4096
    max_live_workers: int = MAX_LIVE_WORKERS
    decoder_worker: bool = False          # 4B-3a: start the decoder entry point instead of the handshake-only one (needs a slot)

    def __post_init__(self) -> None:
        if not isinstance(self.decoder_worker, bool):
            raise ValueError("decoder_worker must be a bool.")
        if self.decoder_worker and (isinstance(self.slot_bytes, bool) or not isinstance(self.slot_bytes, int) or self.slot_bytes <= 0):
            raise ValueError("a decoder worker needs a shared slot (slot_bytes > 0).")
        for name in ("start_deadline_s", "ping_deadline_s", "close_graceful_s", "terminate_wait_s", "kill_wait_s",
                     "open_margin_s", "read_margin_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 120:
                raise ValueError(f"{name} must be a number in (0, 120].")
        for name, low, high in (("slot_bytes", 0, 256 * 1024 * 1024), ("max_width", 1, 16384), ("max_height", 1, 16384),
                                ("max_live_workers", 1, MAX_LIVE_WORKERS)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise ValueError(f"{name} must be an integer in [{low}, {high}].")


@dataclass(slots=True, frozen=True)
class StopReport:
    steps: tuple
    exit_code: object
    containment_failed: bool
    elapsed_s: float
    uncertain: tuple = ()                 # fixed tokens for outcomes the parent cannot know (see WorkerProcess.stop)
    segment_released: bool = True
    slot_released: bool = True            # False: the process was not confirmed gone, so it still counts against the limit


@dataclass(slots=True, frozen=True)
class WorkerImage:
    """One image copied out of the shared slot into memory owned by the parent. ``decode_ns`` is diagnostic, never a scene time."""

    seq: int
    width: int
    height: int
    pixel_format: int
    pixels: bytes
    decode_ns: int

    def __repr__(self) -> str:
        return f"WorkerImage(seq={self.seq}, {self.width}x{self.height}, format={self.pixel_format}, nbytes={len(self.pixels)})"


def child_environment(extra_pythonpath=()) -> dict:
    """The child's environment: an allowlist of what the OS needs plus ``PYTHONPATH``. Built fresh; ``os.environ`` is only read."""
    env = {key: os.environ[key] for key in _ENV_KEYS if key in os.environ}
    env["PYTHONPATH"] = os.pathsep.join([str(_REPO_ROOT), *map(str, extra_pythonpath)])
    return env


def abandoned_worker_count() -> int:
    """Workers that could not be reaped and whose ``Popen`` is therefore kept alive (a visible leak)."""
    with _ABANDONED_LOCK:
        return len(_ABANDONED)


def _reclaim_locked() -> int:
    """Drop abandoned workers whose exit is now confirmed by ``poll()``; a slot is never freed on any other evidence."""
    freed = 0
    for popen in list(_ABANDONED):
        try:
            gone = popen.poll() is not None
        except Exception:
            gone = False
        if gone:
            _ABANDONED.remove(popen)
            freed += 1
    return freed


def reclaim_abandoned_workers() -> int:
    """Collect workers that were unreapable earlier and have exited since; returns how many slots were recovered."""
    with _ABANDONED_LOCK:
        return _reclaim_locked()


def live_worker_count() -> int:
    """Workers counted against the limit: from reservation until the process is confirmed reaped (abandoned ones included)."""
    with _ABANDONED_LOCK:
        return len(_LIVE) + len(_ABANDONED)


def stop_all_workers() -> int:
    """Best-effort bounded stop of every live worker. Registered with ``atexit``; a crash of the host bypasses it."""
    with _ABANDONED_LOCK:
        workers = list(_LIVE.values())
    for worker in workers:
        try:
            worker.stop()
        except Exception:
            pass
    return len(workers)


def _register_atexit() -> None:
    global _ATEXIT_REGISTERED
    with _ABANDONED_LOCK:
        if _ATEXIT_REGISTERED:
            return
        _ATEXIT_REGISTERED = True
    atexit.register(stop_all_workers)      # registered after ``multiprocessing`` was imported, so it runs before its own exit handler


class WorkerProcess:
    """One worker process. Single use. Safe to ``stop`` from any thread."""

    def __init__(self, config: WorkerConfig | None = None, *, python_version=None, _entry_module: str = PRODUCTION_ENTRY,
                 _extra_args: tuple = (), _extra_pythonpath: tuple = ()) -> None:
        require_supported_python(python_version)
        self._config = config or WorkerConfig()
        # test seam: production code never passes ``_entry_module``; the two production entries are chosen by the config flag
        self._entry = _entry_module if _entry_module != PRODUCTION_ENTRY else (DECODER_ENTRY if self._config.decoder_worker else PRODUCTION_ENTRY)
        self._extra_args = tuple(_extra_args)
        self._extra_pythonpath = tuple(_extra_pythonpath)
        self._lock = threading.RLock()
        self._io_changed = threading.Condition(self._lock)
        self._io_active = 0
        self._stopping = False
        self._terminated = threading.Event()
        self._state = WorkerState.CREATED
        self._transitions: list = []
        self._id = "isolated-worker-" + secrets.token_hex(4)
        self._conn = None
        self._popen = None
        self._next_op = 0
        self._stop_report: StopReport | None = None
        self.failure_category: str | None = None
        self.exit_code = None
        self.pid: int | None = None
        self.worker_pid: int | None = None               # as the worker wrote it into the segment: diagnostic, may differ from ``pid`` (e.g. a Windows venv launcher)
        self.abandoned = False
        self._segment: ParentSegment | None = None
        self._reserved = False
        self._decoder_open = False
        self._read_timeout_s = 0.0
        self._last_seq = 0
        self._nonce = 0

    def __repr__(self) -> str:
        return f"WorkerProcess({self._id}, {self._state.value})"

    # --- read-only view -----------------------------------------------------------------------

    @property
    def state(self) -> WorkerState:
        return self._state

    @property
    def transitions(self) -> tuple:
        with self._lock:
            return tuple(self._transitions)

    @property
    def decoder_open(self) -> bool:
        """True only after a correct RESULT for OPEN. A READY worker says nothing about this."""
        return self._decoder_open

    @property
    def popen_released(self) -> bool:
        """True once the ``Popen`` (and with it the process handle) is no longer held by this object."""
        return self._popen is None

    # --- state ----------------------------------------------------------------------------------

    def _apply(self, event: WorkerEvent, evidence: str = "") -> None:
        with self._lock:
            new = worker_next_state(self._state, event)
            if new is not self._state:
                self._transitions.append(
                    TransitionRecord(self._id, self._state.value, event.value, new.value, datetime.now(timezone.utc), evidence)
                )
            self._state = new

    def _new_op(self) -> int:
        with self._lock:
            self._next_op += 1
            return self._next_op

    @contextlib.contextmanager
    def _io(self):
        with self._lock:
            if self._stopping:
                raise WorkerError("worker_ipc_lost")
            self._io_active += 1
        try:
            yield
        finally:
            with self._lock:
                self._io_active -= 1
                self._io_changed.notify_all()
                finalize = self._stopping and self._io_active == 0
            if finalize:
                self._close_channel()

    def _close_channel(self) -> None:
        """Close the pipe and the parent's mapping (unlinking the name first). Only called when no operation uses them."""
        with self._lock:
            conn, self._conn = self._conn, None
            segment, self._segment = self._segment, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if segment is not None:
            segment.close()

    # --- launching --------------------------------------------------------------------------------

    def _spawn(self):
        with _SPAWN_LOCK:                                  # handle-inheritance flags must not leak into a concurrent spawn
            parent_end, child_end = Pipe(duplex=True)
            try:
                handle = child_end.fileno()
                argv = [sys.executable, "-P", "-m", self._entry, "--ctl", str(handle), "--proto", str(P.PROTOCOL_VERSION),
                        *self._extra_args]
                kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              env=child_environment(self._extra_pythonpath), cwd=tempfile.gettempdir(), close_fds=True)
                if sys.platform == "win32":
                    os.set_handle_inheritable(handle, True)
                    kwargs["startupinfo"] = subprocess.STARTUPINFO(lpAttributeList={"handle_list": [handle]})
                else:
                    kwargs["pass_fds"] = (handle,)
                popen = subprocess.Popen(argv, **kwargs)
            except BaseException:
                parent_end.close()
                raise
            finally:
                child_end.close()                          # the parent must drop its copy of the child's end (EOF visibility)
        return parent_end, popen

    # --- channel ----------------------------------------------------------------------------------

    def _channel_lost(self) -> WorkerError:
        popen = self._popen
        if popen is not None:
            try:
                popen.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
            if popen.poll() is not None:
                return WorkerError("worker_crashed")
        return WorkerError("worker_ipc_lost")

    def _send(self, message) -> None:
        conn = self._conn
        if conn is None:
            raise WorkerError("worker_ipc_lost")
        try:
            conn.send_bytes(P.encode(message))
        except (OSError, EOFError, ValueError):
            raise self._channel_lost() from None

    def _recv(self, deadline: float, timeout_category: str) -> bytes:
        while True:
            if self._stopping:
                raise WorkerError("worker_ipc_lost")
            conn = self._conn
            if conn is None:
                raise WorkerError("worker_ipc_lost")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkerError(timeout_category)
            try:
                ready = conn.poll(min(remaining, _POLL_SLICE_S))
                if ready:
                    return conn.recv_bytes(P.MAX_MESSAGE_BYTES)
            except (EOFError, ValueError):
                raise self._channel_lost() from None
            except OSError as exc:
                if "bad message length" in str(exc):
                    raise WorkerError("worker_protocol_error") from None
                raise self._channel_lost() from None

    def _expect(self, kind, deadline: float, timeout_category: str):
        raw = self._recv(deadline, timeout_category)
        try:
            message = P.decode(raw)
        except P.ProtocolError:
            raise WorkerError("worker_protocol_error") from None
        if not isinstance(message, kind):
            raise WorkerError("worker_protocol_error")
        return message

    # --- lifecycle --------------------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the worker and complete the readiness handshake. Raises ``WorkerError``; on failure the worker is already stopped."""
        self._apply(WorkerEvent.START_REQUESTED)
        deadline = time.monotonic() + self._config.start_deadline_s
        try:
            self._reserve_slot()
            with self._io():
                self._create_segment()
                try:
                    conn, popen = self._spawn()
                except Exception:
                    raise WorkerError("worker_start_failed") from None
                with self._lock:
                    self._conn, self._popen, self.pid = conn, popen, popen.pid
                self._nonce = secrets.randbits(64)
                segment, cfg = self._segment, self._config
                self._send(P.Init(P.PROTOCOL_VERSION, P.STATUS_TABLE_VERSION, self._nonce,
                                  segment.name if segment else "", cfg.slot_bytes, cfg.max_width, cfg.max_height))
                hello = self._expect(P.Hello, deadline, "worker_handshake_timeout")
                if hello.proto_version != P.PROTOCOL_VERSION or hello.status_table_version != P.STATUS_TABLE_VERSION:
                    raise WorkerError("worker_protocol_error")
                category = self._status_category(hello.status)
                if category is not None:
                    raise WorkerError(category)
                if hello.nonce != self._nonce:
                    raise WorkerError("worker_protocol_error")
                self._verify_segment()
                self._probe(deadline, "worker_handshake_timeout")
                if segment is not None:
                    segment.unlink_name()                  # POSIX: from here on no name can leak; Windows: no-op
            self._apply(WorkerEvent.HANDSHAKE_COMPLETE, "ping_ok")
        except WorkerError as exc:
            self._fail(exc.category)
            raise
        except BaseException:
            self._fail("worker_start_failed")
            raise

    def _reserve_slot(self) -> None:
        """Count this worker against the process-wide limit before any resource is created (decision D10)."""
        limit = min(self._config.max_live_workers, MAX_LIVE_WORKERS)
        with _ABANDONED_LOCK:
            _reclaim_locked()
            if len(_LIVE) + len(_ABANDONED) >= limit:
                raise WorkerError("worker_limit")
            _LIVE[id(self)] = self
            self._reserved = True
        _register_atexit()

    def _create_segment(self) -> None:
        if self._config.slot_bytes <= 0:
            return
        try:
            segment = ParentSegment(self._config.slot_bytes)
        except (SegmentUnavailable, P.ProtocolError):
            raise WorkerError("shm_unavailable") from None
        with self._lock:
            self._segment = segment

    def _verify_segment(self) -> None:
        """The nonce the worker wrote into the shared header must equal the one it echoed on the pipe (proves a shared mapping)."""
        segment = self._segment
        if segment is None:
            return
        try:
            header = segment.read_header()
        except (P.ProtocolError, SegmentClosed):
            raise WorkerError("worker_protocol_error") from None
        if header.handshake_nonce != self._nonce or header.begin_seq or header.end_seq:
            raise WorkerError("worker_protocol_error")
        self.worker_pid = header.worker_pid

    @staticmethod
    def _status_category(code: int):
        try:
            return P.category_for_status(code)
        except P.UnknownStatus:
            raise WorkerError("worker_protocol_error") from None

    def _probe(self, deadline: float, timeout_category: str) -> None:
        op = self._new_op()
        self._send(P.Ping(op))
        pong = self._expect(P.Pong, deadline, timeout_category)
        if pong.op_id != op:
            raise WorkerError("worker_protocol_error")

    def ping(self) -> None:
        """Round-trip liveness probe on a READY worker. Failure stops the worker and raises ``WorkerError``."""
        with self._lock:
            if self._state is not WorkerState.READY:
                raise InvalidTransition("isolated worker", self._state.value, "ping")
        try:
            with self._io():
                self._probe(time.monotonic() + self._config.ping_deadline_s, "worker_ipc_lost")
        except WorkerError as exc:
            self._fail(exc.category)
            raise

    # --- operations (4B-2: exercised with fixture workers only; the production worker has no decoder) ---------

    def _begin_operation(self, event: WorkerEvent) -> int:
        with self._lock:
            if self._stopping or self._state in (WorkerState.FAILED, WorkerState.STOPPING, WorkerState.TERMINATED):
                raise WorkerError("worker_ipc_lost")
            if self._state is not WorkerState.READY:       # STARTING, CREATED, or an operation already in flight
                raise InvalidTransition("isolated worker", self._state.value, event.value)
            if (event is WorkerEvent.OPEN_REQUESTED) == self._decoder_open:
                raise InvalidTransition("isolated worker", "open" if self._decoder_open else "not_open", event.value)
            if event is WorkerEvent.READ_REQUESTED and self._config.slot_bytes <= 0:
                raise InvalidTransition("isolated worker", "no_shared_slot", event.value)
            self._apply(event)
            return self._new_op()

    def _fail_unexpected(self) -> None:
        self._fail("worker_ipc_lost")

    def open(self, address: str, open_timeout_ms: int, read_timeout_ms: int) -> None:
        """Ask the worker to open a stream. Hard deadline: ``open_timeout_ms`` + ``open_margin_s``. READY is not proof of this call."""
        try:
            P.Open(1, open_timeout_ms, read_timeout_ms, address)
        except P.ProtocolError:
            raise ValueError("invalid open arguments") from None
        op = self._begin_operation(WorkerEvent.OPEN_REQUESTED)
        try:
            with self._io():
                self._read_timeout_s = read_timeout_ms / 1000
                self._send(P.Open(op, open_timeout_ms, read_timeout_ms, address))
                deadline = time.monotonic() + open_timeout_ms / 1000 + self._config.open_margin_s
                result = self._expect(P.Result, deadline, "open_deadline_exceeded")
                if result.op_id != op:
                    raise WorkerError("worker_protocol_error")
                category = self._status_category(result.status)
                if category is not None:
                    raise WorkerError(category)
            with self._lock:
                if self._stopping:
                    raise WorkerError("worker_ipc_lost")
                self._decoder_open = True
                self._apply(WorkerEvent.OPEN_COMPLETE, "open_ok")
        except WorkerError as exc:
            self._fail(exc.category)
            raise
        except BaseException:
            self._fail_unexpected()
            raise

    def read(self) -> WorkerImage:
        """Ask for one image. Hard deadline: ``read_timeout_ms`` + ``read_margin_s``. The image is copied into memory owned by the parent."""
        op = self._begin_operation(WorkerEvent.READ_REQUESTED)
        try:
            with self._io():
                self._send(P.Read(op))
                deadline = time.monotonic() + self._read_timeout_s + self._config.read_margin_s
                message = self._expect((P.ImageReady, P.Result), deadline, "read_deadline_exceeded")
                if message.op_id != op:
                    raise WorkerError("worker_protocol_error")
                if isinstance(message, P.Result):
                    category = self._status_category(message.status)
                    raise WorkerError(category or "worker_protocol_error")     # a READ answered with OK and no image is a violation
                image = self._take_image(message)
            with self._lock:
                if self._stopping:
                    raise WorkerError("worker_ipc_lost")
                self._last_seq = image.seq
                self._apply(WorkerEvent.READ_COMPLETE, "image_ok")
            return image
        except WorkerError as exc:
            self._fail(exc.category)
            raise
        except BaseException:
            self._fail_unexpected()
            raise

    def _take_image(self, message: P.ImageReady) -> WorkerImage:
        """Design 7.4: validate against the header, copy to parent memory, re-read the header, check the CRC on the copy."""
        segment, cfg = self._segment, self._config
        if segment is None:
            raise WorkerError("worker_ipc_lost")
        if message.seq != self._last_seq + 1:
            raise WorkerError("worker_protocol_error")
        try:
            first = segment.read_header()
            validate_image(message, first, max_width=cfg.max_width, max_height=cfg.max_height, max_image_bytes=cfg.slot_bytes)
            if first.handshake_nonce != self._nonce or first.worker_pid != self.worker_pid:
                raise P.ProtocolError("header_identity")
            pixels = segment.copy_pixels(message.nbytes)
            second = segment.read_header()
        except P.ProtocolError:
            raise WorkerError("worker_protocol_error") from None
        except SegmentClosed:
            raise WorkerError("worker_ipc_lost") from None
        if second != first or not fence_is_stable(second, message.seq):     # the worker wrote while the parent owned the slot
            raise WorkerError("worker_protocol_error")
        if crc32_of(pixels) != first.crc32:                                 # checked on the parent's own copy
            raise WorkerError("worker_protocol_error")
        return WorkerImage(message.seq, message.width, message.height, message.pixel_format, pixels, message.decode_ns)

    def _fail(self, category: str) -> None:
        with self._lock:
            if self.failure_category is None:
                self.failure_category = category
            try:
                self._apply(WorkerEvent.FAILURE, category)
            except InvalidTransition:
                pass
        self.stop()

    def stop(self) -> StopReport:
        """Bounded, idempotent containment. Callable from any thread; later callers wait for the first to finish."""
        cfg = self._config
        with self._lock:
            if self._state is WorkerState.TERMINATED:
                return self._stop_report
            first = not self._stopping
            self._stopping = True
            was_ready = self._state is WorkerState.READY and self._io_active == 0
            in_flight = self._state in (WorkerState.OPENING, WorkerState.READING)
            had_stream = self._decoder_open
            self._apply(WorkerEvent.STOP_REQUESTED)
        if not first:
            self._terminated.wait(cfg.close_graceful_s + cfg.terminate_wait_s + cfg.kill_wait_s + 3.0)
            return self._stop_report
        began = time.monotonic()
        steps: list = []
        popen, conn = self._popen, self._conn
        containment_failed = False
        if popen is not None and conn is not None and was_ready and popen.poll() is None:
            try:                                           # graceful: best effort, never required
                op = self._new_op()
                conn.send_bytes(P.encode(P.Close(op)))
                steps.append("close")
                end = time.monotonic() + cfg.close_graceful_s
                answered = False
                while time.monotonic() < end and not answered:
                    if conn.poll(min(_POLL_SLICE_S, max(0.0, end - time.monotonic()))):
                        message = P.decode(conn.recv_bytes(P.MAX_MESSAGE_BYTES))
                        answered = isinstance(message, P.Result) and message.op_id == op
                if answered:
                    popen.wait(timeout=cfg.close_graceful_s)
            except Exception:
                pass
        if popen is not None:
            if popen.poll() is None:
                steps.append("terminate")
                try:
                    popen.terminate()
                    popen.wait(timeout=cfg.terminate_wait_s)
                except (subprocess.TimeoutExpired, OSError):
                    pass
            if popen.poll() is None:
                steps.append("kill")
                try:
                    popen.kill()
                    popen.wait(timeout=cfg.kill_wait_s)
                except (subprocess.TimeoutExpired, OSError):
                    pass
            if popen.poll() is None:
                containment_failed = True
            else:
                self.exit_code = popen.returncode
        uncertain: list = []
        if (had_stream or in_flight) and ("terminate" in steps or "kill" in steps):
            uncertain.append("operation_interrupted" if in_flight else "stream_terminated")   # effect on the device is unknown (U5)
        if self.pid is not None and self.worker_pid is not None and self.pid != self.worker_pid:
            uncertain.append("intermediate_launcher")      # diagnostic only (e.g. a Windows venv redirector); NOT proof of containment
        if containment_failed:
            uncertain.append("process_unreaped")
        popen = None
        with self._lock:                                   # let an operation thread leave the channel before it is closed
            end = time.monotonic() + 1.0
            while self._io_active and time.monotonic() < end:
                self._io_changed.wait(0.05)
            idle = self._io_active == 0
        if idle:
            self._close_channel()
        with self._lock:
            if containment_failed:
                self.abandoned = True
                if self.failure_category is None:
                    self.failure_category = "containment_failed"
            gone = self._popen
            self._popen = None                             # ownership ends: the Popen (and its process handle) is released
            self._decoder_open = False
            self._apply(WorkerEvent.STOPPED)
            slot_released = self._settle_slot(gone if containment_failed else None)
            segment_released = self._segment is None
            if not segment_released:
                uncertain.append("segment_not_released")
            self._stop_report = StopReport(tuple(steps), self.exit_code, containment_failed, time.monotonic() - began,
                                           tuple(uncertain), segment_released, slot_released)
        self._terminated.set()
        return self._stop_report

    def _settle_slot(self, unreaped) -> bool:
        """End of life: free the slot only when the process is confirmed gone (or never existed); otherwise the Popen moves
        to the abandoned list and keeps counting. A timeout, ``kill()`` or a lost pipe is never evidence of exit."""
        with _ABANDONED_LOCK:
            if not self._reserved:
                return True
            _LIVE.pop(id(self), None)
            self._reserved = False
            if unreaped is not None:
                _ABANDONED.append(unreaped)                # the handle is still needed; the leak stays visible and counted
                return False
            return True

    def __enter__(self) -> "WorkerProcess":
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()
