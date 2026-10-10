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

**History is unknown until established.** A device this process has not established is never "clear":
``state_for`` answers ``UNKNOWN`` (fail closed) until the device has a *baseline* or a resolved entry.
A baseline is an explicit, recorded act with the same two kinds as a resolution: fresh recovery evidence
assessed for the device, or an authorized operator clearance. Both describe what was accepted at that
moment and neither proves what earlier Commands did. This is the initialization gate of the in-process
store (REQ-049, REQ-050): a newly constructed store, or one in a restarted process, cannot be mistaken for
verified absence of unresolved physical effects. Passive reads never consult it.

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

from .command_models import FreshnessRequirement
from .errors import DeviceRuntimeError
from .safety_gates import UncertaintyState

__all__ = [
    "BaselineRecovery",
    "RecoveryAssessor",
    "RecoveryVerdict",
    "Resolution",
    "ResolutionKind",
    "BaselineAlreadyEstablished",
    "UncertaintyAlreadyResolved",
    "UncertaintyEntry",
    "UncertaintyStore",
    "UnknownUncertainty",
]


class UnknownUncertainty(DeviceRuntimeError):
    """No uncertainty entry exists for the given Provider, Device Reference and Command."""


class UncertaintyAlreadyResolved(DeviceRuntimeError):
    """The entry was already resolved; a resolution is never replaced."""


class BaselineAlreadyEstablished(DeviceRuntimeError):
    """The device already has a baseline or a resolved uncertainty in this process."""


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


@dataclass(slots=True, frozen=True)
class BaselineRecovery:
    """How a device's unknown history may be established from fresh evidence (no defaults, no vendor logic).

    ``freshness`` declares which evidence must be fresh and for how long, chosen by whoever configures it;
    ``assessor`` decides from that evidence whether the device's observed state is acceptable to start from.
    """

    assessor: RecoveryAssessor
    freshness: tuple[FreshnessRequirement, ...]

    def __post_init__(self) -> None:
        if not callable(self.assessor):
            raise ValueError("assessor must be callable.")
        object.__setattr__(self, "freshness", tuple(self.freshness))
        if not self.freshness or not all(isinstance(r, FreshnessRequirement) for r in self.freshness):
            raise ValueError("a baseline recovery declares at least one FreshnessRequirement.")


class UncertaintyStore:
    """Thread-safe, in-process, never persisted."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[tuple[str, str], list[UncertaintyEntry]] = {}
        self._baselines: dict[tuple[str, str], Resolution] = {}

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

    def establish_baseline(self, provider_id: str, device_ref: str, resolution: Resolution) -> Resolution:
        """Record that this process has explicitly established the device's history. Once per device;
        authorization and evidence checks belong to the caller (the executor)."""
        if not (isinstance(provider_id, str) and provider_id.strip() and isinstance(device_ref, str) and device_ref.strip()):
            raise ValueError("baseline needs a Provider and a Device Reference.")
        with self._lock:
            key = (provider_id, device_ref)
            if self._baselines.get(key) is not None or any(not e.unresolved for e in self._entries.get(key, ())):
                raise BaselineAlreadyEstablished(device_ref)
            self._baselines[key] = resolution
            return resolution

    def baseline(self, provider_id: str, device_ref: str) -> Resolution | None:
        with self._lock:
            return self._baselines.get((provider_id, device_ref))

    def history_established(self, provider_id: str, device_ref: str) -> bool:
        with self._lock:
            key = (provider_id, device_ref)
            return key in self._baselines or any(not e.unresolved for e in self._entries.get(key, ()))

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
        """Any open entry means ``UNRESOLVED``. A device whose history this process never established
        answers ``UNKNOWN`` (an empty store is not verified absence of uncertainty). Otherwise the latest
        resolution or baseline says how the history was established. Unusable identities answer ``UNKNOWN``."""
        if not (isinstance(provider_id, str) and provider_id.strip() and isinstance(device_ref, str) and device_ref.strip()):
            return UncertaintyState.UNKNOWN
        with self._lock:
            key = (provider_id, device_ref)
            entries = self._entries.get(key, ())
            if any(e.unresolved for e in entries):
                return UncertaintyState.UNRESOLVED
            established = [e.resolution for e in entries] + ([self._baselines[key]] if key in self._baselines else [])
            if not established:
                return UncertaintyState.UNKNOWN
            latest = max(established, key=lambda r: r.resolved_at)
            if latest.kind is ResolutionKind.OPERATOR_CLEARANCE:
                return UncertaintyState.CLEARED_BY_OPERATOR
            return UncertaintyState.RESOLVED_BY_RECOVERY_EVIDENCE
