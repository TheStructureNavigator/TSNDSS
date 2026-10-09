"""Shared deterministic fixtures for DB-01 device runtime tests."""

from __future__ import annotations

from tsn_dss.engine.device_runtime import (
    ManualClock,
    ProviderRegistry,
    ProviderRuntime,
    SequentialIdGenerator,
    SimulatedDeviceSpec,
    SimulatorProvider,
)


def make_provider(**kwargs) -> SimulatorProvider:
    kwargs.setdefault("clock", ManualClock())
    return SimulatorProvider(**kwargs)


def make_runtime(provider: SimulatorProvider | None = None) -> tuple[ProviderRuntime, SimulatorProvider]:
    provider = provider or make_provider()
    runtime = ProviderRuntime(provider, clock=ManualClock(), id_generator=SequentialIdGenerator())
    return runtime, provider


def make_registry() -> ProviderRegistry:
    return ProviderRegistry(clock=ManualClock(), id_generator=SequentialIdGenerator())


def connected_runtime():
    """Runtime with a discovered device and a connected Connection."""
    runtime, provider = make_runtime()
    device = runtime.discover().devices[0]
    connection = runtime.connect(runtime.open_connection(device))
    return runtime, provider, connection


def two_devices() -> tuple[SimulatedDeviceSpec, ...]:
    return (SimulatedDeviceSpec("sim-a"), SimulatedDeviceSpec("sim-b"))
