"""Neutral readiness gate for opening preview streams (DB-03 Wave 2, local semantics).

A stream may be opened only on positive, fresh, correctly attributed evidence that
the specific camera is available. This module defines that evidence, the pure
decision function and a read-only evidence-provider protocol. It performs no I/O
and never starts, stops or configures a camera; integration with a concrete
provider (Wave 3) supplies evidence through ``ReadinessEvidenceProvider``.

``Connection`` state is a necessary precondition checked by the manager, never
sufficient: a ``ready`` Connection says nothing about whether a camera is
available (see the DB-02 limitation on item-level freshness).

Decision precedence (the first matching rule wins):

1. no evidence                                   -> NO_EVIDENCE
2. evidence names another provider, Connection,
   device or camera                              -> IDENTITY_MISMATCH
3. evidence observed after ``now``               -> CLOCK_REGRESSION
4. evidence observed before the last stream end
   of this camera (``not_before``)               -> PREDATES_LOSS
5. age greater than ``max_age``                  -> STALE_EVIDENCE
6. availability UNKNOWN / UNAVAILABLE            -> UNKNOWN / UNAVAILABLE
7. availability AVAILABLE                        -> ALLOWED

An age exactly equal to ``max_age`` is still fresh (same convention as Wave 1).
The default ``max_age`` is provisional.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Protocol, runtime_checkable

from .models import ConnectionId, PreviewAvailability, ProviderId
from .preview_models import _require_aware, _require_label

__all__ = [
    "DEFAULT_READINESS_MAX_AGE",
    "GateDecision",
    "GateReason",
    "ReadinessEvidence",
    "ReadinessEvidenceProvider",
    "ReadinessGate",
    "ReadinessIdentity",
    "evaluate_readiness",
]

DEFAULT_READINESS_MAX_AGE = timedelta(seconds=10)


class GateReason(Enum):
    ALLOWED = "allowed"
    NO_EVIDENCE = "no_evidence"
    IDENTITY_MISMATCH = "identity_mismatch"
    CLOCK_REGRESSION = "clock_regression"
    PREDATES_LOSS = "predates_loss"
    STALE_EVIDENCE = "stale_evidence"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"
    EVIDENCE_ERROR = "evidence_error"


@dataclass(slots=True, frozen=True)
class ReadinessIdentity:
    """Who the manager is acting for. Evidence must match all of it."""

    provider_id: ProviderId
    connection_id: ConnectionId
    device_ref: str


@dataclass(slots=True, frozen=True)
class ReadinessEvidence:
    """Runtime evidence about one camera of one Connection. Never a canonical record."""

    provider_id: ProviderId
    connection_id: ConnectionId
    device_ref: str
    source_label: str
    availability: PreviewAvailability
    host_observed_at: datetime
    simulated: bool = False

    def __post_init__(self) -> None:
        for name in ("provider_id", "connection_id", "device_ref"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} is required.")
        _require_label("source_label", self.source_label)
        if not isinstance(self.availability, PreviewAvailability):
            raise ValueError("availability must be a PreviewAvailability.")
        _require_aware("host_observed_at", self.host_observed_at)


@dataclass(slots=True, frozen=True)
class GateDecision:
    allowed: bool
    reason: GateReason
    source_label: str
    evidence_age: timedelta | None = None


@runtime_checkable
class ReadinessEvidenceProvider(Protocol):
    """Read-only source of readiness evidence. It must not change any device state."""

    def read_evidence(self, source_label: str) -> ReadinessEvidence | None:
        """Return fresh evidence for ``source_label``, or ``None`` when there is none."""
        ...


def evaluate_readiness(
    evidence: ReadinessEvidence | None,
    *,
    identity: ReadinessIdentity,
    source_label: str,
    now: datetime,
    max_age: timedelta = DEFAULT_READINESS_MAX_AGE,
    not_before: datetime | None = None,
) -> GateDecision:
    """Pure decision. Only AVAILABLE, fresh, correctly attributed evidence allows an open."""
    _require_aware("now", now)
    if not isinstance(max_age, timedelta) or max_age <= timedelta(0):
        raise ValueError("max_age must be a positive timedelta.")

    def deny(reason: GateReason, age: timedelta | None = None) -> GateDecision:
        return GateDecision(False, reason, source_label, age)

    if evidence is None:
        return deny(GateReason.NO_EVIDENCE)
    if (
        evidence.provider_id != identity.provider_id
        or evidence.connection_id != identity.connection_id
        or evidence.device_ref != identity.device_ref
        or evidence.source_label != source_label
    ):
        return deny(GateReason.IDENTITY_MISMATCH)
    age = now - evidence.host_observed_at
    if age < timedelta(0):
        return deny(GateReason.CLOCK_REGRESSION)
    if not_before is not None and evidence.host_observed_at <= not_before:
        return deny(GateReason.PREDATES_LOSS, age)
    if age > max_age:
        return deny(GateReason.STALE_EVIDENCE, age)
    if evidence.availability is PreviewAvailability.AVAILABLE:
        return GateDecision(True, GateReason.ALLOWED, source_label, age)
    if evidence.availability is PreviewAvailability.UNAVAILABLE:
        return deny(GateReason.UNAVAILABLE, age)
    return deny(GateReason.UNKNOWN, age)


class ReadinessGate:
    """Reads evidence from a provider and applies ``evaluate_readiness``. Holds no cache."""

    def __init__(self, evidence_provider: ReadinessEvidenceProvider, *, max_age: timedelta = DEFAULT_READINESS_MAX_AGE) -> None:
        if not isinstance(max_age, timedelta) or max_age <= timedelta(0):
            raise ValueError("max_age must be a positive timedelta.")
        self._provider = evidence_provider
        self._max_age = max_age

    @property
    def max_age(self) -> timedelta:
        return self._max_age

    def check(
        self,
        identity: ReadinessIdentity,
        source_label: str,
        now: datetime,
        *,
        not_before: datetime | None = None,
    ) -> GateDecision:
        """Every call reads evidence anew; a provider failure is a denial, never an allowance."""
        try:
            evidence = self._provider.read_evidence(source_label)
        except Exception:
            return GateDecision(False, GateReason.EVIDENCE_ERROR, source_label)
        if evidence is not None and not isinstance(evidence, ReadinessEvidence):
            return GateDecision(False, GateReason.EVIDENCE_ERROR, source_label)
        return evaluate_readiness(
            evidence, identity=identity, source_label=source_label, now=now, max_age=self._max_age, not_before=not_before
        )
