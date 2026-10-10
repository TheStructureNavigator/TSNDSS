"""Command admission, submission and outcomes (DSS-CTR-013 sections 6, 9, 10 and 11; DB-04 S2-S4).

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

Submission (S4) goes through ``submit`` only; no other code calls a Provider's command methods.
``submit`` re-runs the safety gate immediately before the Provider call, marks the possible-submission
boundary (``submitted``) *before* calling, and never retries: a Command is submitted at most once,
whatever happens next. Outcomes:

* the Provider accepts: ``acknowledged``. That is an acknowledgement, never success. ``succeeded`` is
  reached only through a registered ``EffectVerifier`` (fresh post-command evidence);
* the Provider refuses before accepting: ``failed`` if it states no physical effect was possible,
  otherwise ``unknown_result``;
* transport is lost, or a Provider call fails, after the boundary: ``unknown_result``;
* the deadline elapses after the boundary (``enforce_deadline``): ``timed_out`` only for a kind that
  cannot produce a physical effect, otherwise ``unknown_result``. The S2 ``check_deadline`` timeout
  applies to ``validated`` Commands only;
* cancellation: ``cancelled`` only on Provider-supplied no-effect evidence, ``unknown_result`` on a race.

An ``unknown_result`` of a non-idempotent physical Command opens an entry in the in-process
``UncertaintyStore`` for that Provider and Device Reference and degrades the Connection instead of
returning it to ``ready``. The entry survives disconnect and reconnect and blocks every later
safety-sensitive Command on that device whatever the Connection state is: ``ready`` is never proof of
physical safety, and a ``degraded`` Connection with an unresolved entry only returns to ``connected``
on fresh evidence, not to ``ready``. An entry is resolved only by ``resolve_uncertainty_by_recovery``
(fresh, kind-specific recovery evidence) or ``clear_uncertainty_by_operator`` (an authorized operator
clearance, which is not proof of any physical effect). The Command record stays ``unknown_result``.
A restarted process starts with an empty store, which proves nothing (REQ-050).

One executor is assumed to own the Commands of a Connection. Several executors over the same runtime
are not arbitrated (an unattributed ``busy`` is only detected, as a conflict); there is no global
arbitration.

``busy`` is entered when a state-changing Command is admitted (reaches ``validated`` and wins the
slot) and left when that Command reaches a terminal outcome. The contract words the entry as
"State-changing Command begins"; admission is the earliest point at which exclusivity can be made
atomic, and the owner decision keeps the reservation there (not at submission).
"""

from __future__ import annotations

import threading
from typing import Callable, Protocol, runtime_checkable

from .command_lifecycle import CommandEvent, CommandRecord
from .command_effects import EffectVerdictKind, EffectVerifier
from .command_models import (
    FreshnessRequirement,
    CommandKindPolicy,
    CommandKindRegistry,
    CommandPolicyError,
    CommandRequest,
    UnknownCommandKind,
)
from .connection import Connection
from .errors import (
    DeviceRuntimeError,
    InvalidTransition,
    ProviderCommandRejected,
    ProviderConnectionError,
)
from .lifecycle import BUSY_CONTROL, BusyEvent
from .models import CommandId, CommandRef, CommandState, ConnectionState
from .provider import CommandCapableProvider, ProviderCancelOutcome, ProviderCommandStatus
from .runtime import ProviderRuntime
from .safety_gates import EvidenceSnapshot, UncertaintyState, UncertaintyView, evaluate_gate
from .uncertainty import (
    BaselineRecovery,
    RecoveryAssessor,
    Resolution,
    ResolutionKind,
    UncertaintyEntry,
    UncertaintyStore,
)
from .support import Clock, IdGenerator, random_id_generator, utc_now

__all__ = ["ClearanceAuthorizer", "ClearanceNotAuthorized", "CommandAuthorizer", "CommandExecutor", "CommandIntent"]


class ClearanceNotAuthorized(DeviceRuntimeError):
    """An operator clearance was refused: not authorized, or no authorizer is configured."""


class RecoveryNotEstablished(DeviceRuntimeError):
    """Recovery evidence did not establish a resolution; the entry is unchanged."""

_ESTABLISHED = (UncertaintyState.RESOLVED_BY_RECOVERY_EVIDENCE, UncertaintyState.CLEARED_BY_OPERATOR)

_ADMISSIBLE_TARGETS = (
    ConnectionState.CONNECTED,
    ConnectionState.READY,
    ConnectionState.BUSY,
    ConnectionState.DEGRADED,
)


@runtime_checkable
class ClearanceAuthorizer(Protocol):
    """Authorizes an operator to clear an unresolved uncertainty. There is no default: without one, no clearance."""

    def may_clear(self, operator_id: str, provider_id: str, device_ref: str) -> bool: ...


@runtime_checkable
class CommandAuthorizer(Protocol):
    """Explicit authorization (REQ-021). There is no default authorizer: the executor needs one."""

    def is_authorized(self, requested_by: str, kind: CommandKindPolicy, ref: CommandRef) -> bool: ...


class _CombinedView:
    """The uncertainty view the gate sees: the store, then the explicitly configured view.

    Anything but a pass state from either blocks. A missing configured view blocks (the gate gets ``None``).
    """

    def __init__(self, store: UncertaintyStore, configured: UncertaintyView) -> None:
        self._store = store
        self._configured = configured

    def state_for(self, provider_id, device_ref):
        own = self._store.state_for(provider_id, device_ref)
        if own not in _ESTABLISHED:
            return own  # unresolved, unknown or nothing established: blocks
        theirs = self._configured.state_for(provider_id, device_ref)
        if theirs not in _ESTABLISHED:
            return theirs
        return own  # the store holds the history; it says how it was established


class _NoUncertaintyAssumed:
    """Used only to isolate the freshness part of ``evaluate_gate`` when judging recovery evidence, which
    must not be gated by the very uncertainty it is meant to resolve. Never passed to a real admission."""

    def state_for(self, provider_id, device_ref):
        return UncertaintyState.RESOLVED_BY_RECOVERY_EVIDENCE


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
        command_provider: CommandCapableProvider | None = None,
        verifiers: dict[str, EffectVerifier] | None = None,
        recovery_assessors: dict[str, RecoveryAssessor] | None = None,
        clearance_authorizer: ClearanceAuthorizer | None = None,
        baseline_recovery: BaselineRecovery | None = None,
    ) -> None:
        if not isinstance(authorizer, CommandAuthorizer):
            raise DeviceRuntimeError("an explicit CommandAuthorizer is required.")
        if command_provider is not None and (
            not isinstance(command_provider, CommandCapableProvider)
            or command_provider.descriptor.provider_id != runtime.provider_id
        ):
            raise DeviceRuntimeError("command_provider must be the command-capable Provider of this runtime.")
        self._runtime = runtime
        self._kinds = kinds
        self._authorizer = authorizer
        self._clock = clock
        self._ids = id_generator
        self._evidence_source = evidence_source or self._runtime_evidence
        self._uncertainty = uncertainty
        self._command_provider = command_provider
        self._verifiers = dict(verifiers or {})
        self._store = uncertainty if isinstance(uncertainty, UncertaintyStore) else UncertaintyStore()
        self._assessors = dict(recovery_assessors or {})
        self._clearance_authorizer = clearance_authorizer
        self._baseline_recovery = baseline_recovery
        self._lock = threading.RLock()
        runtime.add_transport_loss_observer(self._on_transport_loss)
        runtime.add_unresolved_condition_check(
            lambda connection: (runtime.provider_id, connection.device.device_ref) in self._store.unresolved_devices()
        )
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

    @property
    def uncertainty_store(self) -> UncertaintyStore:
        return self._store

    def unresolved_devices(self) -> frozenset[tuple[str, str]]:
        """(provider_id, device_ref) pairs with an unresolved physical uncertainty."""
        return self._store.unresolved_devices()

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
            self._block_if_gate_fails(record, connection, policy)
        return record

    def _block_if_gate_fails(self, record: CommandRecord, connection: Connection, policy: CommandKindPolicy) -> bool:
        result = self._evaluate_gate(connection, policy)
        if result.passed:
            return False
        event = (
            CommandEvent.UNCERTAINTY_NOT_CLEARED
            if result.blocked_by_uncertainty
            else CommandEvent.SAFETY_EVIDENCE_INSUFFICIENT
        )
        at = self._clock()
        record.apply(event, at, result.summary())
        self._settle(record, at)
        return True

    # --- submission and outcomes (S4) -----------------------------------------

    def submit(self, command_id: CommandId) -> CommandRecord:
        """Submit an admitted (``validated``) Command to the Provider, at most once, never retried."""
        with self._lock:
            record = self._records[command_id]
            if record.state is not CommandState.VALIDATED:
                raise InvalidTransition("command", record.state.value, "submit")
            if self._command_provider is None:
                raise DeviceRuntimeError("no command-capable Provider is configured.")
            request, policy = self._requests[command_id], record.policy
            now = self._clock()
            if request.deadline is not None and request.deadline <= now:
                record.apply(CommandEvent.DEADLINE_BEFORE_SUBMISSION, now, "deadline elapsed before submission; nothing submitted")
                self._settle(record, now)
                return record
            connection = self._runtime.get_connection(record.connection_id)
            if policy.state_changing and (connection is None or connection.state is not ConnectionState.BUSY):
                state = "unknown" if connection is None else connection.state.value
                record.apply(CommandEvent.SAFETY_EVIDENCE_INSUFFICIENT, now, f"connection_not_busy:{state}")
                self._settle(record, now)
                return record
            if policy.safety_sensitive and self._block_if_gate_fails(record, connection, policy):
                return record
            at = self._clock()
            record.apply(CommandEvent.GATES_PASSED, at, "possible-submission boundary: provider call follows")
            try:
                self._command_provider.submit_command(
                    record.connection_id, command_id, request.kind_id, request.idempotency_key
                )
            except ProviderCommandRejected as exc:
                event = (
                    CommandEvent.PROVIDER_REJECTED_NO_EFFECT
                    if exc.effect_possible is False
                    else CommandEvent.PROVIDER_REJECTED_EFFECT_POSSIBLE
                )
                self._finish(record, event, f"provider rejection:{exc.category}")
            except Exception as exc:  # transport or any other failure after the boundary: effect unknown
                self._lost_transport(record, connection, f"submit failed:{type(exc).__name__}")
            else:
                record.apply(CommandEvent.PROVIDER_ACKNOWLEDGED, self._clock(), "provider acknowledgement (not success)")
            return record

    def poll(self, command_id: CommandId) -> CommandRecord:
        """Ask the Provider for status of an ``acknowledged``/``in_progress`` Command and progress it."""
        with self._lock:
            record = self._records[command_id]
            if record.state not in (CommandState.ACKNOWLEDGED, CommandState.IN_PROGRESS):
                raise InvalidTransition("command", record.state.value, "poll")
            connection = self._runtime.get_connection(record.connection_id)
            try:
                report = self._command_provider.poll_command(record.connection_id, command_id)
            except Exception as exc:
                self._lost_transport(record, connection, f"poll failed:{type(exc).__name__}")
                return record
            status = report.status
            if status is ProviderCommandStatus.IN_PROGRESS and record.state is CommandState.ACKNOWLEDGED:
                record.apply(CommandEvent.MONITORING_STARTED, self._clock(), "provider reports in progress")
            elif status is ProviderCommandStatus.REPORTED_COMPLETE:
                self._verify(record, connection)
            elif status is ProviderCommandStatus.REPORTED_FAILED:
                # DSS-CTR-013 amendment A1: a Provider-reported failure is `failed` only when no physical effect
                # was possible (a kind that cannot produce one, or the Provider's explicit `effect_possible=False`,
                # which is provider-reported and relied on only in that direction). Anything else is unknown_result
                # at once, not at the deadline.
                if record.policy.physical is False or report.effect_possible is False:
                    self._finish(record, CommandEvent.EFFECT_FAILED, f"provider reports failure, no physical effect possible:{report.detail}")
                else:
                    self._finish(
                        record, CommandEvent.PROVIDER_FAILURE_EFFECT_POSSIBLE,
                        f"provider reports failure, physical effect may have occurred:{report.detail}",
                    )
            return record

    def _verify(self, record: CommandRecord, connection) -> None:
        verifier = self._verifiers.get(record.policy.kind_id)
        if verifier is None:
            return  # provider completion alone is not a verified effect
        try:
            verdict = verifier(connection, record)
        except Exception:
            return
        evidence = verdict.evidence or "effect verification"
        if verdict.kind is EffectVerdictKind.VERIFIED:
            self._finish(record, CommandEvent.EFFECT_VERIFIED, evidence)
        elif (
            verdict.kind is EffectVerdictKind.VERIFIED_BY_ACKNOWLEDGEMENT
            and record.state is CommandState.ACKNOWLEDGED
            and not record.policy.physical
        ):
            self._finish(record, CommandEvent.ACKNOWLEDGEMENT_IS_VERIFIED_EFFECT, evidence)
        elif verdict.kind is EffectVerdictKind.FAILED:
            self._finish(record, CommandEvent.EFFECT_FAILED, evidence)

    def cancel(self, command_id: CommandId) -> CommandRecord:
        """Ask the Provider to cancel a submitted Command. Cancellation never proves the effect away."""
        with self._lock:
            record = self._records[command_id]
            if record.state not in (CommandState.SUBMITTED, CommandState.ACKNOWLEDGED, CommandState.IN_PROGRESS):
                raise InvalidTransition("command", record.state.value, "cancel")
            connection = self._runtime.get_connection(record.connection_id)
            try:
                result = self._command_provider.cancel_command(record.connection_id, command_id)
            except Exception as exc:
                self._lost_transport(record, connection, f"cancel failed:{type(exc).__name__}")
                return record
            if result.outcome is ProviderCancelOutcome.CANCELLED_NO_EFFECT and result.evidence.strip():
                self._finish(record, CommandEvent.CANCEL_ACCEPTED_NO_EFFECT, result.evidence)
            elif result.outcome is not ProviderCancelOutcome.REFUSED:
                self._finish(record, CommandEvent.CANCEL_RACE_UNDETERMINED, f"cancellation race:{result.outcome.value}")
            return record

    def enforce_deadline(self, command_id: CommandId) -> CommandRecord:
        """Apply an elapsed deadline to a Command after the possible-submission boundary.

        A kind that cannot produce a physical effect times out; any other kind becomes ``unknown_result``.
        A deadline alone never decides success or failure. ``validated`` Commands use ``check_deadline``.
        """
        with self._lock:
            record = self._records[command_id]
            request = self._requests.get(command_id)
            if record.state is CommandState.VALIDATED:
                return self._check_deadline(command_id)
            now = self._clock()
            if (
                record.state in (CommandState.SUBMITTED, CommandState.ACKNOWLEDGED, CommandState.IN_PROGRESS)
                and request is not None
                and request.deadline is not None
                and request.deadline <= now
            ):
                if record.policy.physical:
                    self._finish(record, CommandEvent.DEADLINE_EFFECT_UNDETERMINED, "deadline elapsed; effect undetermined")
                else:
                    self._finish(record, CommandEvent.DEADLINE_NON_PHYSICAL, "deadline elapsed; no physical effect possible")
            return record

    def _finish(self, record: CommandRecord, event: CommandEvent, evidence: str) -> None:
        at = self._clock()
        record.apply(event, at, evidence)
        self._settle(record, at)

    def _lost_transport(self, record: CommandRecord, connection, evidence: str) -> None:
        """A failed Provider call after the boundary. The Connection records its own loss (independently
        of the Command) and the loss observer classifies every open Command on it; whatever it did not
        reach is classified here."""
        if connection is not None and connection.state in (
            ConnectionState.BUSY, ConnectionState.READY, ConnectionState.CONNECTED, ConnectionState.DEGRADED
        ):
            self._runtime.report_transport_loss(connection, ConnectionState.DEGRADED)
        if not record.state.is_terminal:
            self._finish(record, CommandEvent.TRANSPORT_LOST_EFFECT_UNKNOWN, evidence)

    def _on_transport_loss(self, connection: Connection, resulting_state: ConnectionState) -> None:
        with self._lock:
            for record in tuple(self._records.values()):
                if record.connection_id != connection.connection_id or record.state.is_terminal:
                    continue
                if record.state is CommandState.VALIDATED:
                    self._finish(record, CommandEvent.SAFETY_EVIDENCE_INSUFFICIENT, "transport_lost_before_submission")
                elif record.state in (CommandState.SUBMITTED, CommandState.ACKNOWLEDGED, CommandState.IN_PROGRESS):
                    self._finish(record, CommandEvent.TRANSPORT_LOST_EFFECT_UNKNOWN, "transport lost; effect unknown")

    # --- resolving uncertainty (S5) -------------------------------------------

    def resolve_uncertainty_by_recovery(self, connection: Connection, command_id: CommandId) -> UncertaintyEntry:
        """Resolve the uncertainty left by ``command_id`` using fresh, read-only recovery evidence.

        ``connection`` is any active Connection of the same device (a reconnect is fine). The evidence must
        meet the origin kind's own freshness requirements and the kind's registered assessor must classify
        the observed state as resolved; otherwise ``RecoveryNotEstablished`` is raised and nothing changes.
        Nothing is sent to the Provider, the Command record is untouched, and the result is recorded as
        recovery evidence, never as proof of the Command's effect.
        """
        with self._lock:
            record = self._records.get(command_id)
            if record is None or not isinstance(connection, Connection):
                raise RecoveryNotEstablished("unknown command or connection")
            if self._runtime.get_connection(connection.connection_id) is not connection:
                raise RecoveryNotEstablished("connection_not_owned")
            if connection.state not in _ADMISSIBLE_TARGETS:
                raise RecoveryNotEstablished(f"connection_not_active:{connection.state.value}")
            entry = self._store.get(self._runtime.provider_id, connection.device.device_ref, command_id)
            if not entry.unresolved:
                raise RecoveryNotEstablished("already_resolved")
            assessor = self._assessors.get(entry.kind_id)
            if assessor is None:
                raise RecoveryNotEstablished("no_recovery_assessor_for_kind")
            try:
                snapshot = self._evidence_source(connection)
            except Exception:
                raise RecoveryNotEstablished("evidence_unavailable") from None
            now = self._clock()  # after the evidence, as for the gate
            fresh = evaluate_gate(
                self._freshness_only(self._kinds.get(entry.kind_id)),
                snapshot,
                now,
                provider_id=self._runtime.provider_id,
                connection_id=connection.connection_id,
                device_ref=connection.device.device_ref,
                uncertainty=_NoUncertaintyAssumed(),
            )
            if not fresh.passed:
                raise RecoveryNotEstablished(f"evidence_not_fresh:{fresh.summary()}")
            try:
                verdict = assessor(connection, record, snapshot, now)
            except Exception:
                raise RecoveryNotEstablished("assessor_failed") from None
            if not verdict.resolved:
                raise RecoveryNotEstablished("recovery_not_established")
            try:
                resolution = Resolution(
                    ResolutionKind.RECOVERY_EVIDENCE, now, verdict.evidence, basis=tuple(verdict.basis)
                )
            except ValueError as exc:
                raise RecoveryNotEstablished(str(exc)) from None
            return self._store.resolve(entry.provider_id, entry.device_ref, command_id, resolution)

    def establish_baseline_by_recovery(self, connection: Connection) -> Resolution:
        """Establish the history of a device this process has not seen before, from fresh read-only evidence.

        This is the initialization gate: until a device has a baseline (or a resolved uncertainty) its state is
        ``unknown`` and safety-sensitive Commands stay blocked. A configured ``BaselineRecovery`` supplies the
        freshness requirements and the assessor; without one this refuses. Nothing is sent to the Provider, and
        the result records what was accepted now, not what earlier Commands did.
        """
        with self._lock:
            baseline = self._baseline_recovery
            if baseline is None:
                raise RecoveryNotEstablished("no_baseline_recovery_configured")
            if not isinstance(connection, Connection) or self._runtime.get_connection(connection.connection_id) is not connection:
                raise RecoveryNotEstablished("connection_not_owned")
            if connection.state not in _ADMISSIBLE_TARGETS:
                raise RecoveryNotEstablished(f"connection_not_active:{connection.state.value}")
            provider_id, device_ref = self._runtime.provider_id, connection.device.device_ref
            if self._store.state_for(provider_id, device_ref) is UncertaintyState.UNRESOLVED:
                raise RecoveryNotEstablished("unresolved_uncertainty_present")
            if self._store.history_established(provider_id, device_ref):
                raise RecoveryNotEstablished("already_established")
            try:
                snapshot = self._evidence_source(connection)
            except Exception:
                raise RecoveryNotEstablished("evidence_unavailable") from None
            now = self._clock()
            fresh = evaluate_gate(
                CommandKindPolicy(
                    "baseline", state_changing=False, physical=False, idempotent=True,
                    safety_sensitive=True, freshness=baseline.freshness,
                ),
                snapshot, now,
                provider_id=provider_id, connection_id=connection.connection_id, device_ref=device_ref,
                uncertainty=_NoUncertaintyAssumed(),
            )
            if not fresh.passed:
                raise RecoveryNotEstablished(f"evidence_not_fresh:{fresh.summary()}")
            try:
                verdict = baseline.assessor(connection, None, snapshot, now)
            except Exception:
                raise RecoveryNotEstablished("assessor_failed") from None
            if not verdict.resolved:
                raise RecoveryNotEstablished("recovery_not_established")
            try:
                resolution = Resolution(ResolutionKind.RECOVERY_EVIDENCE, now, verdict.evidence, basis=tuple(verdict.basis))
            except ValueError as exc:
                raise RecoveryNotEstablished(str(exc)) from None
            return self._store.establish_baseline(provider_id, device_ref, resolution)

    def establish_baseline_by_operator(self, device_ref: str, operator_id: str, reason: str) -> Resolution:
        """Establish an unknown history by authorized operator clearance. Not proof of anything physical."""
        with self._lock:
            authorizer = self._clearance_authorizer
            if authorizer is None:
                raise ClearanceNotAuthorized("no clearance authorizer is configured")
            try:
                allowed = authorizer.may_clear(operator_id, self._runtime.provider_id, device_ref) is True
            except Exception:
                allowed = False
            if not allowed:
                raise ClearanceNotAuthorized("operator not authorized")
            if self._store.state_for(self._runtime.provider_id, device_ref) is UncertaintyState.UNRESOLVED:
                raise ClearanceNotAuthorized("unresolved uncertainty present; clear that entry instead")
            try:
                resolution = Resolution(ResolutionKind.OPERATOR_CLEARANCE, self._clock(), reason, resolved_by=operator_id)
                return self._store.establish_baseline(self._runtime.provider_id, device_ref, resolution)
            except ValueError as exc:
                raise ClearanceNotAuthorized(str(exc)) from None

    @staticmethod
    def _freshness_only(policy: CommandKindPolicy) -> CommandKindPolicy:
        """The kind's evidence requirements without their value constraints.

        Recovery looks at the state *after* a Command whose outcome is unknown, which legitimately differs from the
        preconditions the kind demands before running (DB-05 Slice 2). Freshness, availability and consistency still apply
        in full; judging whether the observed value is an acceptable place to start from is the kind's recovery assessor's job.
        """
        return CommandKindPolicy(
            policy.kind_id, state_changing=policy.state_changing, physical=policy.physical, idempotent=policy.idempotent,
            safety_sensitive=policy.safety_sensitive,
            freshness=tuple(FreshnessRequirement(r.item, r.max_age) for r in policy.freshness),
        )

    def clear_uncertainty_by_operator(
        self, device_ref: str, command_id: CommandId, operator_id: str, reason: str
    ) -> UncertaintyEntry:
        """Record an authorized operator clearance of the uncertainty left by ``command_id``.

        The clearance is a recorded decision by a named operator; it is not independent evidence about
        what the Command did, and it does not replace the fresh evidence later Commands need.
        """
        with self._lock:
            authorizer = self._clearance_authorizer
            if authorizer is None:
                raise ClearanceNotAuthorized("no clearance authorizer is configured")
            try:
                allowed = authorizer.may_clear(operator_id, self._runtime.provider_id, device_ref) is True
            except Exception:
                allowed = False
            if not allowed:
                raise ClearanceNotAuthorized("operator not authorized")
            entry = self._store.get(self._runtime.provider_id, device_ref, command_id)
            try:
                resolution = Resolution(ResolutionKind.OPERATOR_CLEARANCE, self._clock(), reason, resolved_by=operator_id)
            except ValueError as exc:
                raise ClearanceNotAuthorized(str(exc)) from None
            return self._store.resolve(entry.provider_id, entry.device_ref, command_id, resolution)

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
            self._settle(record, now)
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
            uncertainty=None if self._uncertainty is None else _CombinedView(self._store, self._uncertainty),
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

    def _settle(self, record: CommandRecord, now) -> None:
        """A terminal Command frees its slot; ``busy`` is left to ``ready`` or, when an unresolved
        physical uncertainty exists, ``degraded``. Safety is never implied by leaving ``busy``."""
        if not record.state.is_terminal:
            return
        policy = record.policy
        connection = self._runtime.get_connection(record.connection_id)
        uncertain = (
            record.state is CommandState.UNKNOWN_RESULT
            and policy is not None
            and policy.physical
            and not policy.idempotent
            and connection is not None
        )
        if uncertain:
            self._store.open_entry(
                record.provider_id, connection.device.device_ref, record.command_id, policy.kind_id,
                now, record.connection_id,
            )
        if self._active.get(record.connection_id) is not record:
            return
        del self._active[record.connection_id]
        if connection is not None and connection.state is ConnectionState.BUSY:
            # Only this Command's own uncertain outcome degrades the Connection (section 6). A Command that
            # was merely blocked by an existing uncertainty leaves the Connection as it found it.
            event = BusyEvent.BUSY_LEFT_DEGRADED if uncertain else BusyEvent.BUSY_LEFT_READY
            connection.apply_busy(event, now, record.state.value, BUSY_CONTROL)
