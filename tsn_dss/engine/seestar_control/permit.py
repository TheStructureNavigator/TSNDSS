"""Operator permits for physical commands and, separately, for clearing an uncertainty (DB-05 Slice 3).

Deliberately small. A permit is an in-memory object, never persisted, valid for a bounded time, bound to ONE Connection (hence one
device identity) and to the four allow-listed command kinds. It is the DB-04 ``CommandAuthorizer`` the composition is built
with, and it denies everything until the operator has granted it. It follows the pattern of the operator-run experiment
(an explicit opt-in flag, an interactive terminal and a typed phrase), with one addition: arm motion needs a fresh, single-use
confirmation each time.

``OperatorClearance`` is a different object with a different phrase and its own scope and expiry: a command permit never lets an
uncertainty be cleared, and a clearance never lets a command through. Nothing here recovers, retries or sends anything.

Every refusal is a fixed token. Anything missing (grant, identity, expiry, operator, scope, confirmation) means *denied*.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Callable

from ..device_runtime import Connection
from ..device_runtime.errors import DeviceRuntimeError
from .commands import ARM_DEPLOY, ARM_PARK, COMMANDS, GOTO

__all__ = ["ARM_PHRASE", "CLEAR_PHRASE", "GRANT_PHRASE", "OperatorClearance", "OperatorPermit", "PermitError"]

GRANT_PHRASE = "DEPLOY"  # the phrase of the operator-run experiment
ARM_PHRASE = "ARM"
CLEAR_PHRASE = "CLEAR"
ARM_KINDS = (ARM_DEPLOY, ARM_PARK, GOTO)  # movements: each admission needs its own single-use confirmation


class PermitError(DeviceRuntimeError):
    """A permit could not be granted or used. The message is a fixed token."""


def _require(condition: bool, token: str) -> None:
    if not condition:
        raise PermitError(token)


def _check_terms(operator_id: object, valid_for: object, clock: object) -> None:
    _require(isinstance(operator_id, str) and bool(operator_id.strip()), "operator_required")
    _require(isinstance(valid_for, timedelta) and valid_for > timedelta(0), "validity_required")
    _require(callable(clock), "clock_required")


def _consent(allow_flag: bool, interactive: bool, confirm: Callable[[], str], phrase: str, connection: Connection) -> None:
    _require(allow_flag is True, "physical_motion_not_allowed")
    _require(interactive is True, "interactive_consent_required")
    _require(isinstance(connection, Connection) and bool(connection.device.device_ref), "device_identity_missing")
    answer = confirm()
    _require(isinstance(answer, str) and answer.strip() == phrase, "confirmation_not_given")


class OperatorPermit:
    """``CommandAuthorizer``: denies until ``grant`` succeeds, then allows one device's four kinds until it expires."""

    def __init__(self, *, operator_id: str, valid_for: timedelta, clock: Callable[[], datetime]) -> None:
        _check_terms(operator_id, valid_for, clock)
        self._operator, self._valid_for, self._clock = operator_id.strip(), valid_for, clock
        self._lock = threading.Lock()
        self._bound: tuple[str, str] | None = None  # (provider_id, connection_id)
        self._expires: datetime | None = None
        self._arm_confirmed: set[str] = set()

    def __repr__(self) -> str:
        return f"OperatorPermit(granted={self._bound is not None})"

    def grant(self, connection: Connection, *, allow_physical_motion: bool, interactive: bool, confirm: Callable[[], str]) -> None:
        """Bind this permit to ``connection`` (its device) after explicit, interactive consent. Once per permit."""
        with self._lock:
            _require(self._bound is None, "already_granted")
            _consent(allow_physical_motion, interactive, confirm, GRANT_PHRASE, connection)
            self._bound = (connection.provider_id, connection.connection_id)
            self._expires = self._clock() + self._valid_for

    def confirm_arm_motion(self, kind_id: str, confirm: Callable[[], str]) -> None:
        """A fresh, single-use confirmation for the NEXT admission of ``kind_id`` (deploy or park)."""
        with self._lock:
            _require(kind_id in ARM_KINDS, "not_an_arm_motion")
            _require(self._active(), "permit_not_active")
            answer = confirm()
            _require(isinstance(answer, str) and answer.strip() == ARM_PHRASE, "arm_confirmation_not_given")
            self._arm_confirmed.add(kind_id)

    def revoke(self) -> None:
        with self._lock:
            self._bound, self._expires = None, None
            self._arm_confirmed.clear()

    def remaining(self) -> timedelta | None:
        """Time left, or ``None`` when not granted or expired."""
        with self._lock:
            if not self._active():
                return None
            return self._expires - self._clock()  # type: ignore[operator]

    def _active(self) -> bool:
        return self._bound is not None and self._expires is not None and self._clock() < self._expires

    # --- CommandAuthorizer ---------------------------------------------------------------

    def is_authorized(self, requested_by, kind, ref) -> bool:
        try:
            with self._lock:
                if not self._active() or requested_by != self._operator:
                    return False
                kind_id = getattr(kind, "kind_id", None)
                if (kind_id not in COMMANDS and kind_id != GOTO) or (ref.provider_id, ref.connection_id) != self._bound:
                    return False
                if kind_id in ARM_KINDS:
                    if kind_id not in self._arm_confirmed:
                        return False
                    self._arm_confirmed.discard(kind_id)  # single use: consumed by this admission
                return True
        except Exception:
            return False  # fail closed


class OperatorClearance:
    """``ClearanceAuthorizer``: its own consent, phrase, scope and expiry. It never authorizes a command."""

    def __init__(self, *, operator_id: str, valid_for: timedelta, clock: Callable[[], datetime]) -> None:
        _check_terms(operator_id, valid_for, clock)
        self._operator, self._valid_for, self._clock = operator_id.strip(), valid_for, clock
        self._lock = threading.Lock()
        self._device: tuple[str, str] | None = None  # (provider_id, device_ref)
        self._expires: datetime | None = None

    def __repr__(self) -> str:
        return f"OperatorClearance(granted={self._device is not None})"

    def grant(self, connection: Connection, *, allow_clearance: bool, interactive: bool, confirm: Callable[[], str]) -> None:
        with self._lock:
            _require(self._device is None, "already_granted")
            _require(allow_clearance is True, "clearance_not_allowed")
            _require(interactive is True, "interactive_consent_required")
            _require(isinstance(connection, Connection) and bool(connection.device.device_ref), "device_identity_missing")
            answer = confirm()
            _require(isinstance(answer, str) and answer.strip() == CLEAR_PHRASE, "confirmation_not_given")
            self._device = (connection.provider_id, connection.device.device_ref)
            self._expires = self._clock() + self._valid_for

    def may_clear(self, operator_id, provider_id, device_ref) -> bool:
        try:
            with self._lock:
                return (
                    self._device is not None
                    and self._expires is not None
                    and self._clock() < self._expires
                    and operator_id == self._operator
                    and (provider_id, device_ref) == self._device
                )
        except Exception:
            return False
