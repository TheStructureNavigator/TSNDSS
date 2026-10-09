"""State machine of an isolated decoder worker (DB-03 Wave 4B-1, design section 5).

States: CREATED, STARTING, READY, OPENING, READING, FAILED, STOPPING, TERMINATED. READY means "process and IPC
verified"; whether a decoder stream is open is a separate flag, not a state. FAILED is never a resting state: it always
proceeds to STOPPING. TERMINATED is terminal and never reused. The transition table is explicit; anything absent is
invalid and raises ``InvalidTransition`` (the Wave 1 pattern).
"""

from __future__ import annotations

from enum import Enum

from ..device_runtime.errors import InvalidTransition

__all__ = ["WORKER_TRANSITIONS", "WorkerEvent", "WorkerState", "worker_next_state"]


class WorkerState(Enum):
    CREATED = "created"
    STARTING = "starting"
    READY = "ready"
    OPENING = "opening"
    READING = "reading"
    FAILED = "failed"
    STOPPING = "stopping"
    TERMINATED = "terminated"


class WorkerEvent(Enum):
    START_REQUESTED = "start_requested"
    HANDSHAKE_COMPLETE = "handshake_complete"
    OPEN_REQUESTED = "open_requested"
    OPEN_COMPLETE = "open_complete"
    READ_REQUESTED = "read_requested"
    READ_COMPLETE = "read_complete"
    FAILURE = "failure"
    STOP_REQUESTED = "stop_requested"
    STOPPED = "stopped"


S = WorkerState
V = WorkerEvent

WORKER_TRANSITIONS: dict[tuple[WorkerState, WorkerEvent], WorkerState] = {
    (S.CREATED, V.START_REQUESTED): S.STARTING,
    (S.CREATED, V.STOP_REQUESTED): S.STOPPING,
    (S.STARTING, V.HANDSHAKE_COMPLETE): S.READY,
    (S.STARTING, V.FAILURE): S.FAILED,
    (S.STARTING, V.STOP_REQUESTED): S.STOPPING,
    (S.READY, V.OPEN_REQUESTED): S.OPENING,
    (S.READY, V.READ_REQUESTED): S.READING,
    (S.READY, V.FAILURE): S.FAILED,
    (S.READY, V.STOP_REQUESTED): S.STOPPING,
    (S.OPENING, V.OPEN_COMPLETE): S.READY,
    (S.OPENING, V.FAILURE): S.FAILED,
    (S.OPENING, V.STOP_REQUESTED): S.STOPPING,
    (S.READING, V.READ_COMPLETE): S.READY,
    (S.READING, V.FAILURE): S.FAILED,
    (S.READING, V.STOP_REQUESTED): S.STOPPING,
    (S.FAILED, V.STOP_REQUESTED): S.STOPPING,
    (S.STOPPING, V.STOP_REQUESTED): S.STOPPING,      # idempotent stop
    (S.STOPPING, V.FAILURE): S.STOPPING,             # a secondary failure while stopping is recorded, not a new state
    (S.STOPPING, V.STOPPED): S.TERMINATED,
    (S.TERMINATED, V.STOP_REQUESTED): S.TERMINATED,  # idempotent stop
}


def worker_next_state(state: WorkerState, event: WorkerEvent) -> WorkerState:
    try:
        return WORKER_TRANSITIONS[(state, event)]
    except KeyError:
        raise InvalidTransition("isolated worker", state.value, event.value) from None
