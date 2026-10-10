"""Provider runtime orchestration: lifecycle, discovery, Connections and read evidence.

``ProviderRuntime`` wraps one ``DeviceProvider``. It holds no database handle and
imports nothing from the canonical domain, so it cannot create or modify domain
records (DSS-CTR-013-REQ-003, REQ-032, REQ-052, REQ-055). It exposes no Command
operation; DB-01 only defines Command identity and state types.

Instances are not thread-safe.
"""

from __future__ import annotations

from datetime import datetime

from .connection import Connection
from .errors import (
    ConnectionNotActive,
    ContractViolation,
    InvalidTransition,
    ProviderConnectionError,
    ProviderDiscoveryError,
    ProviderNotUsable,
)
from .lifecycle import (
    ConnectionEvent,
    ProviderEvent,
    TransitionRecord,
    provider_next_state,
)
from .models import (
    CapabilityEntry,
    CapabilityReport,
    ConfigurationStatus,
    ConnectionId,
    ConnectionState,
    DeviceReference,
    DiscoveryOutcome,
    DiscoveryResult,
    PreviewDescriptor,
    PreviewId,
    ProviderDescriptor,
    ProviderId,
    ProviderLifecycleState,
    TelemetryItem,
    TelemetrySample,
    TelemetrySource,
    ValueState,
)
from .provider import DeviceProvider
from .support import Clock, IdGenerator, random_id_generator, utc_now

_ACTIVE_STATES = (
    ConnectionState.CONNECTED,
    ConnectionState.READY,
    ConnectionState.BUSY,
    ConnectionState.DEGRADED,
)
_LOSS_EVENTS = {
    ConnectionState.DISCONNECTED: ConnectionEvent.TRANSPORT_LOST_DISCONNECTED,
    ConnectionState.DEGRADED: ConnectionEvent.TRANSPORT_LOST_DEGRADED,
    ConnectionState.FAILED: ConnectionEvent.TRANSPORT_LOST_FAILED,
}


class ProviderRuntime:
    def __init__(
        self,
        provider: DeviceProvider,
        *,
        clock: Clock = utc_now,
        id_generator: IdGenerator = random_id_generator,
    ) -> None:
        self._provider = provider
        self._descriptor: ProviderDescriptor = provider.descriptor
        self._clock = clock
        self._ids = id_generator
        self._state = (
            ProviderLifecycleState.CONFIGURED
            if self._descriptor.configuration_status is ConfigurationStatus.CONFIGURED
            else ProviderLifecycleState.UNCONFIGURED
        )
        self._history: list[TransitionRecord] = []
        self._connections: dict[ConnectionId, Connection] = {}
        self._last_discovery: DiscoveryResult | None = None
        self._loss_observers: list = []

    # --- identity and lifecycle ---------------------------------------------

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    @property
    def provider_id(self) -> ProviderId:
        return self._descriptor.provider_id

    @property
    def state(self) -> ProviderLifecycleState:
        return self._state

    @property
    def history(self) -> tuple[TransitionRecord, ...]:
        return tuple(self._history)

    @property
    def last_discovery(self) -> DiscoveryResult | None:
        return self._last_discovery

    def _apply(self, event: ProviderEvent, at: datetime, evidence: str = "") -> None:
        new_state = provider_next_state(self._state, event)
        self._history.append(
            TransitionRecord(self.provider_id, self._state.value, event.value, new_state.value, at, evidence)
        )
        self._state = new_state

    def supply_configuration(self, evidence: str = "") -> None:
        self._apply(ProviderEvent.CONFIGURATION_SUPPLIED, self._clock(), evidence)

    def report_health_partial(self, evidence: str = "") -> None:
        self._apply(ProviderEvent.HEALTH_PARTIAL, self._clock(), evidence)

    def report_health_recovered(self, evidence: str = "") -> None:
        self._apply(ProviderEvent.HEALTH_RECOVERED, self._clock(), evidence)

    def report_fatal_condition(self, category: str, message: str = "") -> None:
        self._apply(ProviderEvent.FATAL_CONDITION, self._clock(), f"{category}: {message}".strip(": "))

    def reset(self, *, configured: bool, evidence: str = "") -> None:
        event = ProviderEvent.RESET_TO_CONFIGURED if configured else ProviderEvent.RESET_TO_UNCONFIGURED
        self._apply(event, self._clock(), evidence)

    # --- discovery -------------------------------------------------------------

    def discover(self) -> DiscoveryResult:
        """Discover Device References. Failures are outcomes, not exceptions (REQ-036).

        Discovery never submits Commands, moves hardware or touches domain
        records (REQ-007, REQ-055). Zero devices is a valid outcome unless the
        Provider requires a specific Device (REQ-037).
        """
        requested_at = self._clock()
        self._apply(ProviderEvent.DISCOVERY_STARTED, requested_at)
        try:
            devices = tuple(self._provider.discover())
            self._validate_devices(devices)
        except ProviderDiscoveryError as exc:
            event = (
                ProviderEvent.DISCOVERY_FAILED_FATAL if exc.fatal else ProviderEvent.DISCOVERY_FAILED_NONFATAL
            )
            return self._finish_discovery(
                requested_at,
                event,
                DiscoveryOutcome.FATAL_FAILURE if exc.fatal else DiscoveryOutcome.NONFATAL_FAILURE,
                (),
                exc.category,
                exc.message,
            )
        except Exception as exc:
            self._apply(ProviderEvent.DISCOVERY_FAILED_FATAL, self._clock(), type(exc).__name__)
            raise

        if devices:
            event, outcome = ProviderEvent.DISCOVERY_FOUND_DEVICES, DiscoveryOutcome.DEVICES_FOUND
        elif self._descriptor.device_required:
            event = ProviderEvent.DISCOVERY_EMPTY_REQUIRED_DEVICE_MISSING
            outcome = DiscoveryOutcome.EMPTY_REQUIRED_DEVICE_MISSING
        else:
            event, outcome = ProviderEvent.DISCOVERY_EMPTY_VALID, DiscoveryOutcome.EMPTY_VALID
        return self._finish_discovery(requested_at, event, outcome, devices, None, None)

    def _finish_discovery(
        self,
        requested_at: datetime,
        event: ProviderEvent,
        outcome: DiscoveryOutcome,
        devices: tuple[DeviceReference, ...],
        category: str | None,
        message: str | None,
    ) -> DiscoveryResult:
        completed_at = self._clock()
        self._apply(event, completed_at, category or outcome.value)
        result = DiscoveryResult(
            provider_id=self.provider_id,
            outcome=outcome,
            devices=devices,
            requested_at=requested_at,
            completed_at=completed_at,
            provider_state=self._state,
            simulated=self._descriptor.simulated,
            error_category=category,
            error_message=message,
        )
        self._last_discovery = result
        return result

    def _validate_devices(self, devices: tuple[DeviceReference, ...]) -> None:
        seen: set[str] = set()
        for device in devices:
            if device.provider_id != self.provider_id:
                raise ContractViolation("discovered Device Reference names a different Provider.")
            if device.simulated != self._descriptor.simulated:
                raise ContractViolation("Device Reference simulation marking contradicts its Provider.")
            if device.device_ref in seen:
                raise ContractViolation("duplicate Device Reference in one discovery result.")
            seen.add(device.device_ref)

    # --- connections -------------------------------------------------------------

    @property
    def connections(self) -> tuple[Connection, ...]:
        return tuple(self._connections.values())

    def get_connection(self, connection_id: ConnectionId) -> Connection | None:
        return self._connections.get(connection_id)

    def open_connection(self, device: DeviceReference) -> Connection:
        """Create a new Connection object in ``new`` state with a fresh ``connection_id``.

        A ``new`` Connection is only an object; no transport attempt has begun (section 6),
        so creation is not gated on Provider state. Reconnecting always goes through
        here, so it always gets a new identity (REQ-042).
        """
        if device.provider_id != self.provider_id:
            raise ContractViolation("Device Reference belongs to a different Provider.")
        connection = Connection(
            connection_id=ConnectionId(self._ids("conn")),
            device=device,
            simulated=self._descriptor.simulated,
            created_at=self._clock(),
        )
        self._connections[connection.connection_id] = connection
        return connection

    def replace_failed_connection(self, failed: Connection) -> Connection:
        """Replace (never reset) a failed Connection with a new one (REQ-043)."""
        self._own(failed)
        if failed.state is not ConnectionState.FAILED:
            raise InvalidTransition("connection", failed.state.value, "replace_failed")
        return self.open_connection(failed.device)

    def connect(self, connection: Connection) -> Connection:
        """Request a connection. Repeated requests make no duplicate Provider call (REQ-041).

        The first attempt (``new`` -> ``connecting``) is refused with ``ProviderNotUsable``
        while the Provider is ``unconfigured`` (section 4: not enough configuration to
        attempt a connection) or ``failed`` (cannot proceed without intervention). The
        Connection stays ``new`` and no Provider call is made. Repeated requests on an
        established Connection are unaffected.
        """
        self._own(connection)
        if connection.state is ConnectionState.NEW and self._state in (
            ProviderLifecycleState.UNCONFIGURED,
            ProviderLifecycleState.FAILED,
        ):
            raise ProviderNotUsable(f"Provider is {self._state.value}; cannot attempt a connection.")
        transition = connection.apply(ConnectionEvent.CONNECT_REQUESTED, self._clock())
        if transition.provider_call is None:
            return connection
        try:
            self._provider.connect(connection.connection_id, connection.device)
        except ProviderConnectionError as exc:
            connection.apply(ConnectionEvent.CANNOT_CONTINUE, self._clock(), exc.category)
            return connection
        except Exception as exc:
            connection.apply(ConnectionEvent.CANNOT_CONTINUE, self._clock(), type(exc).__name__)
            raise
        connection.apply(ConnectionEvent.TRANSPORT_ESTABLISHED, self._clock())
        return connection

    def disconnect(self, connection: Connection) -> Connection:
        """Request a disconnect. Repeated or no-op requests make no Provider call (REQ-041)."""
        self._own(connection)
        transition = connection.apply(ConnectionEvent.DISCONNECT_REQUESTED, self._clock())
        if transition.provider_call is None:
            return connection
        try:
            self._provider.disconnect(connection.connection_id)
        except ProviderConnectionError as exc:
            connection.apply(ConnectionEvent.CANNOT_CONTINUE, self._clock(), exc.category)
            return connection
        except Exception as exc:
            connection.apply(ConnectionEvent.CANNOT_CONTINUE, self._clock(), type(exc).__name__)
            raise
        connection.apply(ConnectionEvent.DISCONNECT_COMPLETED, self._clock())
        return connection

    def add_transport_loss_observer(self, observer) -> None:
        """Register ``observer(connection, resulting_state)``, called after a transport loss was recorded.

        The runtime knows nothing about who listens; the Command executor uses this (DB-04 S4).
        """
        self._loss_observers.append(observer)

    def report_transport_loss(self, connection: Connection, resulting_state: ConnectionState) -> Connection:
        """Record unexpected transport loss. No Provider call. Observers decide the Command side."""
        self._own(connection)
        event = _LOSS_EVENTS.get(resulting_state)
        if event is None:
            raise ValueError("transport loss resolves to disconnected, degraded or failed only.")
        connection.apply(event, self._clock(), "transport_lost")
        for observer in tuple(self._loss_observers):
            observer(connection, resulting_state)
        return connection

    def refresh_evidence(self, connection: Connection) -> ConnectionState:
        """Obtain fresh capability and telemetry evidence; ``ready`` requires it.

        DB-01 reading of section 6 "required fresh state obtained": a successful Provider
        capability read and telemetry read at the host clock count as fresh state/capability
        evidence, so ``connected``/``degraded`` -> ``ready``. A Provider failure while
        reading marks the Connection ``degraded``. Command-kind-specific freshness
        predicates (REQ-051) and "no unresolved safety condition" are DB-04 concerns.
        """
        self._require_active(connection)
        try:
            self._capability_entries(connection.connection_id)
            self._provider.read_telemetry(connection.connection_id)
        except ProviderConnectionError as exc:
            if connection.state not in (ConnectionState.DEGRADED, ConnectionState.BUSY):
                connection.apply(ConnectionEvent.EVIDENCE_STALE_OR_PARTIAL, self._clock(), exc.category)
            return connection.state
        if connection.state in (ConnectionState.CONNECTED, ConnectionState.DEGRADED):
            connection.apply(ConnectionEvent.FRESH_STATE_OBTAINED, self._clock())
        return connection.state

    def _own(self, connection: Connection) -> None:
        if self._connections.get(connection.connection_id) is not connection:
            raise ContractViolation("Connection does not belong to this Provider runtime.")

    def _require_active(self, connection: Connection) -> None:
        self._own(connection)
        if connection.state not in _ACTIVE_STATES:
            raise ConnectionNotActive(f"Connection is {connection.state.value}.")

    # --- read-only evidence ---------------------------------------------------------

    def _capability_entries(self, connection_id: ConnectionId | None) -> tuple[CapabilityEntry, ...]:
        entries = tuple(self._provider.describe_capabilities(connection_id))
        if any(entry.simulated != self._descriptor.simulated for entry in entries):
            raise ContractViolation("capability simulation marking contradicts its Provider.")
        return entries

    def capability_report(self, connection: Connection | None = None) -> CapabilityReport:
        """Timestamped Capability Report. Never proof a Command will succeed (REQ-015)."""
        connection_id = None
        if connection is not None:
            self._require_active(connection)
            connection_id = connection.connection_id
        entries = self._capability_entries(connection_id)
        try:
            return CapabilityReport(
                provider_id=self.provider_id,
                connection_id=connection_id,
                observed_at=self._clock(),
                entries=entries,
                simulated=self._descriptor.simulated,
            )
        except ValueError as exc:
            raise ContractViolation(str(exc)) from exc

    def read_telemetry(self, connection: Connection) -> TelemetrySample:
        """Read-only telemetry. Unknown, stale and unavailable states are preserved (REQ-058)."""
        self._require_active(connection)
        reading = self._provider.read_telemetry(connection.connection_id)
        if any(item.source is not TelemetrySource.PROVIDER_REPORTED for item in reading.items):
            raise ContractViolation("a Provider may only report provider_reported telemetry.")
        host_items = (
            TelemetryItem(
                "connection_state",
                TelemetrySource.HOST_OBSERVED,
                ValueState.KNOWN,
                connection.state.value,
            ),
        )
        try:
            return TelemetrySample(
                provider_id=self.provider_id,
                connection_id=connection.connection_id,
                host_observed_at=self._clock(),
                items=tuple(reading.items) + host_items,
                simulated=self._descriptor.simulated,
                provider_reported_at=reading.provider_reported_at,
            )
        except ValueError as exc:
            raise ContractViolation(str(exc)) from exc

    def describe_preview(self, connection: Connection) -> PreviewDescriptor:
        """Runtime preview descriptor. It is evidence only and creates no Capture or Frame."""
        self._require_active(connection)
        reading = self._provider.describe_preview(connection.connection_id)
        try:
            return PreviewDescriptor(
                preview_id=PreviewId(self._ids("preview")),
                provider_id=self.provider_id,
                connection_id=connection.connection_id,
                source_kind=reading.source_kind,
                availability=reading.availability,
                host_observed_at=self._clock(),
                simulated=self._descriptor.simulated,
                provider_reported_at=reading.provider_reported_at,
            )
        except ValueError as exc:
            raise ContractViolation(str(exc)) from exc
