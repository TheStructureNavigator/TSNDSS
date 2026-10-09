"""Provider contract for the provider-neutral runtime (DSS-CTR-013 section 4).

A Provider supplies evidence. The runtime (``ProviderRuntime``) owns lifecycle,
identity, timestamps and conformance checks. There is deliberately no Command
method in DB-01: Command execution belongs to DB-04.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, Sequence, runtime_checkable

from .models import (
    CapabilityEntry,
    ConnectionId,
    DeviceReference,
    PreviewAvailability,
    ProviderDescriptor,
    TelemetryItem,
)


@dataclass(slots=True, frozen=True)
class ProviderTelemetryReading:
    items: tuple[TelemetryItem, ...]
    provider_reported_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class ProviderPreviewReading:
    source_kind: str
    availability: PreviewAvailability
    provider_reported_at: datetime | None = None


@runtime_checkable
class DeviceProvider(Protocol):
    @property
    def descriptor(self) -> ProviderDescriptor: ...

    def discover(self) -> Sequence[DeviceReference]:
        """Return Device References or raise ``ProviderDiscoveryError``. Must not move hardware."""
        ...

    def connect(self, connection_id: ConnectionId, device: DeviceReference) -> None:
        """Establish transport or raise ``ProviderConnectionError``."""
        ...

    def disconnect(self, connection_id: ConnectionId) -> None:
        """Release the transport or raise ``ProviderConnectionError``."""
        ...

    def describe_capabilities(self, connection_id: ConnectionId | None) -> Sequence[CapabilityEntry]:
        ...

    def read_telemetry(self, connection_id: ConnectionId) -> ProviderTelemetryReading:
        ...

    def describe_preview(self, connection_id: ConnectionId) -> ProviderPreviewReading:
        ...
