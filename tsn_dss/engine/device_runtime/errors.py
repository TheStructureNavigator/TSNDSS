"""Exceptions for the provider-neutral device runtime (DSS-CTR-013)."""

from __future__ import annotations


class DeviceRuntimeError(Exception):
    """Base class for device runtime errors."""


class InvalidTransition(DeviceRuntimeError):
    """A lifecycle event is not allowed from the current state."""

    def __init__(self, subject: str, state: str, event: str) -> None:
        super().__init__(f"{subject}: event '{event}' is not allowed from state '{state}'.")
        self.subject = subject
        self.state = state
        self.event = event


class ContractViolation(DeviceRuntimeError):
    """A Provider returned evidence that violates DSS-CTR-013 invariants."""


class ProviderAlreadyRegistered(DeviceRuntimeError):
    """A provider_id is already registered in this runtime."""


class UnknownProvider(DeviceRuntimeError):
    """No Provider is registered under the requested provider_id."""


class ProviderNotUsable(DeviceRuntimeError):
    """The Provider lifecycle state does not allow the requested operation."""


class ConnectionNotActive(DeviceRuntimeError):
    """A read was requested on a Connection that cannot serve reads."""


class ProviderError(DeviceRuntimeError):
    """Base class for failures reported by a Provider implementation."""

    def __init__(self, category: str, message: str = "") -> None:
        super().__init__(f"{category}: {message}" if message else category)
        self.category = category
        self.message = message


class ProviderDiscoveryError(ProviderError):
    """Discovery failed. ``fatal`` selects the fatal or nonfatal lifecycle path."""

    def __init__(self, category: str, message: str = "", *, fatal: bool = False) -> None:
        super().__init__(category, message)
        self.fatal = fatal


class ProviderConnectionError(ProviderError):
    """A connect, disconnect or evidence read failed at the Provider."""


class ProviderCommandRejected(DeviceRuntimeError):
    """A Provider refused a submitted Command before accepting it (DSS-CTR-013 section 9).

    ``effect_possible`` is the Provider's statement of whether a physical effect may nevertheless have
    occurred; the runtime treats anything but an explicit ``False`` as possible.
    """

    def __init__(self, category: str, *, effect_possible: bool = True) -> None:
        super().__init__(category)
        self.category = category
        self.effect_possible = effect_possible
