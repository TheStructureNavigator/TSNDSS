"""Deterministic simulator Provider (DSS-CTR-013 section 12).

Everything it produces is marked ``simulated``. It performs no I/O, has no
Command surface in DB-01, and behaves identically for identical scripted input
and injected clock (REQ-030, REQ-031, REQ-063).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Sequence

from .errors import ProviderConnectionError, ProviderDiscoveryError
from .models import (
    CapabilityConfirmation,
    CapabilityEntry,
    ConfigurationStatus,
    ConnectionId,
    DeviceReference,
    EvidenceFreshness,
    PreviewAvailability,
    ProviderDescriptor,
    ProviderId,
    TelemetryItem,
    TelemetrySource,
    ValueState,
)
from .provider import ProviderPreviewReading, ProviderTelemetryReading
from .support import Clock, utc_now

DEFAULT_SIMULATOR_PROVIDER_ID = ProviderId("sim-provider")


@dataclass(slots=True, frozen=True)
class SimulatedDeviceSpec:
    device_ref: str
    label: str | None = None
    model: str | None = None
    firmware_version: str | None = None
    endpoint_hint: str | None = None


DEFAULT_DEVICES = (
    SimulatedDeviceSpec("sim-device-1", "Simulated Telescope", "SIM-1", "0.0.0-sim"),
)


def _default_telemetry() -> tuple[TelemetryItem, ...]:
    pr = TelemetrySource.PROVIDER_REPORTED
    return (
        TelemetryItem("tracking_state", pr, ValueState.KNOWN, "idle"),
        TelemetryItem("ambient_temperature_c", pr, ValueState.STALE, 12.5),
        TelemetryItem("focus_position", pr, ValueState.UNKNOWN),
        TelemetryItem("battery_percent", pr, ValueState.UNAVAILABLE),
    )


class SimulatorProvider:
    def __init__(
        self,
        *,
        provider_id: ProviderId = DEFAULT_SIMULATOR_PROVIDER_ID,
        clock: Clock = utc_now,
        devices: Sequence[SimulatedDeviceSpec] = DEFAULT_DEVICES,
        configured: bool = True,
        device_required: bool = False,
    ) -> None:
        self._descriptor = ProviderDescriptor(
            provider_id=provider_id,
            provider_kind="simulator",
            implementation_label="tsn-dss-simulator/1",
            simulated=True,
            configuration_status=(
                ConfigurationStatus.CONFIGURED if configured else ConfigurationStatus.UNCONFIGURED
            ),
            device_required=device_required,
        )
        self._clock = clock
        self._devices = tuple(devices)
        self._discovery_script: deque[ProviderDiscoveryError | None] = deque()
        self._connect_failures: deque[ProviderConnectionError] = deque()
        self._disconnect_failures: deque[ProviderConnectionError] = deque()
        self._evidence_failures: deque[ProviderConnectionError] = deque()
        self._active: set[ConnectionId] = set()
        self._telemetry = _default_telemetry()
        self.calls: list[tuple[str, ...]] = []

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    # --- scripting (test control surface) --------------------------------------

    def script_discovery_failure(self, category: str, message: str = "", *, fatal: bool = False) -> None:
        self._discovery_script.append(ProviderDiscoveryError(category, message, fatal=fatal))

    def script_empty_discovery(self) -> None:
        self._discovery_script.append(None)

    def script_connect_failure(self, category: str, message: str = "") -> None:
        self._connect_failures.append(ProviderConnectionError(category, message))

    def script_disconnect_failure(self, category: str, message: str = "") -> None:
        self._disconnect_failures.append(ProviderConnectionError(category, message))

    def script_evidence_failure(self, category: str, message: str = "") -> None:
        self._evidence_failures.append(ProviderConnectionError(category, message))

    def set_telemetry(self, items: Sequence[TelemetryItem]) -> None:
        self._telemetry = tuple(items)

    @property
    def active_connections(self) -> frozenset[ConnectionId]:
        return frozenset(self._active)

    # --- DeviceProvider ------------------------------------------------------------

    def discover(self) -> Sequence[DeviceReference]:
        self.calls.append(("discover",))
        if self._discovery_script:
            step = self._discovery_script.popleft()
            if step is None:
                return ()
            raise step
        now = self._clock()
        return tuple(
            DeviceReference(
                provider_id=self._descriptor.provider_id,
                device_ref=spec.device_ref,
                discovered_at=now,
                simulated=True,
                label=spec.label,
                model=spec.model,
                firmware_version=spec.firmware_version,
                endpoint_hint=spec.endpoint_hint,
                evidence_freshness=EvidenceFreshness.FRESH,
            )
            for spec in self._devices
        )

    def connect(self, connection_id: ConnectionId, device: DeviceReference) -> None:
        self.calls.append(("connect", connection_id))
        if self._connect_failures:
            raise self._connect_failures.popleft()
        if device.device_ref not in {spec.device_ref for spec in self._devices}:
            raise ProviderConnectionError("unknown_device", device.device_ref)
        self._active.add(connection_id)

    def disconnect(self, connection_id: ConnectionId) -> None:
        self.calls.append(("disconnect", connection_id))
        if self._disconnect_failures:
            raise self._disconnect_failures.popleft()
        self._active.discard(connection_id)

    def describe_capabilities(self, connection_id: ConnectionId | None) -> Sequence[CapabilityEntry]:
        self.calls.append(("describe_capabilities", connection_id or ""))
        if connection_id is not None:
            self._require_connected(connection_id)
        connected = None if connection_id is None else True
        return (
            CapabilityEntry(
                "telemetry.read", True, connected, False, CapabilityConfirmation.SIMULATED, True
            ),
            CapabilityEntry(
                "preview.describe", True, connected, False, CapabilityConfirmation.SIMULATED, True
            ),
            # Supported by this Provider but never currently available in DB-01:
            # Command execution is not implemented until DB-04.
            CapabilityEntry(
                "sim.synthetic_action",
                True,
                None if connection_id is None else False,
                True,
                CapabilityConfirmation.SIMULATED,
                True,
            ),
            CapabilityEntry(
                "heater.control",
                False,
                None if connection_id is None else False,
                True,
                CapabilityConfirmation.UNSUPPORTED,
                True,
            ),
        )

    def read_telemetry(self, connection_id: ConnectionId) -> ProviderTelemetryReading:
        self.calls.append(("read_telemetry", connection_id))
        self._require_connected(connection_id)
        return ProviderTelemetryReading(items=self._telemetry, provider_reported_at=self._clock())

    def describe_preview(self, connection_id: ConnectionId) -> ProviderPreviewReading:
        self.calls.append(("describe_preview", connection_id))
        self._require_connected(connection_id)
        return ProviderPreviewReading(
            source_kind="simulated_test_pattern",
            availability=PreviewAvailability.AVAILABLE,
            provider_reported_at=self._clock(),
        )

    def _require_connected(self, connection_id: ConnectionId) -> None:
        if self._evidence_failures:
            raise self._evidence_failures.popleft()
        if connection_id not in self._active:
            raise ProviderConnectionError("not_connected", connection_id)
