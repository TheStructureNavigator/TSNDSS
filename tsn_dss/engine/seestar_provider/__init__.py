"""Seestar read-only Provider (ROADMAP_DEVICE_BACKEND.md DB-02).

Vendor-specific code lives here, outside the vendor-neutral ``device_runtime``.
Importing this package performs no network access, reads no key and imports no
third-party module. Read-only: no Commands, no preview streaming, no motion.
"""

import sys as _sys

from .auth import Authenticator, RsaKeyFileAuthenticator
from .config import SeestarProviderConfig
from .errors import (
    SeestarAuthError,
    SeestarConfigError,
    SeestarError,
    SeestarMethodNotAllowed,
    SeestarProtocolError,
    SeestarRpcError,
    SeestarTimeout,
    SeestarUnreachable,
)
from .protocol import READ_METHODS, RpcReply, SeestarAnnouncement
from .provider import SeestarProvider
from .transport import SeestarReadTransport, TcpSeestarTransport

__all__ = [
    name
    for name, value in list(globals().items())
    if not name.startswith("_") and not isinstance(value, type(_sys))
]
