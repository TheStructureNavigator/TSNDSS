"""Provider-neutral device runtime (DSS-CTR-013, ROADMAP_DEVICE_BACKEND.md DB-01).

Standard-library only. This package is runtime-only: it has no database access,
no Command execution and no vendor-specific Provider behavior.
"""

import sys as _sys

from .connection import Connection
from .errors import (
    ConnectionNotActive,
    ContractViolation,
    DeviceRuntimeError,
    InvalidTransition,
    ProviderAlreadyRegistered,
    ProviderConnectionError,
    ProviderDiscoveryError,
    ProviderError,
    ProviderNotUsable,
    UnknownProvider,
)
from .lifecycle import (
    CONNECTION_TRANSITIONS,
    PROVIDER_TRANSITIONS,
    ConnectionEvent,
    ConnectionTransition,
    ProviderCall,
    ProviderEvent,
    TransitionRecord,
    connection_next,
    provider_next_state,
)
from .models import (
    COMMAND_TERMINAL_STATES,
    CapabilityConfirmation,
    CapabilityEntry,
    CapabilityReport,
    CommandId,
    CommandRef,
    CommandState,
    ConfigurationStatus,
    ConnectionId,
    ConnectionState,
    DeviceReference,
    DiscoveryOutcome,
    DiscoveryResult,
    EvidenceFreshness,
    PreviewAvailability,
    PreviewDescriptor,
    PreviewId,
    ProviderDescriptor,
    ProviderId,
    ProviderLifecycleState,
    TelemetryItem,
    TelemetrySample,
    TelemetrySource,
    ValueState,
)
from .provider import DeviceProvider, ProviderPreviewReading, ProviderTelemetryReading
from .registry import ProviderRegistry
from .runtime import ProviderRuntime
from .simulator import SimulatedDeviceSpec, SimulatorProvider
from .support import ManualClock, SequentialIdGenerator, random_id_generator, utc_now

__all__ = [
    name
    for name, value in list(globals().items())
    if not name.startswith("_") and not isinstance(value, type(_sys))
]
