"""Provider contract for the provider-neutral runtime (DSS-CTR-013 section 4).

A Provider supplies evidence. The runtime (``ProviderRuntime``) owns lifecycle,
identity, timestamps and conformance checks. There is deliberately no Command
method in ``DeviceProvider``. Commands go through the separate ``CommandCapableProvider``
(DB-04 S4), which only the Command executor calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol, Sequence, runtime_checkable

from .models import (
    CapabilityEntry,
    CommandId,
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


# --- Commands (DB-04 S4): a separate protocol; DeviceProvider stays read-only --------------


class ProviderCommandStatus(Enum):
    """What a Provider *reports* about a Command it accepted. Reports are not verified physical truth."""

    ACKNOWLEDGED = "acknowledged"
    IN_PROGRESS = "in_progress"
    REPORTED_COMPLETE = "reported_complete"
    REPORTED_FAILED = "reported_failed"


class ProviderCancelOutcome(Enum):
    CANCELLED_NO_EFFECT = "cancelled_no_effect"
    RACE_UNDETERMINED = "race_undetermined"
    REFUSED = "refused"


@dataclass(slots=True, frozen=True)
class ProviderCommandReceipt:
    """The Provider accepted the Command. This is an acknowledgement, never a success."""

    accepted_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class ProviderCommandReport:
    """A Provider's status report.

    Provider trust model: ``effect_possible=False`` is a Provider-reported statement, not independent proof
    (REQ-061). The runtime relies on it in exactly one direction, to let a Provider-reported terminal failure
    be ``failed`` (DSS-CTR-013 section 9, amendment A1). Only an explicit ``False`` counts; ``True``, ``None``
    or any other value means "a physical effect may have occurred" and yields ``unknown_result``. A Provider
    must therefore state ``False`` only when it can support it, and a Provider that cannot say so leaves the
    Command uncertain, which is the safe default.
    """

    status: ProviderCommandStatus
    effect_possible: bool = True
    detail: str = ""


@dataclass(slots=True, frozen=True)
class ProviderCancelResult:
    outcome: ProviderCancelOutcome
    evidence: str = ""


@runtime_checkable
class CommandCapableProvider(Protocol):
    """Command submission, status and cancellation. Failures raise ``ProviderCommandRejected`` (refused
    before acceptance) or ``ProviderConnectionError`` (transport; the effect is then unknown)."""

    @property
    def descriptor(self) -> ProviderDescriptor: ...

    def submit_command(
        self, connection_id: ConnectionId, command_id: CommandId, kind_id: str, idempotency_key: str | None
    ) -> ProviderCommandReceipt: ...

    def poll_command(self, connection_id: ConnectionId, command_id: CommandId) -> ProviderCommandReport: ...

    def cancel_command(self, connection_id: ConnectionId, command_id: CommandId) -> ProviderCancelResult: ...
