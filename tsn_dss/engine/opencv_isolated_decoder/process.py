"""Parent-side launcher and lifecycle of one isolated decoder worker (DB-03 Wave 4B-1).

What this module does: start a dedicated worker module with ``subprocess.Popen``, hand it one end of a
``multiprocessing`` ``Pipe`` through handle inheritance, run the readiness handshake, probe it with PING, and end it
with bounded containment. What it does not do (4B-2/4B-3): open a stream, read frames, attach shared memory, or touch
OpenCV.

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
from .states import WorkerEvent, WorkerState, worker_next_state

__all__ = [
    "MIN_PYTHON", "PRODUCTION_ENTRY", "IsolationUnsupported", "StopReport", "WorkerConfig", "WorkerError",
    "WorkerProcess", "abandoned_worker_count", "child_environment", "require_supported_python",
]

MIN_PYTHON = (3, 13)
PRODUCTION_ENTRY = "tsn_dss.engine.opencv_isolated_decoder.worker_main"
_REPO_ROOT = Path(__file__).resolve().parents[3]          # the directory that contains the ``tsn_dss`` package
_ENV_KEYS = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "PATHEXT", "COMSPEC", "LANG", "LC_ALL")
_POLL_SLICE_S = 0.05
_SPAWN_LOCK = threading.Lock()
_ABANDONED: list = []
_ABANDONED_LOCK = threading.Lock()


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

    def __post_init__(self) -> None:
        for name in ("start_deadline_s", "ping_deadline_s", "close_graceful_s", "terminate_wait_s", "kill_wait_s"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 120:
                raise ValueError(f"{name} must be a number in (0, 120].")


@dataclass(slots=True, frozen=True)
class StopReport:
    steps: tuple
    exit_code: object
    containment_failed: bool
    elapsed_s: float


def child_environment(extra_pythonpath=()) -> dict:
    """The child's environment: an allowlist of what the OS needs plus ``PYTHONPATH``. Built fresh; ``os.environ`` is only read."""
    env = {key: os.environ[key] for key in _ENV_KEYS if key in os.environ}
    env["PYTHONPATH"] = os.pathsep.join([str(_REPO_ROOT), *map(str, extra_pythonpath)])
    return env


def abandoned_worker_count() -> int:
    """Workers that could not be reaped and whose ``Popen`` is therefore kept alive (a visible leak)."""
    with _ABANDONED_LOCK:
        return len(_ABANDONED)


class WorkerProcess:
    """One worker process. Single use. Safe to ``stop`` from any thread."""

    def __init__(self, config: WorkerConfig | None = None, *, python_version=None, _entry_module: str = PRODUCTION_ENTRY,
                 _extra_args: tuple = (), _extra_pythonpath: tuple = ()) -> None:
        require_supported_python(python_version)
        self._config = config or WorkerConfig()
        self._entry = _entry_module                      # test seam: production code always uses PRODUCTION_ENTRY
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
        self.abandoned = False

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
        return False                                      # no stream can be opened before 4B-3

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
                self._close_connection()

    def _close_connection(self) -> None:
        with self._lock:
            conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

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
            with self._io():
                try:
                    conn, popen = self._spawn()
                except Exception:
                    raise WorkerError("worker_start_failed") from None
                with self._lock:
                    self._conn, self._popen, self.pid = conn, popen, popen.pid
                nonce = secrets.randbits(64)
                self._send(P.Init(P.PROTOCOL_VERSION, P.STATUS_TABLE_VERSION, nonce, "", 0, 1, 1))
                hello = self._expect(P.Hello, deadline, "worker_handshake_timeout")
                if hello.proto_version != P.PROTOCOL_VERSION or hello.status_table_version != P.STATUS_TABLE_VERSION:
                    raise WorkerError("worker_protocol_error")
                try:
                    category = P.category_for_status(hello.status)
                except P.UnknownStatus:
                    raise WorkerError("worker_protocol_error") from None
                if category is not None:
                    raise WorkerError(category)
                if hello.nonce != nonce:
                    raise WorkerError("worker_protocol_error")
                self._probe(deadline, "worker_handshake_timeout")
            self._apply(WorkerEvent.HANDSHAKE_COMPLETE, "ping_ok")
        except WorkerError as exc:
            self._fail(exc.category)
            raise
        except BaseException:
            self._fail("worker_start_failed")
            raise

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
        popen = None
        with self._lock:                                   # let an operation thread leave the channel before it is closed
            end = time.monotonic() + 1.0
            while self._io_active and time.monotonic() < end:
                self._io_changed.wait(0.05)
            idle = self._io_active == 0
        if idle:
            self._close_connection()
        with self._lock:
            if containment_failed:
                self.abandoned = True
                if self.failure_category is None:
                    self.failure_category = "containment_failed"
                with _ABANDONED_LOCK:
                    _ABANDONED.append(self._popen)         # the handle is still needed; the leak stays visible
            self._popen = None                             # ownership ends: the Popen (and its process handle) is released
            self._apply(WorkerEvent.STOPPED)
            self._stop_report = StopReport(tuple(steps), self.exit_code, containment_failed, time.monotonic() - began)
        self._terminated.set()
        return self._stop_report

    def __enter__(self) -> "WorkerProcess":
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()
