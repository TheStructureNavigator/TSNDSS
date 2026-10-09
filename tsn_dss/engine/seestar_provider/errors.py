"""Exceptions for the Seestar read-only provider.

Messages are fixed category strings. They never contain hosts, addresses, key
paths, serial numbers or device payloads, so they are safe to log.
"""

from __future__ import annotations


class SeestarError(Exception):
    """Base class. ``category`` is a short fixed token, never device data."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class SeestarConfigError(SeestarError):
    """The provider configuration is invalid or incomplete."""


class SeestarMethodNotAllowed(SeestarError):
    """A request outside the read-only allow-list was attempted."""


class SeestarTimeout(SeestarError):
    """A connect, send or read exceeded its deadline."""


class SeestarUnreachable(SeestarError):
    """The device could not be reached or the connection was lost."""


class SeestarProtocolError(SeestarError):
    """The device answered with something this client cannot interpret."""


class SeestarAuthError(SeestarError):
    """The authentication prerequisite failed (missing, unusable or rejected key)."""


class SeestarRpcError(SeestarError):
    """The device answered with a non-zero RPC code."""

    def __init__(self, code: object) -> None:
        super().__init__("rpc_error")
        self.code = code if isinstance(code, int) else None
