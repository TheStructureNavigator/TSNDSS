"""Seestar command Provider: ``CommandCapableProvider`` on top of the unchanged read-only DB-02 provider (DB-05 Slice 1).

``SeestarCommandProvider`` extends ``SeestarProvider`` (which is not modified) and satisfies the DB-04 command protocol:
``submit_command``, ``poll_command``, ``cancel_command``. Only the DB-04 ``CommandExecutor`` is meant to call them; this
module offers no other way to send a command and no way to pass a method name or parameters.

Provider trust model for ``effect_possible`` (DSS-CTR-013 amendment A1):

* ``ProviderCommandRejected(effect_possible=False)`` is raised ONLY for failures that are structurally before the command frame
  is handed to the socket: unknown kind, a key argument, a repeated command id, an unknown or lost local session, a failed fresh
  identity check, or a ``ControlPreSendError`` (connect or any handshake failure). Nothing was sent.
* Everything else keeps the effect possible: a device reply with a non-zero code or an unexpected result
  (``effect_possible=True``), and any failure from the moment the frame may have started to be written, which surfaces as a
  ``ProviderConnectionError`` (transport lost, effect unknown). An RPC error, a timeout, a closed connection or an ambiguous
  reply never produces ``False``.

Polling reports what the telemetry shows and nothing more: ``ACKNOWLEDGED`` (no change visible), ``IN_PROGRESS``,
``REPORTED_COMPLETE`` (the target state is visible). It never reports a failure, and a report is not verified physical
success (that is the executor's ``EffectVerifier`` job, a later slice). Cancellation is refused: no cancellation semantics for
these commands have been verified on the device, and a refusal contacts nothing.
"""

from __future__ import annotations

from dataclasses import replace

from ..device_runtime import (
    CapabilityConfirmation,
    CapabilityEntry,
    CommandId,
    ConnectionId,
    ProviderConnectionError,
    TelemetrySample,
)
from ..device_runtime.errors import ProviderCommandRejected
from ..device_runtime.provider import (
    ProviderCancelOutcome,
    ProviderCancelResult,
    ProviderCommandReceipt,
    ProviderCommandReport,
    ProviderCommandStatus,
)
from ..device_runtime.support import Clock, utc_now
from ..seestar_provider.config import SeestarProviderConfig
from ..seestar_provider.errors import SeestarError
from ..seestar_provider.provider import SeestarProvider
from ..seestar_provider.transport import SeestarReadTransport
from . import commands as _commands
from .commands import ARM_DEPLOY, ARM_PARK, COMMANDS, GOTO, SCENERY_START, SCENERY_STOP, GotoTarget, MountCoordinates
from .errors import ControlPostSendError, ControlPreSendError
from .goto_watch import GotoCompletion, GotoWatch
from .states import CAMERA_ITEMS, arm_closed, arm_stationary, cameras_ready, cameras_stopped_count
from .transport import SeestarControlTransport

IMPLEMENTATION_LABEL = "tsn-dss-seestar-control/1"
_REPLACED_CAPABILITIES = ("mount.motion", "camera.mode_control")  # superseded by the four specific entries below

__all__ = ["SeestarCommandProvider"]


class SeestarCommandProvider(SeestarProvider):
    def __init__(
        self,
        config: SeestarProviderConfig,
        read_transport: SeestarReadTransport,
        control_transport: SeestarControlTransport,
        *,
        clock: Clock = utc_now,
    ) -> None:
        super().__init__(config, read_transport, clock=clock)
        self._control = control_transport
        self._submitted: dict[CommandId, tuple[ConnectionId, str]] = {}
        self._goto_watches: dict[CommandId, tuple[ConnectionId, GotoWatch]] = {}  # the issuing connections of GoTos still awaiting their end
        self._goto_results: dict[CommandId, GotoCompletion] = {}
        self._descriptor = replace(self._descriptor, implementation_label=IMPLEMENTATION_LABEL)

    def __repr__(self) -> str:
        return f"SeestarCommandProvider(provider_id={self._config.provider_id!r}, <redacted>)"

    # --- capabilities ---------------------------------------------------------------

    def describe_capabilities(self, connection_id):
        entries = [e for e in super().describe_capabilities(connection_id) if e.name not in _REPLACED_CAPABILITIES]
        available = None if connection_id is None else True  # super() verified the endpoint's identity just now
        entries += [
            CapabilityEntry(kind_id, True, available, True, CapabilityConfirmation.IMPLEMENTED_UNTESTED, False)
            for kind_id in (*COMMANDS, GOTO)
        ]
        return tuple(entries)

    # --- submission -------------------------------------------------------------------

    def submit_command(self, connection_id, command_id, kind_id, idempotency_key, parameters=None) -> ProviderCommandReceipt:
        with self._lock:
            self._refuse_before_sending(connection_id, command_id, kind_id, idempotency_key, parameters)
            host, device_ref = self._host_for(connection_id), self._sessions[connection_id]
            try:
                self._verify_identity(host, device_ref)  # a different device at the endpoint must never receive the command
            except SeestarError as exc:
                raise ProviderCommandRejected(f"identity_check_failed:{exc.category}", effect_possible=False) from None
            self._submitted[command_id] = (connection_id, kind_id)  # recorded before sending: never submitted twice
            try:
                watch = None
                if kind_id == GOTO:
                    reply, watch = self._control.open_goto(host, parameters)
                else:
                    reply = self._control.send_command(host, kind_id)
            except ControlPreSendError as exc:
                raise ProviderCommandRejected(f"not_sent:{exc.category}", effect_possible=False) from None
            except ControlPostSendError as exc:
                raise ProviderConnectionError(exc.category) from None
            except Exception:
                raise ProviderConnectionError("control_failed") from None  # untyped: cannot prove the frame was not sent
            if reply.code != 0:
                if watch is not None:
                    watch.close()
                raise ProviderCommandRejected("device_error_reply", effect_possible=True)
            if kind_id == GOTO:  # the reply to a GoTo is an acknowledgement at most; its end is the ScopeGoto event on this connection
                self._goto_watches[command_id] = (connection_id, watch)
                return ProviderCommandReceipt(accepted_at=self._clock())
            command = COMMANDS[kind_id]
            if command.result_must_be_zero and (reply.result != 0 or isinstance(reply.result, bool)):
                raise ProviderCommandRejected("unexpected_result", effect_possible=True)
            return ProviderCommandReceipt(accepted_at=self._clock())

    def _refuse_before_sending(self, connection_id, command_id, kind_id, idempotency_key, parameters=None) -> None:
        def refuse(category: str) -> None:
            raise ProviderCommandRejected(category, effect_possible=False)

        if kind_id == GOTO:
            if _commands.GOTO_PHYSICAL_ENABLED is not True:
                refuse("goto_physical_blocked")  # BLOCKED pending owner decisions; nothing is sent
            if not isinstance(parameters, GotoTarget):
                refuse("goto_target_required")
        elif kind_id not in COMMANDS:
            refuse("kind_not_supported")
        elif parameters is not None:
            refuse("parameters_not_supported")
        if idempotency_key is not None:
            refuse("idempotency_key_not_supported")
        if command_id in self._submitted:
            refuse("duplicate_command")
        if connection_id not in self._sessions:
            refuse("not_connected")

    # --- progress ----------------------------------------------------------------------

    def poll_command(self, connection_id, command_id) -> ProviderCommandReport:
        with self._lock:
            known = self._submitted.get(command_id)
            if known is None or known[0] != connection_id:
                raise ProviderConnectionError("unknown_command")
            if known[1] == GOTO:
                return self._poll_goto(command_id)
            reading = self.read_telemetry(connection_id)  # raises ProviderConnectionError when the device cannot be read
            sample = TelemetrySample(
                provider_id=self._descriptor.provider_id,
                connection_id=connection_id,
                host_observed_at=self._clock(),
                items=reading.items,
                simulated=False,
            )
            return ProviderCommandReport(_status(known[1], sample), detail="telemetry")

    # --- evidence for GoTo verification ------------------------------------------------------------------

    def read_mount_coordinates(self, connection_id) -> MountCoordinates | None:
        """One passive read of the device's reported pointing, or ``None`` if it cannot be read or is not two finite numbers.
        Nothing is converted and nothing is assumed about the frame."""
        with self._lock:
            host = self._host_for(connection_id)
            reader = getattr(self._transport, "read_equ_coord", None)
            if reader is None:
                return None
            try:
                reply = reader(host)
            except SeestarError:
                return None
            result = reply.result
            if reply.code != 0 or not isinstance(result, dict):
                return None
            ra, dec = result.get("ra"), result.get("dec")
            for value in (ra, dec):
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or value in (float("inf"), float("-inf")):
                    return None
            return MountCoordinates(float(ra), float(dec), self._clock())

    def _poll_goto(self, command_id) -> ProviderCommandReport:
        """What the issuing connection has shown. Only an outer ScopeGoto ``complete`` is a completion report (still to be verified by the
        executor's verifier); ``fail``/``cancel`` is a failure with a possible physical effect (-> unknown_result, amendment A1); a lost
        connection raises, which the executor also turns into unknown_result."""
        entry = self._goto_watches.get(command_id)
        if entry is not None:
            state = entry[1].pump()
            if state == "lost":
                self._goto_watches.pop(command_id, None)
                raise ProviderConnectionError("goto_connection_lost")
            if state in ("complete", "fail", "cancel"):
                self._goto_watches.pop(command_id, None)
                self._goto_results[command_id] = entry[1].completion
            else:
                done = ProviderCommandStatus.IN_PROGRESS if entry[1].progress_seen else ProviderCommandStatus.ACKNOWLEDGED
                return ProviderCommandReport(done, detail="awaiting ScopeGoto")
        result = self._goto_results.get(command_id)
        if result is None:
            raise ProviderConnectionError("goto_watch_missing")
        if result.state == "complete":
            return ProviderCommandReport(ProviderCommandStatus.REPORTED_COMPLETE, detail="ScopeGoto complete")
        return ProviderCommandReport(ProviderCommandStatus.REPORTED_FAILED, effect_possible=True, detail=f"ScopeGoto {result.state}")

    def goto_completion(self, command_id) -> GotoCompletion | None:
        """The firmware-reported end of this GoTo as seen on its issuing connection, or ``None`` (not ended, or never seen)."""
        with self._lock:
            return self._goto_results.get(command_id)

    def disconnect(self, connection_id) -> None:
        with self._lock:
            for command_id, (owner, watch) in list(self._goto_watches.items()):
                if owner == connection_id:
                    watch.close()
                    self._goto_watches.pop(command_id, None)
        super().disconnect(connection_id)

    # --- cancellation ----------------------------------------------------------------------

    def cancel_command(self, connection_id, command_id) -> ProviderCancelResult:
        """Refused. Device-specific cancellation semantics are unverified; nothing is sent."""
        return ProviderCancelResult(ProviderCancelOutcome.REFUSED, "")


def _status(kind_id: str, sample: TelemetrySample) -> ProviderCommandStatus:
    """What the telemetry shows for ``kind_id``. Unknown or missing values never claim progress or completion."""
    ack, progress, done = (ProviderCommandStatus.ACKNOWLEDGED, ProviderCommandStatus.IN_PROGRESS, ProviderCommandStatus.REPORTED_COMPLETE)
    if kind_id in (ARM_DEPLOY, ARM_PARK):
        stationary, closed = arm_stationary(sample), arm_closed(sample)
        if stationary is False:
            return progress
        target_closed = kind_id == ARM_PARK
        return done if stationary is True and closed is target_closed else ack
    if kind_id == SCENERY_START:
        if cameras_ready(sample):
            return done
        stopped = cameras_stopped_count(sample)
        return progress if stopped is not None and stopped < len(CAMERA_ITEMS) else ack
    if kind_id == GOTO:  # a stationary mount is only the cue to ask the verifier (it needs coordinates); it is not completion
        stationary = arm_stationary(sample)
        return progress if stationary is False else done if stationary is True else ack
    if kind_id == SCENERY_STOP:
        stopped = cameras_stopped_count(sample)
        if stopped is None:
            return ack
        return done if stopped == len(CAMERA_ITEMS) else progress if stopped > 0 else ack
    return ack
