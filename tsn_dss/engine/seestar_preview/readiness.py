"""Readiness evidence for the neutral gate, built from DB-02 runtime telemetry.

Only the public DB-02 runtime surface is used: ``ProviderRuntime.read_telemetry``
returns a sample whose ``app.<camera>.*`` items carry the device's own report of
each camera's RTSP state. This module maps those items, per camera, to a
``ReadinessEvidence``:

* AVAILABLE only when every one of the five items for that camera is KNOWN and
  matches the DB-02 rule (mode ``scenery``, stage ``RTSP``, state ``working``,
  RTSP state ``working``, the camera's own port);
* UNAVAILABLE when all five are KNOWN but do not match (device reports the
  camera is not serving);
* UNKNOWN when any item is absent, unknown or stale. This is stricter than
  ``normalize.preview_availability`` for partial replies: it can only withhold
  an AVAILABLE verdict, never grant one (see the equivalence tests).

Evidence is bound to the Connection id, provider id and device reference of the
Connection it was read for. The observation time is the host time of the
sample. No device timestamp is read or invented. ``Connection`` readiness is
not an input. Any failure to read yields no evidence.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..device_runtime import (
    Connection,
    DeviceRuntimeError,
    PreviewAvailability,
    TelemetrySample,
    TelemetrySource,
    ValueState,
)
from ..device_runtime.preview_readiness import ReadinessEvidence
from ..seestar_provider.normalize import PREVIEW_PORTS
from .config import CAMERAS

APP_FIELDS = ("mode", "stage", "state", "rtsp_state", "rtsp_port")

__all__ = ["APP_FIELDS", "SeestarReadinessEvidenceProvider", "TelemetryReader", "camera_availability"]


@runtime_checkable
class TelemetryReader(Protocol):
    """The part of ``ProviderRuntime`` this integration uses. Read-only."""

    def read_telemetry(self, connection: Connection) -> TelemetrySample: ...


def camera_availability(sample: TelemetrySample, camera: str) -> PreviewAvailability:
    """Availability of one camera from one telemetry sample. Cameras never share evidence."""
    if camera not in PREVIEW_PORTS:
        return PreviewAvailability.UNKNOWN
    items = {name: sample.get(f"app.{camera}.{name}", TelemetrySource.PROVIDER_REPORTED) for name in APP_FIELDS}
    if any(item is None or item.state is not ValueState.KNOWN for item in items.values()):
        return PreviewAvailability.UNKNOWN
    ready = (
        items["mode"].value == "scenery"
        and items["stage"].value == "RTSP"
        and items["state"].value == "working"
        and items["rtsp_state"].value == "working"
        and items["rtsp_port"].value == PREVIEW_PORTS[camera]
    )
    return PreviewAvailability.AVAILABLE if ready else PreviewAvailability.UNAVAILABLE


class SeestarReadinessEvidenceProvider:
    """``ReadinessEvidenceProvider`` for one Connection. Every call reads anew; nothing is cached."""

    def __init__(self, reader: TelemetryReader, connection: Connection, *, cameras: tuple[str, ...] = CAMERAS) -> None:
        self._reader = reader
        self._connection = connection
        self._cameras = tuple(cameras)
        self.last_error_category: str | None = None

    def __repr__(self) -> str:
        return f"SeestarReadinessEvidenceProvider({self._connection.connection_id!r})"

    def read_evidence(self, source_label: str) -> ReadinessEvidence | None:
        self.last_error_category = None
        if source_label not in self._cameras:
            self.last_error_category = "camera_not_configured"
            return None
        try:
            sample = self._reader.read_telemetry(self._connection)
        except DeviceRuntimeError:
            self.last_error_category = "telemetry_unavailable"
            return None
        if (
            sample.connection_id != self._connection.connection_id
            or sample.provider_id != self._connection.provider_id
        ):
            self.last_error_category = "identity_mismatch"
            return None
        return ReadinessEvidence(
            provider_id=self._connection.provider_id,
            connection_id=self._connection.connection_id,
            device_ref=self._connection.device.device_ref,
            source_label=source_label,
            availability=camera_availability(sample, source_label),
            host_observed_at=sample.host_observed_at,
            simulated=sample.simulated,
        )
