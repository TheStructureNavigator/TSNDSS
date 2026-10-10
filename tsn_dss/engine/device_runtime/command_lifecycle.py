"""Command lifecycle table (DSS-CTR-013 section 9; DB-04 S1).

The table is data, one row per contract transition. It is pure: no Provider call, no clock, no
submission. ``requested -> rejected`` / ``safety_blocked`` are alternative outcomes, not steps
toward execution (REQ-023). Provider acknowledgement only ever reaches ``acknowledged``; ``succeeded``
needs a verification event (REQ-024). A deadline never decides an outcome by itself (REQ-045): the
event names the evidence that selects it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from .errors import InvalidTransition
from .lifecycle import TransitionRecord
from .models import CommandState, ProviderId, ConnectionId, CommandId
from .command_models import CommandKindPolicy

S = CommandState


class CommandEvent(str, Enum):
    REQUEST_INVALID_OR_UNSUPPORTED = "request_invalid_or_unsupported"
    CONFLICT_REJECTED = "conflict_rejected"
    CONFLICT_SAFETY_BLOCKED = "conflict_safety_blocked"
    REQUEST_VALID = "request_valid"
    DEADLINE_BEFORE_SUBMISSION = "deadline_before_submission"
    SAFETY_EVIDENCE_INSUFFICIENT = "safety_evidence_insufficient"
    UNCERTAINTY_NOT_CLEARED = "uncertainty_not_cleared"
    GATES_PASSED = "gates_passed"
    PROVIDER_REJECTED_NO_EFFECT = "provider_rejected_no_effect"
    PROVIDER_REJECTED_EFFECT_POSSIBLE = "provider_rejected_effect_possible"
    PROVIDER_ACKNOWLEDGED = "provider_acknowledged"
    MONITORING_STARTED = "monitoring_started"
    ACKNOWLEDGEMENT_IS_VERIFIED_EFFECT = "acknowledgement_is_verified_effect"
    EFFECT_VERIFIED = "effect_verified"
    EFFECT_FAILED = "effect_failed"
    PROVIDER_FAILURE_EFFECT_POSSIBLE = "provider_failure_effect_possible"
    DEADLINE_EFFECT_UNDETERMINED = "deadline_effect_undetermined"
    DEADLINE_NO_EFFECT_PROVEN = "deadline_no_effect_proven"
    DEADLINE_NON_PHYSICAL = "deadline_non_physical"
    TRANSPORT_LOST_EFFECT_UNKNOWN = "transport_lost_effect_unknown"
    CANCEL_ACCEPTED_NO_EFFECT = "cancel_accepted_no_effect"
    CANCEL_RACE_UNDETERMINED = "cancel_race_undetermined"


@dataclass(slots=True, frozen=True)
class CommandTransition:
    next_state: CommandState
    required_evidence: str
    non_physical_only: bool = False


E = CommandEvent
_T = CommandTransition
_PRE = (S.REQUESTED, S.VALIDATED)
_LIVE = (S.SUBMITTED, S.ACKNOWLEDGED, S.IN_PROGRESS)

COMMAND_TRANSITIONS: dict[tuple[CommandState, CommandEvent], CommandTransition] = {
    (S.REQUESTED, E.REQUEST_INVALID_OR_UNSUPPORTED): _T(S.REJECTED, "validation error"),
    (S.REQUESTED, E.CONFLICT_REJECTED): _T(S.REJECTED, "active command identity and conflict reason"),
    (S.REQUESTED, E.CONFLICT_SAFETY_BLOCKED): _T(S.SAFETY_BLOCKED, "active command identity and conflict reason"),
    (S.REQUESTED, E.REQUEST_VALID): _T(S.VALIDATED, "validation record"),
    (S.VALIDATED, E.SAFETY_EVIDENCE_INSUFFICIENT): _T(S.SAFETY_BLOCKED, "safety evaluation record"),
    (S.VALIDATED, E.UNCERTAINTY_NOT_CLEARED): _T(S.SAFETY_BLOCKED, "device reference and provider uncertainty record"),
    (S.VALIDATED, E.GATES_PASSED): _T(S.SUBMITTED, "submission timestamp or possible-submission boundary"),
    (S.SUBMITTED, E.PROVIDER_REJECTED_NO_EFFECT): _T(S.FAILED, "provider rejection evidence"),
    (S.SUBMITTED, E.PROVIDER_REJECTED_EFFECT_POSSIBLE): _T(S.UNKNOWN_RESULT, "provider rejection and uncertainty evidence"),
    (S.SUBMITTED, E.PROVIDER_ACKNOWLEDGED): _T(S.ACKNOWLEDGED, "provider acknowledgement evidence"),
    (S.SUBMITTED, E.DEADLINE_NO_EFFECT_PROVEN): _T(S.TIMED_OUT, "timeout record and no-effect evidence"),
    (S.ACKNOWLEDGED, E.MONITORING_STARTED): _T(S.IN_PROGRESS, "monitoring start evidence"),
    (S.ACKNOWLEDGED, E.ACKNOWLEDGEMENT_IS_VERIFIED_EFFECT): _T(
        S.SUCCEEDED, "requirement-specific verification evidence", non_physical_only=True
    ),
}
for _s in _PRE:
    COMMAND_TRANSITIONS[(_s, E.DEADLINE_BEFORE_SUBMISSION)] = _T(
        S.TIMED_OUT, "deadline record and evidence that no provider submission occurred"
    )
for _s in (S.ACKNOWLEDGED, S.IN_PROGRESS):
    COMMAND_TRANSITIONS[(_s, E.EFFECT_VERIFIED)] = _T(S.SUCCEEDED, "fresh post-command evidence")
    COMMAND_TRANSITIONS[(_s, E.EFFECT_FAILED)] = _T(
        S.FAILED, "failure evidence; for a Provider-reported failure, the Provider's statement that no physical effect was possible"
    )
    COMMAND_TRANSITIONS[(_s, E.PROVIDER_FAILURE_EFFECT_POSSIBLE)] = _T(
        S.UNKNOWN_RESULT, "provider failure report and uncertainty classification"
    )
for _s in _LIVE:
    COMMAND_TRANSITIONS[(_s, E.DEADLINE_EFFECT_UNDETERMINED)] = _T(
        S.UNKNOWN_RESULT, "deadline record and uncertainty classification"
    )
    COMMAND_TRANSITIONS[(_s, E.DEADLINE_NON_PHYSICAL)] = _T(
        S.TIMED_OUT, "timeout record and no-physical-effect classification", non_physical_only=True
    )
    COMMAND_TRANSITIONS[(_s, E.TRANSPORT_LOST_EFFECT_UNKNOWN)] = _T(
        S.UNKNOWN_RESULT, "transport-loss and last-known-command evidence"
    )
    COMMAND_TRANSITIONS[(_s, E.CANCEL_ACCEPTED_NO_EFFECT)] = _T(
        S.CANCELLED, "cancellation evidence and no-effect or controlled-stop evidence"
    )
    COMMAND_TRANSITIONS[(_s, E.CANCEL_RACE_UNDETERMINED)] = _T(
        S.UNKNOWN_RESULT, "cancellation evidence and race assessment"
    )


def command_next(
    state: CommandState, event: CommandEvent, policy: CommandKindPolicy | None = None
) -> CommandTransition:
    """The transition for ``event`` from ``state``; terminal states have none.

    Rows marked ``non_physical_only`` additionally need a ``policy`` that is not physical.
    """
    try:
        row = COMMAND_TRANSITIONS[(state, event)]
    except KeyError:
        raise InvalidTransition("command", state.value, event.value) from None
    if row.non_physical_only and (policy is None or policy.physical):
        raise InvalidTransition("command", state.value, event.value)
    return row


class CommandRecord:
    """Lifecycle state holder for one Command. Pure bookkeeping: it executes nothing."""

    __slots__ = ("_command_id", "_provider_id", "_connection_id", "_policy", "_state", "_history", "_parameters")

    def __init__(
        self, *, command_id: CommandId, provider_id: ProviderId, connection_id: ConnectionId, policy: CommandKindPolicy | None,
        parameters: object = None,
    ) -> None:
        self._parameters = parameters
        self._command_id = command_id
        self._provider_id = provider_id
        self._connection_id = connection_id
        self._policy = policy
        self._state = CommandState.REQUESTED
        self._history: list[TransitionRecord] = []

    command_id = property(lambda self: self._command_id)
    provider_id = property(lambda self: self._provider_id)
    connection_id = property(lambda self: self._connection_id)
    policy = property(lambda self: self._policy)
    parameters = property(lambda self: self._parameters)  # the request's immutable parameters, or None
    state = property(lambda self: self._state)
    history = property(lambda self: tuple(self._history))

    def apply(self, event: CommandEvent, at: datetime, evidence: str) -> CommandTransition:
        """Apply ``event``. Evidence is mandatory: every contract row names the evidence it needs."""
        if not isinstance(evidence, str) or not evidence.strip():
            raise InvalidTransition("command", self._state.value, event.value)
        row = command_next(self._state, event, self._policy)
        self._history.append(TransitionRecord(self._command_id, self._state.value, event.value, row.next_state.value, at, evidence))
        self._state = row.next_state
        return row
