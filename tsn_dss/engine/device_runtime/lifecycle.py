"""Provider and Connection lifecycle tables (DSS-CTR-013 sections 4 and 6).

The tables are data. Entering and leaving ``busy`` is kept out of ``CONNECTION_TRANSITIONS``
(``BUSY_TRANSITIONS``, DB-04 S2) so that the public connection events can never change it.
``unknown_result`` effects and the disconnect rejection record while busy belong to later
DB-04 slices; an ordinary disconnect from ``busy`` simply has no transition.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .errors import InvalidTransition
from .models import ConnectionState, ProviderLifecycleState

P = ProviderLifecycleState
C = ConnectionState


@dataclass(slots=True, frozen=True)
class TransitionRecord:
    subject_id: str
    from_state: str
    event: str
    to_state: str
    at: datetime
    evidence: str = ""


# --- Provider lifecycle -----------------------------------------------------


class ProviderEvent(str, Enum):
    CONFIGURATION_SUPPLIED = "configuration_supplied"
    DISCOVERY_STARTED = "discovery_started"
    DISCOVERY_FOUND_DEVICES = "discovery_found_devices"
    DISCOVERY_EMPTY_VALID = "discovery_empty_valid"
    DISCOVERY_EMPTY_REQUIRED_DEVICE_MISSING = "discovery_empty_required_device_missing"
    DISCOVERY_FAILED_NONFATAL = "discovery_failed_nonfatal"
    DISCOVERY_FAILED_FATAL = "discovery_failed_fatal"
    HEALTH_PARTIAL = "health_partial"
    HEALTH_RECOVERED = "health_recovered"
    FATAL_CONDITION = "fatal_condition"
    RESET_TO_CONFIGURED = "reset_to_configured"
    RESET_TO_UNCONFIGURED = "reset_to_unconfigured"


E_ = ProviderEvent

PROVIDER_TRANSITIONS: dict[tuple[ProviderLifecycleState, ProviderEvent], ProviderLifecycleState] = {
    (P.UNCONFIGURED, E_.CONFIGURATION_SUPPLIED): P.CONFIGURED,
    (P.CONFIGURED, E_.DISCOVERY_STARTED): P.DISCOVERING,
    (P.AVAILABLE, E_.DISCOVERY_STARTED): P.DISCOVERING,
    (P.DEGRADED, E_.DISCOVERY_STARTED): P.DISCOVERING,
    (P.DISCOVERING, E_.DISCOVERY_FOUND_DEVICES): P.AVAILABLE,
    (P.DISCOVERING, E_.DISCOVERY_EMPTY_VALID): P.AVAILABLE,
    (P.DISCOVERING, E_.DISCOVERY_EMPTY_REQUIRED_DEVICE_MISSING): P.DEGRADED,
    (P.DISCOVERING, E_.DISCOVERY_FAILED_NONFATAL): P.DEGRADED,
    (P.DISCOVERING, E_.DISCOVERY_FAILED_FATAL): P.FAILED,
    (P.CONFIGURED, E_.HEALTH_PARTIAL): P.DEGRADED,
    (P.AVAILABLE, E_.HEALTH_PARTIAL): P.DEGRADED,
    (P.DEGRADED, E_.HEALTH_RECOVERED): P.AVAILABLE,
    (P.FAILED, E_.RESET_TO_CONFIGURED): P.CONFIGURED,
    (P.FAILED, E_.RESET_TO_UNCONFIGURED): P.UNCONFIGURED,
}
# "Any state: fatal provider condition -> failed".
for _state in ProviderLifecycleState:
    PROVIDER_TRANSITIONS[(_state, E_.FATAL_CONDITION)] = P.FAILED


def provider_next_state(state: ProviderLifecycleState, event: ProviderEvent) -> ProviderLifecycleState:
    try:
        return PROVIDER_TRANSITIONS[(state, event)]
    except KeyError:
        raise InvalidTransition("provider", state.value, event.value) from None


# --- Connection lifecycle ---------------------------------------------------


class ConnectionEvent(str, Enum):
    CONNECT_REQUESTED = "connect_requested"
    TRANSPORT_ESTABLISHED = "transport_established"
    FRESH_STATE_OBTAINED = "fresh_state_obtained"
    USABLE_EVIDENCE_RETURNED = "usable_evidence_returned"
    EVIDENCE_STALE_OR_PARTIAL = "evidence_stale_or_partial"
    DISCONNECT_REQUESTED = "disconnect_requested"
    DISCONNECT_COMPLETED = "disconnect_completed"
    TRANSPORT_LOST_DISCONNECTED = "transport_lost_disconnected"
    TRANSPORT_LOST_DEGRADED = "transport_lost_degraded"
    TRANSPORT_LOST_FAILED = "transport_lost_failed"
    CANNOT_CONTINUE = "cannot_continue"


class ProviderCall(str, Enum):
    """Underlying Provider call a transition requires (none for repeated requests, REQ-041)."""

    CONNECT = "connect"
    DISCONNECT = "disconnect"


@dataclass(slots=True, frozen=True)
class ConnectionTransition:
    next_state: ConnectionState
    provider_call: ProviderCall | None = None


V = ConnectionEvent
_Table = dict[tuple[ConnectionState, ConnectionEvent], ConnectionTransition]

CONNECTION_TRANSITIONS: _Table = {
    (C.NEW, V.CONNECT_REQUESTED): ConnectionTransition(C.CONNECTING, ProviderCall.CONNECT),
    (C.CONNECTING, V.CONNECT_REQUESTED): ConnectionTransition(C.CONNECTING),
    (C.CONNECTING, V.TRANSPORT_ESTABLISHED): ConnectionTransition(C.CONNECTED),
    (C.CONNECTED, V.CONNECT_REQUESTED): ConnectionTransition(C.CONNECTED),
    (C.READY, V.CONNECT_REQUESTED): ConnectionTransition(C.READY),
    (C.CONNECTED, V.FRESH_STATE_OBTAINED): ConnectionTransition(C.READY),
    (C.DEGRADED, V.FRESH_STATE_OBTAINED): ConnectionTransition(C.READY),
    (C.DEGRADED, V.USABLE_EVIDENCE_RETURNED): ConnectionTransition(C.CONNECTED),
    (C.CONNECTED, V.EVIDENCE_STALE_OR_PARTIAL): ConnectionTransition(C.DEGRADED),
    (C.READY, V.EVIDENCE_STALE_OR_PARTIAL): ConnectionTransition(C.DEGRADED),
    (C.CONNECTED, V.DISCONNECT_REQUESTED): ConnectionTransition(C.DISCONNECTING, ProviderCall.DISCONNECT),
    (C.READY, V.DISCONNECT_REQUESTED): ConnectionTransition(C.DISCONNECTING, ProviderCall.DISCONNECT),
    (C.DEGRADED, V.DISCONNECT_REQUESTED): ConnectionTransition(C.DISCONNECTING, ProviderCall.DISCONNECT),
    (C.DISCONNECTING, V.DISCONNECT_REQUESTED): ConnectionTransition(C.DISCONNECTING),
    (C.NEW, V.DISCONNECT_REQUESTED): ConnectionTransition(C.NEW),
    (C.DISCONNECTED, V.DISCONNECT_REQUESTED): ConnectionTransition(C.DISCONNECTED),
    (C.DISCONNECTING, V.DISCONNECT_COMPLETED): ConnectionTransition(C.DISCONNECTED),
}
# "Any nonterminal state" (section 6), including ``busy`` since DB-04 S2.
_NONTERMINAL = (C.NEW, C.CONNECTING, C.CONNECTED, C.READY, C.BUSY, C.DEGRADED, C.DISCONNECTING)
for _state in _NONTERMINAL:
    CONNECTION_TRANSITIONS[(_state, V.TRANSPORT_LOST_DISCONNECTED)] = ConnectionTransition(C.DISCONNECTED)
    CONNECTION_TRANSITIONS[(_state, V.TRANSPORT_LOST_DEGRADED)] = ConnectionTransition(C.DEGRADED)
    CONNECTION_TRANSITIONS[(_state, V.TRANSPORT_LOST_FAILED)] = ConnectionTransition(C.FAILED)
    CONNECTION_TRANSITIONS[(_state, V.CANNOT_CONTINUE)] = ConnectionTransition(C.FAILED)


class BusyEvent(str, Enum):
    """Events that enter and leave ``busy``. Deliberately not ``ConnectionEvent`` members: they are
    reachable only through ``Connection.apply_busy`` with ``BUSY_CONTROL``, which the DB-04
    executor alone holds (DB-04 S2)."""

    BUSY_ENTERED = "busy_entered"
    BUSY_LEFT_READY = "busy_left_ready"
    BUSY_LEFT_DEGRADED = "busy_left_degraded"


class _BusyControl:
    """Marker type of the capability that authorizes ``busy`` transitions."""

    __slots__ = ()


BUSY_CONTROL = _BusyControl()

BUSY_TRANSITIONS: dict[tuple[ConnectionState, BusyEvent], ConnectionTransition] = {
    (C.READY, BusyEvent.BUSY_ENTERED): ConnectionTransition(C.BUSY),
    (C.BUSY, BusyEvent.BUSY_LEFT_READY): ConnectionTransition(C.READY),
    (C.BUSY, BusyEvent.BUSY_LEFT_DEGRADED): ConnectionTransition(C.DEGRADED),
}


def busy_next(state: ConnectionState, event: BusyEvent) -> ConnectionTransition:
    try:
        return BUSY_TRANSITIONS[(state, event)]
    except KeyError:
        raise InvalidTransition("connection", state.value, event.value) from None


def connection_next(state: ConnectionState, event: ConnectionEvent) -> ConnectionTransition:
    """Return the transition; terminal states cannot be reactivated (REQ-042, REQ-043)."""
    try:
        return CONNECTION_TRANSITIONS[(state, event)]
    except KeyError:
        raise InvalidTransition("connection", state.value, event.value) from None
