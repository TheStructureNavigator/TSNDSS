"""Command admission and per-Connection exclusivity (DSS-CTR-013 sections 6, 9 and 10; DB-04 S2-S3).

Admission decides whether a requested Command may *begin*. It never submits anything to a Provider
(that is S4). The only Provider interaction is the passive evidence read a safety-sensitive kind's
gate needs, made through the runtime; passive reads never create Commands (REQ-022).

Outcomes of ``admit``, in order (each rejection happens before any possible submission):

1. ``rejected``: unusable request (unknown kind, malformed fields, Connection not owned by the
   runtime or not active, idempotency key on a non-idempotent kind).
2. ``timed_out``: the deadline has already elapsed; nothing was submitted.
3. ``rejected``: a conflicting state-changing Command is active on the Connection (REQ-039, REQ-040;
   the contract allows rejected or safety_blocked, this implementation chooses rejected).
4. ``validated``, then ``safety_blocked``: the caller is not authorized (explicit, fail-closed;
   REQ-021), or a state-changing Command targets a Connection that is not ``ready``.
5. A state-changing Command takes the Connection's one exclusive slot and the Connection enters
   ``busy``.
6. A safety-sensitive kind then faces the safety gate (``safety_gates.evaluate_gate``) on fresh
   evidence read through the injected evidence source (passive reads only). A blocked Command
   becomes ``safety_blocked`` and gives its ``busy`` reservation back. Otherwise it stays
   ``validated``, still holding the slot.

``admit`` and ``check_deadline`` are serialized by one lock, so two concurrent admissions can never
reserve the same Connection slot. The lock is held across the authorizer and the evidence read; both
must therefore not call back into the executor from another thread.

``busy`` is entered when a state-changing Command is admitted (reaches ``validated`` and wins the
slot) and left when that Command reaches a terminal outcome. The contract words the entry as
"State-changing Command begins"; S2 has no submission, so admission is the earliest point at which
exclusivity can be made atomic. Later slices may move the entry to submission.
"""

from __future__ import annotations

import threading
from typing import Callable, Protocol, runtime_checkable

from .command_lifecycle import CommandEvent, CommandRecord
from .command_models import (
    CommandKindPolicy,
    CommandKindRegistry,
    CommandPolicyError,
    CommandRequest,
    UnknownCommandKind,
)
from .connection import Connection
from .errors import DeviceRuntimeError
from .lifecycle import BUSY_CONTROL, BusyEvent
from .models import CommandId, CommandRef, CommandState, ConnectionState
from .runtime import ProviderRuntime
from .safety_gates import EvidenceSnapshot, UncertaintyView, evaluate_gate
from .support import Clock, IdGenerator, random_id_generator, utc_now

__all__ = ["CommandAuthorizer", "CommandExecutor", "CommandIntent"]

_ADMISSIBLE_TARGETS = (
    ConnectionState.CONNECTED,
    ConnectionState.READY,
    ConnectionState.BUSY,
    ConnectionState.DEGRADED,
)


@runtime_checkable
class CommandAuthorizer(Protocol):
    """Explicit authorization (REQ-021). There is no default authorizer: the executor needs one."""

    def is_authorized(self, requested_by: str, kind: CommandKindPolicy, ref: CommandRef) -> bool: ...


class CommandIntent:
    """What a caller asks for. Identity and binding (``command_id``, Provider, Connection) are
    assigned by the executor, never by the caller (REQ-020, REQ-044)."""

    __slots__ = ("connection", "kind_id", "requested_by", "deadline", "idempotency_key")

    def __init__(self, connection: Connection, kind_id: str, requested_by: str, deadline=None, idempotency_key=None):
        self.connection = connection
        self.kind_id = kind_id
        self.requested_by = requested_by
        self.deadline = deadline
        self.idempotency_key = idempotency_key


class CommandExecutor:
    def __init__(
        self,
        runtime: ProviderRuntime,
        kinds: CommandKindRegistry,
        authorizer: CommandAuthorizer,
        *,
        clock: Clock = utc_now,
        id_generator: IdGenerator = random_id_generator,
        evidence_source: Callable[[Connection], EvidenceSnapshot] | None = None,
        uncertainty: UncertaintyView | None = None,
    ) -> None:
        if not isinstance(authorizer, CommandAuthorizer):
            raise DeviceRuntimeError("an explicit CommandAuthorizer is required.")
        self._runtime = runtime
        self._kinds = kinds
        self._authorizer = authorizer
        self._clock = clock
        self._ids = id_generator
        self._evidence_source = evidence_source or self._runtime_evidence
        self._uncertainty = uncertainty
        self._lock = threading.RLock()
        self._records: dict[CommandId, CommandRecord] = {}
        self._requests: dict[CommandId, CommandRequest] = {}
        self._active: dict[str, CommandRecord] = {}  # connection_id -> its one active state-changing Command

    # --- inspection -----------------------------------------------------------

    @property
    def commands(self) -> tuple[CommandRecord, ...]:
        return tuple(self._records.values())

    def get(self, command_id: CommandId) -> CommandRecord | None:
        return self._records.get(command_id)

    def active_command(self, connection: Connection) -> CommandRecord | None:
        return self._active.get(connection.connection_id)

    # --- admission ------------------------------------------------------------

    def admit(self, intent: CommandIntent) -> CommandRecord:
        """Decide whether the Command may begin. Never submits anything to a Provider."""
        with self._lock:
            return self._admit(intent)

    def _admit(self, intent: CommandIntent) -> CommandRecord:
        now = self._clock()
        command_id = CommandId(self._ids("cmd"))
        connection = intent.connection
        policy = self._policy_or_none(intent.kind_id)
        record = CommandRecord(
            command_id=command_id,
            provider_id=self._runtime.provider_id,
            connection_id=getattr(connection, "connection_id", ""),
            policy=policy,
        )
        self._records[command_id] = record

        reason = self._invalid_reason(intent, policy)
        request = None
        if reason is None:
            try:
                request = CommandRequest(
                    ref=CommandRef(command_id, record.provider_id, record.connection_id),
                    kind_id=intent.kind_id,
                    requested_by=intent.requested_by,
                    requested_at=now,
                    deadline=intent.deadline,
                    idempotency_key=intent.idempotency_key,
                )
            except (CommandPolicyError, ValueError):
                reason = "malformed_request"
            else:
                reason = request.violates_policy(policy)
        if reason is not None:
            record.apply(CommandEvent.REQUEST_INVALID_OR_UNSUPPORTED, now, reason)
            return record
        self._requests[command_id] = request

        if request.deadline is not None and request.deadline <= now:
            record.apply(CommandEvent.DEADLINE_BEFORE_SUBMISSION, now, "deadline elapsed before admission; nothing submitted")
            return record

        if policy.state_changing:
            conflict = self._conflict(connection)
            if conflict is not None:
                record.apply(CommandEvent.CONFLICT_REJECTED, now, conflict)
                return record

        record.apply(CommandEvent.REQUEST_VALID, now, "request shape and target references valid")

        if not self._is_authorized(request, policy):
            record.apply(CommandEvent.SAFETY_EVIDENCE_INSUFFICIENT, now, "authorization missing or denied")
            return record

        if policy.state_changing:
            if connection.state is not ConnectionState.READY:
                record.apply(
                    CommandEvent.SAFETY_EVIDENCE_INSUFFICIENT, now, f"connection_not_ready:{connection.state.value}"
                )
                return record
            self._active[connection.connection_id] = record
            connection.apply_busy(BusyEvent.BUSY_ENTERED, now, command_id, BUSY_CONTROL)

        if policy.safety_sensitive:
            result = self._evaluate_gate(connection, policy)
            if not result.passed:
                event = (
                    CommandEvent.UNCERTAINTY_NOT_CLEARED
                    if result.blocked_by_uncertainty
                    else CommandEvent.SAFETY_EVIDENCE_INSUFFICIENT
                )
                at = self._clock()
                record.apply(event, at, result.summary())
                self._release(record, at)
        return record

    def check_deadline(self, command_id: CommandId) -> CommandRecord:
        """Time out a Command whose deadline elapsed while it is still ``validated`` (nothing was submitted).

        Only ``validated`` is touched. This pre-submission timeout never applies once a Command may
        have been submitted: that case is ``unknown_result`` territory (S4).
        """
        with self._lock:
            return self._check_deadline(command_id)

    def _check_deadline(self, command_id: CommandId) -> CommandRecord:
        record = self._records[command_id]
        request = self._requests.get(command_id)
        now = self._clock()
        if (
            record.state is CommandState.VALIDATED
            and request is not None
            and request.deadline is not None
            and request.deadline <= now
        ):
            record.apply(CommandEvent.DEADLINE_BEFORE_SUBMISSION, now, "deadline elapsed before submission; nothing submitted")
            self._release(record, now)
        return record

    # --- internals ------------------------------------------------------------

    def _runtime_evidence(self, connection: Connection) -> EvidenceSnapshot:
        """Default evidence source: passive runtime reads only."""
        return EvidenceSnapshot(
            telemetry=self._runtime.read_telemetry(connection),
            capabilities=self._runtime.capability_report(connection),
        )

    def _evaluate_gate(self, connection: Connection, policy: CommandKindPolicy):
        try:
            snapshot = self._evidence_source(connection)
        except Exception:
            snapshot = None  # fail closed
        return evaluate_gate(  # the clock is read after the evidence, never before it
            policy,
            snapshot,
            self._clock(),
            provider_id=self._runtime.provider_id,
            connection_id=connection.connection_id,
            device_ref=connection.device.device_ref,
            uncertainty=self._uncertainty,
        )

    def _policy_or_none(self, kind_id: object) -> CommandKindPolicy | None:
        try:
            return self._kinds.get(kind_id)  # type: ignore[arg-type]
        except (UnknownCommandKind, TypeError):
            return None

    def _invalid_reason(self, intent: CommandIntent, policy: CommandKindPolicy | None) -> str | None:
        connection = intent.connection
        if policy is None:
            return "unknown_kind"
        if not isinstance(connection, Connection) or self._runtime.get_connection(connection.connection_id) is not connection:
            return "connection_not_owned"
        if connection.state not in _ADMISSIBLE_TARGETS:
            return f"connection_not_active:{connection.state.value}"
        return None

    def _conflict(self, connection: Connection) -> str | None:
        active = self._active.get(connection.connection_id)
        if active is not None and not active.state.is_terminal:
            return f"active_command:{active.command_id}"
        if connection.state is ConnectionState.BUSY:
            return "connection_busy_unattributed"
        return None

    def _is_authorized(self, request: CommandRequest, policy: CommandKindPolicy) -> bool:
        try:
            return self._authorizer.is_authorized(request.requested_by, policy, request.ref) is True
        except Exception:
            return False  # fail closed

    def _release(self, record: CommandRecord, now) -> None:
        """Free the exclusive slot of a terminal Command and leave ``busy`` if still in it."""
        if not record.state.is_terminal:
            return
        if self._active.get(record.connection_id) is not record:
            return
        del self._active[record.connection_id]
        connection = self._runtime.get_connection(record.connection_id)
        if connection is not None and connection.state is ConnectionState.BUSY:
            connection.apply_busy(BusyEvent.BUSY_LEFT_READY, now, record.state.value, BUSY_CONTROL)
