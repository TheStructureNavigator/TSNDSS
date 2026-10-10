"""In-process unresolved physical uncertainty (DSS-CTR-013 section 11, REQ-048..050, REQ-061; DB-04 S5).

An ``unknown_result`` of a non-idempotent physical Command opens an *entry* keyed by Provider and
Device Reference. Entries live in the running process only: they survive disconnect and reconnect
(a new ``connection_id`` is irrelevant) but a restart loses them, so an empty store is **not**
evidence that hardware is safe. Safety-sensitive Commands always need fresh evidence on top of it.

An entry is resolved only explicitly, in one of two recorded ways that are never conflated:

* ``recovery_evidence``: later read-only evidence classified the observed state. It lists the evidence
  it rests on, labeled by origin; provider-reported evidence stays provider-reported.
* ``operator_clearance``: an authorized operator accepted the risk. It names the operator and the
  reason and is explicitly *not* proof of any physical effect.

Resolving never rewrites the Command: its record stays ``unknown_result`` and the entry keeps
``original_outcome``. Resolving twice is refused. The store is the ``UncertaintyView`` the safety gate
consults; unusable identities answer ``UNKNOWN`` (fail closed).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable

from .errors import DeviceRuntimeError
from .safety_gates import UncertaintyState

__all__ = [
    "RecoveryAssessor",
    "RecoveryVerdict",
    "Resolution",
    "ResolutionKind",
    "UncertaintyAlreadyResolved",
    "UncertaintyEntry",
    "UncertaintyStore",
    "UnknownUncertainty",
]


class UnknownUncertainty(DeviceRuntimeError):
    """No uncertainty entry exists for the given Provider, Device Reference and Command."""


class UncertaintyAlreadyResolved(DeviceRuntimeError):
    """The entry was already resolved; a resolution is never replaced."""


class ResolutionKind(Enum):
    RECOVERY_EVIDENCE = "recovery_evidence"
    OPERATOR_CLEARANCE = "operator_clearance"


@dataclass(slots=True, frozen=True)
class Resolution:
    kind: ResolutionKind
    resolved_at: datetime
    evidence: str
    resolved_by: str | None = None  # the operator, for clearance
    basis: tuple[tuple[str, str], ...] = ()  # (item, origin) the recovery rests on

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, str) or not self.evidence.strip():
            raise ValueError("a resolution needs non-empty evidence or reason.")
        if self.kind is ResolutionKind.OPERATOR_CLEARANCE and not (self.resolved_by or "").strip():
            raise ValueError("an operator clearance names the operator.")
        if self.kind is ResolutionKind.RECOVERY_EVIDENCE and not self.basis:
            raise ValueError("recovery evidence lists the evidence it rests on.")

    @property
    def proves_physical_effect(self) -> bool:
        """Always ``False``: neither recovery classification nor clearance proves what a Command did (REQ-061)."""
        return False


@dataclass(slots=True, frozen=True)
class UncertaintyEntry:
    provider_id: str
    device_ref: str
    command_id: str
    kind_id: str
    created_at: datetime
    origin_connection_id: str
    resolution: Resolution | None = None

    @property
    def original_outcome(self) -> str:
        return "unknown_result"

    @property
    def unresolved(self) -> bool:
        return self.resolution is None


@dataclass(slots=True, frozen=True)
class RecoveryVerdict:
    resolved: bool
    evidence: str = ""
    basis: tuple[tuple[str, str], ...] = ()


# (connection, command_record, evidence_snapshot, now) -> RecoveryVerdict, registered per Command kind
RecoveryAssessor = Callable[[object, object, object, datetime], RecoveryVerdict]


class UncertaintyStore:
    """Thread-safe, in-process, never persisted."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[tuple[str, str], list[UncertaintyEntry]] = {}

    def open_entry(
        self, provider_id: str, device_ref: str, command_id: str, kind_id: str, at: datetime, connection_id: str
    ) -> UncertaintyEntry:
        """Record an uncertainty. Opening the same Command again returns the existing entry."""
        with self._lock:
            entries = self._entries.setdefault((provider_id, device_ref), [])
            for entry in entries:
                if entry.command_id == command_id:
                    return entry
            entry = UncertaintyEntry(provider_id, device_ref, command_id, kind_id, at, connection_id)
            entries.append(entry)
            return entry

    def get(self, provider_id: str, device_ref: str, command_id: str) -> UncertaintyEntry:
        with self._lock:
            for entry in self._entries.get((provider_id, device_ref), ()):
                if entry.command_id == command_id:
                    return entry
        raise UnknownUncertainty(command_id)

    def entries(self, provider_id: str, device_ref: str) -> tuple[UncertaintyEntry, ...]:
        with self._lock:
            return tuple(self._entries.get((provider_id, device_ref), ()))

    def unresolved_devices(self) -> frozenset[tuple[str, str]]:
        with self._lock:
            return frozenset(k for k, v in self._entries.items() if any(e.unresolved for e in v))

    def resolve(self, provider_id: str, device_ref: str, command_id: str, resolution: Resolution) -> UncertaintyEntry:
        """Attach a resolution. Authorization and evidence checks belong to the caller (the executor)."""
        with self._lock:
            entries = self._entries.get((provider_id, device_ref), [])
            for index, entry in enumerate(entries):
                if entry.command_id == command_id:
                    if not entry.unresolved:
                        raise UncertaintyAlreadyResolved(command_id)
                    resolved = UncertaintyEntry(
                        entry.provider_id, entry.device_ref, entry.command_id, entry.kind_id,
                        entry.created_at, entry.origin_connection_id, resolution,
                    )
                    entries[index] = resolved
                    return resolved
        raise UnknownUncertainty(command_id)

    # --- UncertaintyView -----------------------------------------------------

    def state_for(self, provider_id: str, device_ref: str) -> UncertaintyState:
        """Any open entry means ``UNRESOLVED``. With none open, the latest resolution decides how the
        past was resolved; with no entries at all the answer is ``NONE_RECORDED``, which after a
        restart means nothing. Unusable identities answer ``UNKNOWN``."""
        if not (isinstance(provider_id, str) and provider_id.strip() and isinstance(device_ref, str) and device_ref.strip()):
            return UncertaintyState.UNKNOWN
        with self._lock:
            entries = self._entries.get((provider_id, device_ref), ())
            if not entries:
                return UncertaintyState.NONE_RECORDED
            if any(e.unresolved for e in entries):
                return UncertaintyState.UNRESOLVED
            latest = max(entries, key=lambda e: e.resolution.resolved_at)  # type: ignore[union-attr]
            if latest.resolution.kind is ResolutionKind.OPERATOR_CLEARANCE:  # type: ignore[union-attr]
                return UncertaintyState.CLEARED_BY_OPERATOR
            return UncertaintyState.RESOLVED_BY_RECOVERY_EVIDENCE
