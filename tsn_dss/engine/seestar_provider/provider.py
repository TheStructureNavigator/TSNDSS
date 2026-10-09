"""Seestar read-only Provider behind the vendor-neutral DB-01 runtime.

Implements ``device_runtime.DeviceProvider``. It issues only allow-listed read
requests through ``SeestarReadTransport``; it has no Command surface (DB-04),
opens no preview stream (DB-03) and never writes canonical domain records.
Discovery creates no persistent device identity: a ``device_ref`` is a runtime
reference derived from the device's own serial and is never stored.

Not thread-safe by itself; an internal lock serialises device reads.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Sequence

from ..device_runtime import (
    CapabilityConfirmation,
    CapabilityEntry,
    ConfigurationStatus,
    ConnectionId,
    DeviceReference,
    EvidenceFreshness,
    ProviderConnectionError,
    ProviderDescriptor,
    ProviderDiscoveryError,
    ProviderId,
    ProviderPreviewReading,
    ProviderTelemetryReading,
    TelemetryItem,
    ValueState,
)
from ..device_runtime.support import Clock, utc_now
from .config import SeestarProviderConfig
from .errors import (
    SeestarAuthError,
    SeestarConfigError,
    SeestarError,
    SeestarProtocolError,
    SeestarRpcError,
)
from .normalize import (
    APP_ITEM_NAMES,
    DEVICE_ITEM_NAMES,
    PR,
    DeviceIdentity,
    app_items,
    device_items,
    parse_identity,
    preview_availability,
)
from .protocol import RpcReply, device_ref_for
from .transport import SeestarReadTransport

IMPLEMENTATION_LABEL = "tsn-dss-seestar-readonly/1"
IDENTITY_KEYS = ("device",)
TELEMETRY_KEYS = ("device", "mount", "pi_status", "focuser", "second_focuser", "setting")

# Read capabilities this Provider implements. Hardware confirmation is recorded in
# the operator validation report, never asserted by code.
_READ_CAPABILITIES = ("identity.read", "state.read", "telemetry.read", "preview.availability.read")
# Not implemented by this Provider (DB-02 is read-only).
_UNSUPPORTED_CAPABILITIES = (
    "mount.motion",
    "camera.mode_control",
    "preview.stream",
    "acquisition",
    "heater.control",
)


def _require_ok(reply: RpcReply) -> RpcReply:
    if reply.code != 0:
        raise SeestarRpcError(reply.code)
    return reply


class SeestarProvider:
    def __init__(
        self,
        config: SeestarProviderConfig,
        transport: SeestarReadTransport,
        *,
        clock: Clock = utc_now,
    ) -> None:
        self._config = config
        self._transport = transport
        self._clock = clock
        self._lock = threading.RLock()
        self._endpoints: dict[str, str] = {}
        self._sessions: dict[ConnectionId, str] = {}
        self._last_good: dict[str, dict[str, tuple[TelemetryItem, datetime]]] = {}
        self._descriptor = ProviderDescriptor(
            provider_id=ProviderId(config.provider_id),
            provider_kind="seestar",
            implementation_label=IMPLEMENTATION_LABEL,
            simulated=False,
            configuration_status=(
                ConfigurationStatus.CONFIGURED if config.is_complete else ConfigurationStatus.UNCONFIGURED
            ),
            device_required=bool(config.host),
        )

    def __repr__(self) -> str:
        return f"SeestarProvider(provider_id={self._config.provider_id!r}, <redacted>)"

    @property
    def descriptor(self) -> ProviderDescriptor:
        return self._descriptor

    def reconfigure_host(self, host: str) -> None:
        """Operator-driven IP change. Takes effect on the next discovery."""
        with self._lock:
            self._config = self._config.with_host(host)
            self._endpoints.clear()
            self._last_good.clear()  # values from the old endpoint must not outlive it

    # --- discovery ---------------------------------------------------------------

    def discover(self) -> Sequence[DeviceReference]:
        with self._lock:
            now = self._clock()
            try:
                if self._config.host:
                    pairs = [(self._config.host, self._identify(self._config.host))]
                elif self._config.allow_udp_discovery:
                    pairs = [
                        (a.ip, DeviceIdentity(device_ref_for(a.model, a.sn), a.sn, a.model, None))
                        for a in self._transport.discover_via_udp()
                    ]
                else:
                    raise SeestarConfigError("no_discovery_source")
            except SeestarError as exc:
                fatal = isinstance(exc, (SeestarAuthError, SeestarConfigError))
                raise ProviderDiscoveryError(exc.category, fatal=fatal) from None
            self._endpoints = {identity.device_ref: host for host, identity in pairs}
            return tuple(
                DeviceReference(
                    provider_id=self._descriptor.provider_id,
                    device_ref=identity.device_ref,
                    discovered_at=now,
                    simulated=False,
                    label=identity.model or None,
                    model=identity.model or None,
                    firmware_version=identity.firmware_version,
                    endpoint_hint=None,
                    evidence_freshness=EvidenceFreshness.FRESH,
                )
                for _host, identity in pairs
            )

    def _identify(self, host: str) -> DeviceIdentity:
        reply = _require_ok(self._transport.read_device_state(host, IDENTITY_KEYS))
        return parse_identity(reply.result)

    def _verify_identity(self, host: str, device_ref: str) -> None:
        """Fresh identity read. A different device at the endpoint invalidates retained values."""
        if self._identify(host).device_ref != device_ref:
            self._last_good.pop(device_ref, None)
            raise SeestarProtocolError("identity_mismatch")

    # --- connection ----------------------------------------------------------------

    def connect(self, connection_id: ConnectionId, device: DeviceReference) -> None:
        with self._lock:
            host = self._endpoints.get(device.device_ref)
            if host is None:
                raise ProviderConnectionError("unknown_device")
            self._guard(lambda: self._verify_identity(host, device.device_ref))
            self._sessions[connection_id] = device.device_ref

    def disconnect(self, connection_id: ConnectionId) -> None:
        with self._lock:
            device_ref = self._sessions.pop(connection_id, None)
            if device_ref is not None and device_ref not in self._sessions.values():
                self._last_good.pop(device_ref, None)

    # --- evidence reads ----------------------------------------------------------------

    def describe_capabilities(self, connection_id: ConnectionId | None) -> Sequence[CapabilityEntry]:
        with self._lock:
            available: bool | None = None
            if connection_id is not None:
                host = self._host_for(connection_id)
                device_ref = self._sessions[connection_id]
                # Availability is evidence from a fresh authenticated read that also proves the
                # endpoint still answers as this Connection's device, not a static claim.
                self._guard(lambda: self._verify_identity(host, device_ref))
                available = True
            entries = [
                CapabilityEntry(
                    name, True, available, False, CapabilityConfirmation.IMPLEMENTED_UNTESTED, False
                )
                for name in _READ_CAPABILITIES
            ]
            entries += [
                CapabilityEntry(
                    name,
                    False,
                    None if connection_id is None else False,
                    True,
                    CapabilityConfirmation.UNSUPPORTED,
                    False,
                )
                for name in _UNSUPPORTED_CAPABILITIES
            ]
            return tuple(entries)

    def read_telemetry(self, connection_id: ConnectionId) -> ProviderTelemetryReading:
        with self._lock:
            host = self._host_for(connection_id)
            device_ref = self._sessions[connection_id]
            observed_at = self._clock()
            cache = self._last_good.setdefault(device_ref, {})
            items: list[TelemetryItem] = []
            failed_groups: list[tuple[str, ...]] = []
            failures: list[SeestarError] = []

            try:
                reply = _require_ok(self._transport.read_device_state(host, TELEMETRY_KEYS))
                identity = parse_identity(reply.result)
                device_group = device_items(reply.result, reply.device_timestamp)
            except SeestarError as exc:
                failures.append(exc)
                failed_groups.append(DEVICE_ITEM_NAMES)
            else:
                if identity.device_ref != device_ref:
                    # A different device now answers: nothing retained for this one may be served.
                    self._last_good.pop(device_ref, None)
                    raise ProviderConnectionError("identity_mismatch")
                items.extend(device_group)

            try:
                app_group = app_items(_require_ok(self._transport.read_app_state(host)).result)
            except SeestarError as exc:
                failures.append(exc)
                failed_groups.append(APP_ITEM_NAMES)
            else:
                items.extend(app_group)

            if len(failures) == 2:
                raise self._connection_error(failures[0])
            if device_ref in self._last_good:
                for item in items:
                    if item.state is ValueState.KNOWN:
                        cache[item.name] = (item, observed_at)
            for names in failed_groups:  # a group whose read failed: stale if recently known, else unavailable
                items.extend(self._stale_or_unavailable(cache, names, observed_at))
            return ProviderTelemetryReading(items=tuple(items), provider_reported_at=None)

    def _stale_or_unavailable(
        self,
        cache: dict[str, tuple[TelemetryItem, datetime]],
        names: tuple[str, ...],
        now: datetime,
    ) -> list[TelemetryItem]:
        retained: list[TelemetryItem] = []
        for name in names:
            known = cache.get(name)
            if known is not None and (now - known[1]).total_seconds() <= self._config.stale_retention_s:
                retained.append(TelemetryItem(name, PR, ValueState.STALE, known[0].value))
            else:
                retained.append(TelemetryItem(name, PR, ValueState.UNAVAILABLE))
        return retained

    def describe_preview(self, connection_id: ConnectionId) -> ProviderPreviewReading:
        with self._lock:
            host = self._host_for(connection_id)
            device_ref = self._sessions[connection_id]
            self._guard(lambda: self._verify_identity(host, device_ref))
            reply = self._guard(lambda: _require_ok(self._transport.read_app_state(host)))
            camera = self._config.preview_camera
            return ProviderPreviewReading(
                source_kind=f"rtsp_{'secondary' if camera == 'wide' else 'primary'}_camera",
                availability=preview_availability(reply.result, camera),
                provider_reported_at=None,
            )

    # --- helpers --------------------------------------------------------------------------

    def _host_for(self, connection_id: ConnectionId) -> str:
        device_ref = self._sessions.get(connection_id)
        host = self._endpoints.get(device_ref) if device_ref else None
        if host is None:
            raise ProviderConnectionError("not_connected")
        return host

    def _guard(self, action):
        try:
            return action()
        except SeestarError as exc:
            raise self._connection_error(exc) from None

    @staticmethod
    def _connection_error(exc: SeestarError) -> ProviderConnectionError:
        # Fixed category only: never host, payload or key path.
        return ProviderConnectionError(exc.category)
