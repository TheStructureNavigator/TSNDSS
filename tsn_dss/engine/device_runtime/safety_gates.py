"""Provider-neutral safety gates and freshness predicates (DSS-CTR-013 section 10; DB-04 S3).

``evaluate_gate`` is pure and deterministic: it reads no clock, calls no Provider and keeps no
state. It receives the evidence snapshot, the time and an uncertainty view, and fails closed.

* A safety-sensitive kind passes only if *every* freshness requirement declared by its policy is met by
  fresh, known, uncontradicted evidence about the command's own Provider, Connection and Device
  Reference (REQ-028, REQ-051, REQ-060). Connection ``ready`` is never evidence by itself.
* A requirement may also list ``allowed_values``. Fresh, known evidence whose value is not allowed
  (compared type-sensitively) blocks with ``STATE_UNSAFE``. Expired, stale, unknown, unavailable, missing
  and contradictory evidence keep their own reasons and are never reported as merely unsafe or accepted.
  The gate knows no state names or values; the registrant of a kind supplies them.
* Freshness values come from the policy (``FreshnessRequirement.max_age``); nothing here is a threshold.
  Evidence is fresh when ``0 <= age <= max_age``; a timestamp in the future is not fresh.
* Provider-reported evidence is used as evidence and labeled as such. A passing result never claims
  physical truth (``GateResult.physical_truth_verified`` is always ``False``, REQ-061), and operator
  clearance is reported separately and is not proof of any physical effect (REQ-029, REQ-061).
* The uncertainty view is the S5 interface; no store exists in S3. A missing or failing view blocks.

Requirement items name telemetry items (``"pose"``) or capabilities (``"capability:<name>"``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Protocol, runtime_checkable

from .command_models import CAPABILITY_PREFIX, CommandKindPolicy, FreshnessRequirement, value_allowed
from .models import (
    CapabilityReport,
    ConnectionId,
    ProviderId,
    TelemetryItem,
    TelemetrySample,
    TelemetrySource,
    ValueState,
)

__all__ = [
    "CAPABILITY_PREFIX",
    "EvidenceSnapshot",
    "GateDecision",
    "GateReason",
    "GateReasonCode",
    "GateResult",
    "UncertaintyState",
    "UncertaintyView",
    "evaluate_gate",
]


class GateDecision(Enum):
    PASS = "pass"
    BLOCK = "block"


class GateReasonCode(str, Enum):
    NO_FRESHNESS_DECLARED = "no_freshness_declared"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"
    WRONG_TARGET = "wrong_target"
    MISSING = "missing"
    STATE_STALE = "state_stale"
    STATE_UNKNOWN = "state_unknown"
    STATE_UNAVAILABLE = "state_unavailable"
    EXPIRED = "expired"
    FUTURE_TIMESTAMP = "future_timestamp"
    CONTRADICTORY = "contradictory"
    STATE_UNSAFE = "state_unsafe"
    UNCERTAINTY_UNRESOLVED = "uncertainty_unresolved"
    UNCERTAINTY_UNKNOWN = "uncertainty_unknown"
    UNCERTAINTY_VIEW_MISSING = "uncertainty_view_missing"
    HISTORY_NOT_ESTABLISHED = "history_not_established"


_UNCERTAINTY_CODES = frozenset(
    {
        GateReasonCode.UNCERTAINTY_UNRESOLVED,
        GateReasonCode.UNCERTAINTY_UNKNOWN,
        GateReasonCode.UNCERTAINTY_VIEW_MISSING,
        GateReasonCode.HISTORY_NOT_ESTABLISHED,
    }
)


class UncertaintyState(Enum):
    """What a future uncertainty store (S5) may answer for one Provider and Device Reference.

    ``NONE_RECORDED`` and ``UNKNOWN`` never pass: after a process restart "nothing recorded" is the answer
    to everything (REQ-050), so it is not verified absence of uncertainty. Only a device whose history was
    explicitly established (``RESOLVED_BY_RECOVERY_EVIDENCE`` or ``CLEARED_BY_OPERATOR``) can pass, and even
    then every freshness requirement still applies.
    """

    NONE_RECORDED = "none_recorded"
    UNRESOLVED = "unresolved"
    CLEARED_BY_OPERATOR = "cleared_by_operator"
    RESOLVED_BY_RECOVERY_EVIDENCE = "resolved_by_recovery_evidence"
    UNKNOWN = "unknown"


@runtime_checkable
class UncertaintyView(Protocol):
    def state_for(self, provider_id: ProviderId, device_ref: str) -> UncertaintyState: ...


@dataclass(slots=True, frozen=True)
class EvidenceSnapshot:
    """Runtime evidence handed to the gate. Both parts are optional; absence blocks when required."""

    telemetry: TelemetrySample | None = None
    capabilities: CapabilityReport | None = None


@dataclass(slots=True, frozen=True)
class GateReason:
    code: GateReasonCode
    item: str | None = None
    detail: str = ""

    def __str__(self) -> str:
        return ":".join(p for p in (self.code.value, self.item or "", self.detail) if p)


@dataclass(slots=True, frozen=True)
class GateResult:
    decision: GateDecision
    reasons: tuple[GateReason, ...] = ()
    evidence_basis: tuple[tuple[str, str], ...] = ()
    operator_clearance_used: bool = False

    @property
    def passed(self) -> bool:
        return self.decision is GateDecision.PASS

    @property
    def physical_truth_verified(self) -> bool:
        """Always ``False``: a gate pass is evidence-based permission, not verified physical truth (REQ-061)."""
        return False

    @property
    def blocked_by_uncertainty(self) -> bool:
        return any(r.code in _UNCERTAINTY_CODES for r in self.reasons)

    def summary(self) -> str:
        return ",".join(str(r) for r in self.reasons) if self.reasons else "gate_passed"


def _block(reasons: list[GateReason], basis: list[tuple[str, str]]) -> GateResult:
    return GateResult(GateDecision.BLOCK, tuple(reasons), tuple(basis))


def _age_problem(observed_at: datetime, now: datetime, max_age: timedelta) -> GateReasonCode | None:
    age = now - observed_at
    if age < timedelta(0):
        return GateReasonCode.FUTURE_TIMESTAMP
    if age > max_age:
        return GateReasonCode.EXPIRED
    return None


_STATE_CODES = {
    ValueState.STALE: GateReasonCode.STATE_STALE,
    ValueState.UNKNOWN: GateReasonCode.STATE_UNKNOWN,
    ValueState.UNAVAILABLE: GateReasonCode.STATE_UNAVAILABLE,
}


def _telemetry_item(
    requirement: FreshnessRequirement,
    sample: TelemetrySample,
    now: datetime,
    reasons: list[GateReason],
    basis: list[tuple[str, str]],
) -> None:
    item = requirement.item
    readings: list[TelemetryItem] = [i for i in sample.items if i.name == item]
    if not readings:
        reasons.append(GateReason(GateReasonCode.MISSING, item))
        return
    problems: list[GateReason] = []
    for reading in readings:
        basis.append((item, reading.source.value))
        if reading.state is not ValueState.KNOWN:
            problems.append(GateReason(_STATE_CODES[reading.state], item, reading.source.value))
            continue
        observed = sample.host_observed_at
        if reading.source is TelemetrySource.PROVIDER_REPORTED and sample.provider_reported_at is not None:
            observed = min(observed, sample.provider_reported_at)
            if sample.provider_reported_at > now:
                problems.append(GateReason(GateReasonCode.FUTURE_TIMESTAMP, item, "provider_reported_at"))
                continue
        code = _age_problem(observed, now, requirement.max_age)
        if code is not None:
            problems.append(GateReason(code, item, reading.source.value))
        elif requirement.allowed_values is not None and not value_allowed(reading.value, requirement.allowed_values):
            # fresh and known, but not a value the Command kind accepts; the value itself is never echoed
            problems.append(GateReason(GateReasonCode.STATE_UNSAFE, item, reading.source.value))
    known = [r.value for r in readings if r.state is ValueState.KNOWN]
    if any(value != known[0] for value in known[1:]):
        problems.append(GateReason(GateReasonCode.CONTRADICTORY, item))
    reasons.extend(problems)


def _capability_item(
    requirement: FreshnessRequirement,
    report: CapabilityReport,
    now: datetime,
    reasons: list[GateReason],
    basis: list[tuple[str, str]],
) -> None:
    item = requirement.item
    name = item[len(CAPABILITY_PREFIX):]
    basis.append((item, "capability_report"))
    entry = next((e for e in report.entries if e.name == name), None)
    if entry is None:
        reasons.append(GateReason(GateReasonCode.MISSING, item))
        return
    code = _age_problem(report.observed_at, now, requirement.max_age)
    if code is not None:
        reasons.append(GateReason(code, item, "capability_report"))
    if not entry.supported_by_provider:
        reasons.append(GateReason(GateReasonCode.STATE_UNAVAILABLE, item, "unsupported"))
    elif entry.available_now is None:
        reasons.append(GateReason(GateReasonCode.STATE_UNKNOWN, item, "availability_unknown"))
    elif entry.available_now is False:
        reasons.append(GateReason(GateReasonCode.STATE_UNAVAILABLE, item, "not_available_now"))


def evaluate_gate(
    policy: CommandKindPolicy,
    snapshot: EvidenceSnapshot | None,
    now: datetime,
    *,
    provider_id: ProviderId,
    connection_id: ConnectionId,
    device_ref: str,
    uncertainty: UncertaintyView | None,
) -> GateResult:
    """Pure gate for one Command kind. A kind that is not safety-sensitive passes without evidence."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be a timezone-aware datetime.")
    if not policy.safety_sensitive:
        return GateResult(GateDecision.PASS)

    reasons: list[GateReason] = []
    basis: list[tuple[str, str]] = []

    if not policy.freshness:  # defensive: the policy constructor already refuses this
        reasons.append(GateReason(GateReasonCode.NO_FRESHNESS_DECLARED))

    if snapshot is None:
        reasons.append(GateReason(GateReasonCode.EVIDENCE_UNAVAILABLE))
    else:
        for part in (snapshot.telemetry, snapshot.capabilities):
            if part is not None and (
                part.provider_id != provider_id or part.connection_id != connection_id
            ):
                reasons.append(GateReason(GateReasonCode.WRONG_TARGET))
        for requirement in policy.freshness:
            if requirement.item.startswith(CAPABILITY_PREFIX):
                if snapshot.capabilities is None:
                    reasons.append(GateReason(GateReasonCode.MISSING, requirement.item, "no_capability_report"))
                else:
                    _capability_item(requirement, snapshot.capabilities, now, reasons, basis)
            elif snapshot.telemetry is None:
                reasons.append(GateReason(GateReasonCode.MISSING, requirement.item, "no_telemetry"))
            else:
                _telemetry_item(requirement, snapshot.telemetry, now, reasons, basis)

    clearance = False
    if uncertainty is None:
        reasons.append(GateReason(GateReasonCode.UNCERTAINTY_VIEW_MISSING))
    else:
        try:
            state = uncertainty.state_for(provider_id, device_ref)
        except Exception:
            state = UncertaintyState.UNKNOWN
        if state is UncertaintyState.UNRESOLVED:
            reasons.append(GateReason(GateReasonCode.UNCERTAINTY_UNRESOLVED))
        elif state is UncertaintyState.CLEARED_BY_OPERATOR:
            clearance = True
        elif state is UncertaintyState.NONE_RECORDED:
            reasons.append(GateReason(GateReasonCode.HISTORY_NOT_ESTABLISHED))
        elif state is not UncertaintyState.RESOLVED_BY_RECOVERY_EVIDENCE:
            reasons.append(GateReason(GateReasonCode.UNCERTAINTY_UNKNOWN))

    if reasons:
        return _block(reasons, basis)
    return GateResult(GateDecision.PASS, (), tuple(basis), operator_clearance_used=clearance)
