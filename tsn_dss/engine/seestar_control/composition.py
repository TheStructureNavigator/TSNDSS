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
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

from ..device_runtime import ConnectionState, Connection, ProviderRuntime
from ..device_runtime.command_executor import ClearanceAuthorizer, CommandAuthorizer, CommandExecutor
from ..device_runtime.command_models import CommandKindRegistry
from ..device_runtime.errors import DeviceRuntimeError
from ..device_runtime.support import Clock, IdGenerator, random_id_generator, utc_now
from ..device_runtime.uncertainty import UncertaintyStore
from ..seestar_provider.config import SeestarProviderConfig
from ..seestar_provider.transport import SeestarReadTransport
from .driver import CommandDriver
from .kinds import ControlFreshness, register_command_kinds
from .provider import SeestarCommandProvider
from .recovery import build_recovery_assessors, seestar_baseline_recovery
from .transport import SeestarControlTransport
from .verification import build_verifiers

__all__ = ["ControlAttachError", "ControlHandle", "SeestarControl"]


class ControlAttachError(DeviceRuntimeError):
    """The Connection or its executor could not be attached. Messages are fixed tokens."""


@dataclass(slots=True, frozen=True)
class ControlHandle:
    """The live Connection with its single executor and a driver over that executor."""

    connection: Connection
    executor: CommandExecutor
    driver: CommandDriver


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
            self._handle = ControlHandle(connection, executor, CommandDriver(executor, clock=self._clock, sleep=self._sleep))
            return self._handle
