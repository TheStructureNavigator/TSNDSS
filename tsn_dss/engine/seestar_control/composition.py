"""Composition root: one Seestar device, one DB-04 executor per Connection (DB-05 Slice 2).

``SeestarControl`` wires the existing pieces and nothing else: the DB-02 read transport, the Slice 1 command provider and its
control transport, the DB-04 ``ProviderRuntime``, ``CommandExecutor`` and ``UncertaintyStore``, the four kind policies, the
effect verifiers and the recovery assessors. There is no second command lifecycle and no path around the executor: the command
provider and the control transport stay private to this object; callers get the Connection, the executor and the driver.

Nothing is granted by default. The command authorizer is required and injected; a clearance authorizer is optional and, when
absent, no operator clearance is possible. Freshness windows come from the integrator (``ControlFreshness``, no defaults) and so
do deadlines and poll intervals at the driver. A fresh process starts with an empty store, which means *unknown history*: no
safety-sensitive command is admitted until the device's baseline is established explicitly (``establish_baseline_by_*``).
Capability declarations (``implemented_untested``) are evidence that a command exists, never permission, freshness or safety.

The store and the id generator are shared across attachments so that uncertainty survives a reconnect and command ids never
repeat; only one executor may own the live Connection at a time.

Recovery continuity. Resolving an uncertainty by recovery needs the original command record, which lives in the executor that
submitted the command. When a Connection is replaced, ``SeestarControl`` keeps that executor privately, and only while it still
has an unresolved uncertainty, and ``ControlHandle.recover`` uses it with the NEW Connection's fresh telemetry. Nothing of the
old executor is exposed, nothing is recovered, retried or cleared automatically, and a reconnection or ``ready`` Connection is
never evidence of safety: the DB-04 gate, the kind's assessor and the explicit call decide. A handle that has been replaced is
stale: its executor and driver refuse every mutating call. All of this is in-process only; a restart loses it, and the
empty store then means unknown history (explicit baseline required).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from ..device_runtime import ConnectionState, Connection, ProviderRuntime
from ..device_runtime.command_executor import ClearanceAuthorizer, CommandAuthorizer, CommandExecutor, RecoveryNotEstablished
from ..device_runtime.command_models import CommandKindRegistry
from ..device_runtime.errors import DeviceRuntimeError
from ..device_runtime.support import Clock, IdGenerator, random_id_generator, utc_now
from ..device_runtime.uncertainty import UncertaintyEntry, UncertaintyStore
from ..seestar_provider.config import SeestarProviderConfig
from ..seestar_provider.transport import SeestarReadTransport
from .driver import CommandDriver
from .kinds import ControlFreshness, register_command_kinds
from .provider import SeestarCommandProvider
from .recovery import build_recovery_assessors, seestar_baseline_recovery
from .transport import SeestarControlTransport
from .verification import build_verifiers

__all__ = ["ControlAttachError", "ControlHandle", "PendingRecovery", "SeestarControl"]

_READ_ONLY = frozenset({"get", "commands", "active_command", "unresolved_devices", "uncertainty_store"})


class ControlAttachError(DeviceRuntimeError):
    """The Connection or its executor could not be attached. Messages are fixed tokens."""


@dataclass(slots=True, frozen=True)
class PendingRecovery:
    """An unresolved uncertainty of the handle's device, as far as recovery is concerned. Carries no payload."""

    command_id: str
    kind_id: str
    created_at: object
    origin_connection_id: str
    record_available: bool  # the original command record is still held, so recovery by evidence is possible


class _GuardedExecutor:
    """The attachment's executor, usable only while its handle is the current one.

    Reads stay available. Every other call (admit, submit, poll, cancel, deadlines, baseline, resolve, clear) raises
    ``stale_handle`` once the handle has been replaced, so a replaced attachment can never issue a command.
    """

    def __init__(self, executor: CommandExecutor, is_current: Callable[[], bool]) -> None:
        self._executor = executor
        self._is_current = is_current

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        target = getattr(self._executor, name)
        if name in _READ_ONLY or not callable(target):
            return target

        def guarded(*args, **kwargs):
            if not self._is_current():
                raise ControlAttachError("stale_handle")
            return target(*args, **kwargs)

        return guarded

    def __repr__(self) -> str:
        return "GuardedExecutor(<redacted>)"


class ControlHandle:
    """The live Connection with its single executor, a driver over it, and the recovery entry points."""

    __slots__ = ("connection", "executor", "driver", "_control")

    def __init__(self, connection: Connection, executor, driver: CommandDriver, control: "SeestarControl") -> None:
        self.connection, self.executor, self.driver, self._control = connection, executor, driver, control

    def __repr__(self) -> str:
        return "ControlHandle(<redacted>)"

    def pending_recoveries(self) -> tuple[PendingRecovery, ...]:
        """Unresolved uncertainties of this handle's device. Reading them changes nothing."""
        return self._control._pending(self)

    def recover(self, command_id: str) -> UncertaintyEntry:
        """Resolve the uncertainty left by ``command_id`` using fresh telemetry from THIS handle's Connection.

        An explicit call, never automatic. It sends nothing, leaves the command's ``unknown_result`` history untouched, and
        authorizes nothing. Raises ``stale_handle`` for a replaced handle, ``UnknownUncertainty`` if the command's uncertainty
        belongs to another device or does not exist, and ``RecoveryNotEstablished`` otherwise (missing original record,
        stale or ambiguous evidence, already resolved).
        """
        return self._control._recover(self, command_id)


class SeestarControl:
    def __init__(
        self,
        *,
        config: SeestarProviderConfig,
        read_transport: SeestarReadTransport,
        control_transport: SeestarControlTransport,
        freshness: ControlFreshness,
        authorizer: CommandAuthorizer,
        clearance_authorizer: ClearanceAuthorizer | None = None,
        uncertainty_store: UncertaintyStore | None = None,
        clock: Clock = utc_now,
        id_generator: IdGenerator = random_id_generator,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if authorizer is None or not isinstance(authorizer, CommandAuthorizer):
            raise ControlAttachError("command_authorizer_required")
        self._freshness = freshness
        self._authorizer = authorizer
        self._clearance_authorizer = clearance_authorizer
        self._store = uncertainty_store if uncertainty_store is not None else UncertaintyStore()
        self._clock, self._ids, self._sleep = clock, id_generator, sleep
        self._provider = SeestarCommandProvider(config, read_transport, control_transport, clock=clock)  # private: no bypass
        self._runtime = ProviderRuntime(self._provider, clock=clock, id_generator=id_generator)
        self._registry = CommandKindRegistry()
        register_command_kinds(self._registry, freshness)
        self._lock = threading.Lock()
        self._handle: ControlHandle | None = None
        self._raw: CommandExecutor | None = None  # the current attachment's executor, unwrapped
        self._retained: list[CommandExecutor] = []  # replaced executors that still own an unresolved uncertainty

    def __repr__(self) -> str:
        return "SeestarControl(<redacted>)"

    @property
    def runtime(self) -> ProviderRuntime:
        """The vendor-neutral runtime: reads and Connection lifecycle only; it has no command surface."""
        return self._runtime

    @property
    def uncertainty_store(self) -> UncertaintyStore:
        return self._store

    def attach(self) -> ControlHandle:
        """Discover the one device, open and prepare its Connection, and give it its one executor."""
        with self._lock:
            current = self._handle
            if current is not None and current.connection.state not in (ConnectionState.DISCONNECTED, ConnectionState.FAILED):
                raise ControlAttachError("executor_already_attached")
            devices = self._runtime.discover().devices
            if len(devices) != 1:
                raise ControlAttachError("expected_exactly_one_device")
            connection = self._runtime.connect(self._runtime.open_connection(devices[0]))
            if connection.state is not ConnectionState.CONNECTED:
                raise ControlAttachError("connection_failed")
            if self._runtime.refresh_evidence(connection) is not ConnectionState.READY:
                self._runtime.disconnect(connection)
                raise ControlAttachError("evidence_not_ready")
            self._retire_current()
            executor = CommandExecutor(
                self._runtime,
                self._registry,
                self._authorizer,
                clock=self._clock,
                id_generator=self._ids,
                uncertainty=self._store,
                command_provider=self._provider,
                verifiers=build_verifiers(self._runtime, self._freshness, self._clock),
                recovery_assessors=build_recovery_assessors(),
                clearance_authorizer=self._clearance_authorizer,
                baseline_recovery=seestar_baseline_recovery(self._freshness),
            )
            holder: list[ControlHandle] = []
            guarded = _GuardedExecutor(executor, lambda: bool(holder) and self._handle is holder[0])
            handle = ControlHandle(connection, guarded, CommandDriver(guarded, clock=self._clock, sleep=self._sleep), self)
            holder.append(handle)
            self._raw, self._handle = executor, handle
            return handle

    # --- recovery continuity (minimal in-process context) ------------------------------------

    def _open_command_ids(self) -> set[str]:
        return {
            entry.command_id
            for key in self._store.unresolved_devices()
            for entry in self._store.entries(*key)
            if entry.unresolved
        }

    def _retire_current(self) -> None:
        """Keep the executor being replaced only if it still owns an unresolved uncertainty; drop the others."""
        if self._raw is not None:
            self._retained.append(self._raw)
            self._raw = None
        self._prune()

    def _prune(self) -> None:
        open_ids = self._open_command_ids()
        self._retained = [ex for ex in self._retained if any(r.command_id in open_ids for r in ex.commands)]

    def _owner_of(self, command_id: str) -> CommandExecutor | None:
        for executor in ([self._raw] if self._raw is not None else []) + self._retained:
            if executor.get(command_id) is not None:
                return executor
        return None

    def _require_current(self, handle: ControlHandle) -> None:
        if self._handle is not handle:
            raise ControlAttachError("stale_handle")

    def _pending(self, handle: ControlHandle) -> tuple[PendingRecovery, ...]:
        with self._lock:
            self._require_current(handle)
            entries = self._store.entries(self._runtime.provider_id, handle.connection.device.device_ref)
            return tuple(
                PendingRecovery(e.command_id, e.kind_id, e.created_at, e.origin_connection_id, self._owner_of(e.command_id) is not None)
                for e in entries
                if e.unresolved
            )

    def _recover(self, handle: ControlHandle, command_id: str) -> UncertaintyEntry:
        with self._lock:
            self._require_current(handle)
            # the uncertainty must belong to this handle's own device; anything else is UnknownUncertainty
            entry = self._store.get(self._runtime.provider_id, handle.connection.device.device_ref, command_id)
            if not entry.unresolved:
                raise RecoveryNotEstablished("already_resolved")
            owner = self._owner_of(command_id)
            if owner is None:
                raise RecoveryNotEstablished("original_record_missing")
            resolved = owner.resolve_uncertainty_by_recovery(handle.connection, command_id)
            self._prune()
            return resolved
