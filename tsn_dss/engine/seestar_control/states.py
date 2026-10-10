"""Seestar state vocabulary and pure predicates over runtime telemetry (DB-05 Slice 1).

Everything here is vendor-specific and therefore lives outside ``device_runtime``. Item names are those produced by the
DB-02 provider (``seestar_provider.normalize``). The camera readiness predicate is the existing DB-03 one
(``seestar_preview.readiness.camera_availability``); the stopped words are the existing DB-02 constant. Nothing here
reads the device: predicates take a ``TelemetrySample``.
"""

from __future__ import annotations

from ..device_runtime import PreviewAvailability, TelemetrySample, TelemetrySource, ValueState
from ..seestar_preview.readiness import camera_availability
from ..seestar_provider.normalize import STOPPED_STATES

CAMERAS = ("main", "wide")
MOVE_TYPE = "mount.move_type"
ARM_CLOSED = "mount.arm_closed"
CAMERA_ITEMS = tuple(f"app.{camera}.{field}" for camera in CAMERAS for field in ("state", "rtsp_state"))
STATIONARY = ("none",)

__all__ = [
    "ARM_CLOSED", "CAMERAS", "CAMERA_ITEMS", "MOVE_TYPE", "STATIONARY", "STOPPED_STATES",
    "arm_closed", "arm_stationary", "cameras_ready", "cameras_stopped_count", "known_value",
]


def known_value(sample: TelemetrySample, name: str):
    """The provider-reported value of ``name`` if it is KNOWN, else ``None`` (never a guess)."""
    item = sample.get(name, TelemetrySource.PROVIDER_REPORTED)
    return item.value if item is not None and item.state is ValueState.KNOWN else None


def arm_stationary(sample: TelemetrySample) -> bool | None:
    """True/False if the mount's movement is known, ``None`` if it is not."""
    move = known_value(sample, MOVE_TYPE)
    return None if not isinstance(move, str) else move in STATIONARY


def arm_closed(sample: TelemetrySample) -> bool | None:
    value = known_value(sample, ARM_CLOSED)
    return value if isinstance(value, bool) else None


def cameras_ready(sample: TelemetrySample) -> bool:
    return all(camera_availability(sample, camera) is PreviewAvailability.AVAILABLE for camera in CAMERAS)


def cameras_stopped_count(sample: TelemetrySample) -> int | None:
    """How many of the four camera items are in a stopped word; ``None`` if any is not KNOWN."""
    values = [known_value(sample, name) for name in CAMERA_ITEMS]
    if any(not isinstance(v, str) for v in values):
        return None
    return sum(1 for v in values if v in STOPPED_STATES)
