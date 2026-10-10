"""Process isolation for the OpenCV preview decoder (DB-03 Wave 4B-1: protocol, state machine, launcher; 4B-2: shared memory, deadlines, process limit).

Importable on any Python version: the 3.13 requirement is enforced when a ``WorkerProcess`` is created, not here.
Importing this package imports neither OpenCV nor NumPy and starts nothing. There is no decoder yet: the package provides the
versioned binary protocol, the segment codec and the parent-owned shared segment, the worker state machine, the dedicated
worker entry point, the launcher with hard deadlines and bounded containment, and the process-wide worker limit. Nothing here
is connected to the preview manager yet.
"""

from .process import (
    DECODER_ENTRY,
    MAX_LIVE_WORKERS,
    PRODUCTION_ENTRY,
    IsolationUnsupported,
    StopReport,
    WorkerConfig,
    WorkerError,
    WorkerImage,
    WorkerProcess,
    abandoned_worker_count,
    live_worker_count,
    reclaim_abandoned_workers,
    require_supported_python,
    stop_all_workers,
)
from .protocol import PROTOCOL_VERSION, STATUS_TABLE_VERSION, ProtocolError, Status, UnknownStatus
from .states import WorkerEvent, WorkerState

__all__ = [
    "DECODER_ENTRY", "MAX_LIVE_WORKERS", "PRODUCTION_ENTRY", "PROTOCOL_VERSION", "STATUS_TABLE_VERSION", "IsolationUnsupported", "ProtocolError", "Status",
    "StopReport", "UnknownStatus", "WorkerConfig", "WorkerError", "WorkerEvent", "WorkerImage", "WorkerProcess",
    "WorkerState", "abandoned_worker_count", "live_worker_count", "reclaim_abandoned_workers", "require_supported_python",
    "stop_all_workers",
]
