"""Process isolation for the OpenCV preview decoder (DB-03 Wave 4B-1: protocol, state machine, launcher).

Importable on any Python version: the 3.13 requirement is enforced when a ``WorkerProcess`` is created, not here.
Importing this package imports neither OpenCV nor NumPy and starts nothing. 4B-1 contains no decoder: it provides the
versioned binary protocol, the segment-header codec, the worker state machine, the dedicated worker entry point and the
launcher with bounded containment. Nothing here is connected to the preview manager yet.
"""

from .process import (
    PRODUCTION_ENTRY,
    IsolationUnsupported,
    StopReport,
    WorkerConfig,
    WorkerError,
    WorkerProcess,
    abandoned_worker_count,
    require_supported_python,
)
from .protocol import PROTOCOL_VERSION, STATUS_TABLE_VERSION, ProtocolError, Status, UnknownStatus
from .states import WorkerEvent, WorkerState

__all__ = [
    "PRODUCTION_ENTRY", "PROTOCOL_VERSION", "STATUS_TABLE_VERSION", "IsolationUnsupported", "ProtocolError", "Status",
    "StopReport", "UnknownStatus", "WorkerConfig", "WorkerError", "WorkerEvent", "WorkerProcess", "WorkerState",
    "abandoned_worker_count", "require_supported_python",
]
