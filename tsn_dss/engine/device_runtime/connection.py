"""Runtime Connection object (DSS-CTR-013 section 6)."""

from __future__ import annotations

from datetime import datetime

from .errors import InvalidTransition
from .lifecycle import (
    BUSY_CONTROL,
    BusyEvent,
    ConnectionEvent,
    ConnectionTransition,
    TransitionRecord,
    busy_next,
    connection_next,
)
from .models import ConnectionId, ConnectionState, DeviceReference, ProviderId


class Connection:
    """One runtime relationship with exactly one Provider and one Device Reference.

    ``connection_id`` is controlled by TSN DSS and is never a Provider, Device,
    Session, Observation, Capture or Command identity (REQ-009, REQ-010, REQ-038).
    A terminal Connection is never reactivated (REQ-042, REQ-043).
    """

    __slots__ = ("_id", "_device", "_simulated", "_state", "_created_at", "_history")

    def __init__(
        self,
        *,
        connection_id: ConnectionId,
        device: DeviceReference,
        simulated: bool,
        created_at: datetime,
    ) -> None:
        self._id = connection_id
        self._device = device
        self._simulated = simulated
        self._state = ConnectionState.NEW
        self._created_at = created_at
        self._history: list[TransitionRecord] = []

    @property
    def connection_id(self) -> ConnectionId:
        return self._id

    @property
    def provider_id(self) -> ProviderId:
        return self._device.provider_id

    @property
    def device(self) -> DeviceReference:
        return self._device

    @property
    def simulated(self) -> bool:
        return self._simulated

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def created_at(self) -> datetime:
        return self._created_at

    @property
    def history(self) -> tuple[TransitionRecord, ...]:
        return tuple(self._history)

    def apply(self, event: ConnectionEvent, at: datetime, evidence: str = "") -> ConnectionTransition:
        """Apply an event per the lifecycle table; raise ``InvalidTransition`` if not allowed."""
        transition = connection_next(self._state, event)
        if transition.next_state is not self._state or transition.provider_call is not None:
            self._history.append(
                TransitionRecord(
                    self._id, self._state.value, event.value, transition.next_state.value, at, evidence
                )
            )
        self._state = transition.next_state
        return transition

    def apply_busy(self, event: BusyEvent, at: datetime, evidence: str, control: object) -> ConnectionTransition:
        """Enter or leave ``busy``. Requires ``BUSY_CONTROL``; only the DB-04 executor uses it."""
        if control is not BUSY_CONTROL:
            raise InvalidTransition("connection", self._state.value, event.value)
        transition = busy_next(self._state, event)
        self._history.append(
            TransitionRecord(self._id, self._state.value, event.value, transition.next_state.value, at, evidence)
        )
        self._state = transition.next_state
        return transition

    def __repr__(self) -> str:
        return f"Connection({self._id!r}, {self._device.device_ref!r}, {self._state.value})"
