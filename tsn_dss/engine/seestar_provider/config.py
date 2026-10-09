"""Operator-supplied configuration with redaction.

Hosts and key paths are connection data. They are never printed, repr'd or put
into error messages. The key itself is never read here; only its path is held,
and only ``auth.RsaKeyFileAuthenticator`` ever opens it.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field

from .errors import SeestarConfigError

DEFAULT_TCP_PORT = 4700
DEFAULT_UDP_PORT = 4720
# Limited broadcast is the only built-in discovery target. There is no default host.
DEFAULT_BROADCAST_TARGETS = ("255.255.255.255",)

_HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
_PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def validate_host(host: str) -> str:
    """Return ``host`` if it is a bare IPv4/IPv6 literal or hostname, else raise."""
    if not isinstance(host, str) or not host or host != host.strip():
        raise SeestarConfigError("invalid_host")
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    if "://" in host or "/" in host or ":" in host or not _HOSTNAME_RE.match(host):
        raise SeestarConfigError("invalid_host")
    return host


@dataclass(slots=True, frozen=True)
class SeestarProviderConfig:
    provider_id: str = "seestar"
    host: str | None = None
    key_path: str | None = None
    allow_udp_discovery: bool = False
    broadcast_targets: tuple[str, ...] = DEFAULT_BROADCAST_TARGETS
    tcp_port: int = DEFAULT_TCP_PORT
    udp_port: int = DEFAULT_UDP_PORT
    connect_timeout_s: float = 3.0
    read_timeout_s: float = 5.0
    discovery_timeout_s: float = 2.0
    read_retries: int = 1
    stale_retention_s: float = 60.0
    preview_camera: str = "wide"
    _redact: bool = field(default=True, repr=False)

    def __post_init__(self) -> None:
        if not _PROVIDER_ID_RE.match(self.provider_id or ""):
            raise SeestarConfigError("invalid_provider_id")
        if self.host is not None:
            validate_host(self.host)
        if self.key_path is not None and (not isinstance(self.key_path, str) or not self.key_path.strip()):
            raise SeestarConfigError("invalid_key_path")
        for port in (self.tcp_port, self.udp_port):
            if not isinstance(port, int) or not 0 < port < 65536:
                raise SeestarConfigError("invalid_port")
        for value in (self.connect_timeout_s, self.read_timeout_s, self.discovery_timeout_s):
            if not isinstance(value, (int, float)) or value <= 0:
                raise SeestarConfigError("invalid_timeout")
        if not isinstance(self.read_retries, int) or not 0 <= self.read_retries <= 3:
            raise SeestarConfigError("invalid_retries")
        if self.stale_retention_s < 0:
            raise SeestarConfigError("invalid_retention")
        if self.preview_camera not in ("main", "wide"):
            raise SeestarConfigError("invalid_preview_camera")
        for target in self.broadcast_targets:
            try:
                ipaddress.IPv4Address(target)
            except ValueError:
                raise SeestarConfigError("invalid_broadcast_target") from None

    @property
    def is_complete(self) -> bool:
        """A key path plus either an explicit host or opt-in UDP discovery.

        The key requirement is a design decision (the handshake is documented as mandatory
        from firmware 7.18), not something proven for read requests on firmware 9.31.
        """
        return bool(self.key_path) and bool(self.host or self.allow_udp_discovery)

    def __repr__(self) -> str:
        return (
            f"SeestarProviderConfig(provider_id={self.provider_id!r}, "
            f"host={'<set>' if self.host else None}, "
            f"key_path={'<set>' if self.key_path else None}, "
            f"allow_udp_discovery={self.allow_udp_discovery})"
        )

    __str__ = __repr__

    def redacted(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "host": "<set>" if self.host else None,
            "key_path": "<set>" if self.key_path else None,
            "allow_udp_discovery": self.allow_udp_discovery,
            "read_retries": self.read_retries,
        }

    def with_host(self, host: str | None) -> "SeestarProviderConfig":
        """Return a copy with another explicit host (the operator-driven IP-change path)."""
        return SeestarProviderConfig(
            provider_id=self.provider_id,
            host=host,
            key_path=self.key_path,
            allow_udp_discovery=self.allow_udp_discovery,
            broadcast_targets=self.broadcast_targets,
            tcp_port=self.tcp_port,
            udp_port=self.udp_port,
            connect_timeout_s=self.connect_timeout_s,
            read_timeout_s=self.read_timeout_s,
            discovery_timeout_s=self.discovery_timeout_s,
            read_retries=self.read_retries,
            stale_retention_s=self.stale_retention_s,
            preview_camera=self.preview_camera,
        )
