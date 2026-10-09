"""Provider-neutral runtime models for DSS-CTR-013.

Everything here is runtime evidence. None of these types is a canonical TSN DSS
domain identity or record (DSS-CTR-013-REQ-008, REQ-032, REQ-052, REQ-064).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import NewType

ProviderId = NewType("ProviderId", str)
ConnectionId = NewType("ConnectionId", str)
CommandId = NewType("CommandId", str)
PreviewId = NewType("PreviewId", str)

TelemetryScalar = str | int | float | bool | None


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string.")


def _require_aware(name: str, value: datetime | None, *, optional: bool = False) -> None:
    if value is None:
        if optional:
            return
        raise ValueError(f"{name} is required.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware.")


# --- Enumerations -----------------------------------------------------------
#
# The three lifecycle/state enums are deliberately plain ``Enum`` (not ``str``
# mixins) so a Connection state never compares equal to a Command state that
# happens to share a value such as "failed" (DSS-CTR-013-REQ-011).


class ConfigurationStatus(str, Enum):
    UNCONFIGURED = "unconfigured"
    CONFIGURED = "configured"


class ProviderLifecycleState(Enum):
    """Provider lifecycle (DSS-CTR-013 section 4). No device-level states (REQ-005)."""

    UNCONFIGURED = "unconfigured"
    CONFIGURED = "configured"
    DISCOVERING = "discovering"
    AVAILABLE = "available"
    DEGRADED = "degraded"
    FAILED = "failed"


class ConnectionState(Enum):
    """Connection lifecycle (section 6). ``unknown_result`` is deliberately absent (REQ-012).

    ``BUSY`` exists as a state name only; no DB-01 transition enters it (DB-04).
    """

    NEW = "new"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    READY = "ready"
    BUSY = "busy"
    DEGRADED = "degraded"
    DISCONNECTING = "disconnecting"
    DISCONNECTED = "disconnected"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in (ConnectionState.DISCONNECTED, ConnectionState.FAILED)


class CommandState(Enum):
    """Command lifecycle states (section 9). Type definition only; DB-04 owns behavior."""

    REQUESTED = "requested"
    VALIDATED = "validated"
    REJECTED = "rejected"
    SAFETY_BLOCKED = "safety_blocked"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    UNKNOWN_RESULT = "unknown_result"

    @property
    def is_terminal(self) -> bool:
        return self in COMMAND_TERMINAL_STATES


COMMAND_TERMINAL_STATES = frozenset(
    {
        CommandState.REJECTED,
        CommandState.SAFETY_BLOCKED,
        CommandState.SUCCEEDED,
        CommandState.FAILED,
        CommandState.TIMED_OUT,
        CommandState.CANCELLED,
        CommandState.UNKNOWN_RESULT,
    }
)


class EvidenceFreshness(str, Enum):
    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"


class DiscoveryOutcome(str, Enum):
    DEVICES_FOUND = "devices_found"
    EMPTY_VALID = "empty_valid"
    EMPTY_REQUIRED_DEVICE_MISSING = "empty_required_device_missing"
    NONFATAL_FAILURE = "nonfatal_failure"
    FATAL_FAILURE = "fatal_failure"


class CapabilityConfirmation(str, Enum):
    SIMULATED = "simulated"
    HARDWARE_CONFIRMED = "hardware_confirmed"
    IMPLEMENTED_UNTESTED = "implemented_untested"
    UNSUPPORTED = "unsupported"


class ValueState(str, Enum):
    """State of one telemetry value. Non-known states never carry a fabricated value."""

    KNOWN = "known"
    STALE = "stale"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


class TelemetrySource(str, Enum):
    PROVIDER_REPORTED = "provider_reported"
    HOST_OBSERVED = "host_observed"


class PreviewAvailability(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


# --- Provider ---------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ProviderDescriptor:
    """Stable Provider identity and declarations (REQ-001, REQ-002, REQ-035)."""

    provider_id: ProviderId
    provider_kind: str
    implementation_label: str
    simulated: bool
    configuration_status: ConfigurationStatus
    device_required: bool = False

    def __post_init__(self) -> None:
        _require_text("provider_id", self.provider_id)
        _require_text("provider_kind", self.provider_kind)
        _require_text("implementation_label", self.implementation_label)


# --- Devices and discovery --------------------------------------------------


@dataclass(slots=True, frozen=True)
class DeviceReference:
    """Provider-native runtime reference. Not a canonical identity (REQ-008, REQ-065)."""

    provider_id: ProviderId
    device_ref: str
    discovered_at: datetime
    simulated: bool
    label: str | None = None
    model: str | None = None
    firmware_version: str | None = None
    endpoint_hint: str | None = None
    evidence_freshness: EvidenceFreshness = EvidenceFreshness.FRESH

    def __post_init__(self) -> None:
        _require_text("provider_id", self.provider_id)
        _require_text("device_ref", self.device_ref)
        _require_aware("discovered_at", self.discovered_at)

    @property
    def key(self) -> tuple[str, str]:
        return (self.provider_id, self.device_ref)


@dataclass(slots=True, frozen=True)
class DiscoveryResult:
    """Discovery evidence returned to the caller (REQ-006, REQ-036, REQ-037)."""

    provider_id: ProviderId
    outcome: DiscoveryOutcome
    devices: tuple[DeviceReference, ...]
    requested_at: datetime
    completed_at: datetime
    provider_state: ProviderLifecycleState
    simulated: bool
    error_category: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        _require_aware("requested_at", self.requested_at)
        _require_aware("completed_at", self.completed_at)


# --- Capability Report ------------------------------------------------------


@dataclass(slots=True, frozen=True)
class CapabilityEntry:
    """One operation. Provider support and current availability are separate (REQ-013)."""

    name: str
    supported_by_provider: bool
    available_now: bool | None
    requires_safety_gate: bool
    confirmation: CapabilityConfirmation
    simulated: bool

    def __post_init__(self) -> None:
        _require_text("name", self.name)
        if not self.supported_by_provider and self.available_now:
            raise ValueError("an unsupported capability cannot be currently available.")
        if self.confirmation is CapabilityConfirmation.UNSUPPORTED and self.supported_by_provider:
            raise ValueError("confirmation 'unsupported' contradicts supported_by_provider.")


@dataclass(slots=True, frozen=True)
class CapabilityReport:
    """Timestamped statement of support and availability (REQ-013, REQ-014, REQ-056)."""

    provider_id: ProviderId
    connection_id: ConnectionId | None
    observed_at: datetime
    entries: tuple[CapabilityEntry, ...]
    simulated: bool

    def __post_init__(self) -> None:
        _require_text("provider_id", self.provider_id)
        _require_aware("observed_at", self.observed_at)
        names = [entry.name for entry in self.entries]
        if len(names) != len(set(names)):
            raise ValueError("capability names must be unique within a report.")
        if self.connection_id is None and any(e.available_now is not None for e in self.entries):
            raise ValueError("availability is only meaningful for a Connection.")
        if self.simulated and not all(e.simulated for e in self.entries):
            raise ValueError("a simulated report must contain only simulated entries.")

    @property
    def proves_command_success(self) -> bool:
        """Always False: a Capability Report is never proof a Command will succeed (REQ-015)."""
        return False

    def entry(self, name: str) -> CapabilityEntry | None:
        return next((e for e in self.entries if e.name == name), None)


# --- Telemetry --------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class TelemetryItem:
    name: str
    source: TelemetrySource
    state: ValueState
    value: TelemetryScalar = None

    def __post_init__(self) -> None:
        _require_text("name", self.name)
        if self.state in (ValueState.UNKNOWN, ValueState.UNAVAILABLE) and self.value is not None:
            raise ValueError("unknown or unavailable telemetry must not carry a value.")


@dataclass(slots=True, frozen=True)
class TelemetrySample:
    """Read-only runtime evidence (REQ-016, REQ-017, REQ-058)."""

    provider_id: ProviderId
    connection_id: ConnectionId
    host_observed_at: datetime
    items: tuple[TelemetryItem, ...]
    simulated: bool
    provider_reported_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_aware("host_observed_at", self.host_observed_at)
        _require_aware("provider_reported_at", self.provider_reported_at, optional=True)

    def get(self, name: str, source: TelemetrySource | None = None) -> TelemetryItem | None:
        return next(
            (i for i in self.items if i.name == name and (source is None or i.source is source)),
            None,
        )


# --- Preview ----------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class PreviewDescriptor:
    """Runtime preview evidence. Never a Capture or Frame (REQ-018, REQ-033)."""

    preview_id: PreviewId
    provider_id: ProviderId
    connection_id: ConnectionId
    source_kind: str
    availability: PreviewAvailability
    host_observed_at: datetime
    simulated: bool
    provider_reported_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_text("source_kind", self.source_kind)
        _require_aware("host_observed_at", self.host_observed_at)
        _require_aware("provider_reported_at", self.provider_reported_at, optional=True)

    @property
    def is_runtime_evidence_only(self) -> bool:
        return True

    @property
    def is_canonical_record(self) -> bool:
        return False


# --- Command identity (types only; DB-04 owns execution and enforcement) ----


@dataclass(slots=True, frozen=True)
class CommandRef:
    """Command identity bound to a Provider and Connection (REQ-020, REQ-044 type level)."""

    command_id: CommandId
    provider_id: ProviderId
    connection_id: ConnectionId

    def __post_init__(self) -> None:
        _require_text("command_id", self.command_id)
        _require_text("provider_id", self.provider_id)
        _require_text("connection_id", self.connection_id)
