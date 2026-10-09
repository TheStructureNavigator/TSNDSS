"""Authentication prerequisite for the TCP control port.

The handshake is a connection prerequisite only. It presents an
operator-supplied key to the device; it grants no permission for physical
control, and the provider has no control surface.

Requiring a key is a TSNDSS design decision. The research documents the
handshake as mandatory from firmware 7.18 (secondary evidence from the
seestarpy documentation), but it has not been proven here that read requests
are refused without it on firmware 9.31. That is a hardware-validation item.

The private key is never bundled, extracted, cached or logged. Only a path is
configured; the file is opened at signing time and discarded immediately.
Handshake method names exist only in this module and are not part of the
read-only allow-list in ``protocol.py``.
"""

from __future__ import annotations

import base64
from typing import Any, Callable, Mapping, Protocol

from .errors import SeestarAuthError, SeestarProtocolError

HANDSHAKE_IDS = (1001, 1002, 1003)
_NO_AUTH_REQUIRED_CODE = 103  # "method not found": firmware without the handshake


class Authenticator(Protocol):
    def sign(self, challenge: str) -> str:
        """Return the base64 signature for ``challenge``."""
        ...


class RsaKeyFileAuthenticator:
    """RSA-SHA1 (PKCS#1 v1.5) signer using an operator-supplied PEM file.

    ``cryptography`` is imported lazily and is an optional dependency. All
    failures surface as fixed categories without paths or exception text.
    """

    def __init__(self, key_path: str) -> None:
        if not key_path:
            raise SeestarAuthError("key_not_configured")
        self._key_path = key_path

    def __repr__(self) -> str:
        return "RsaKeyFileAuthenticator(key_path=<set>)"

    def sign(self, challenge: str) -> str:
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
        except ImportError:
            raise SeestarAuthError("crypto_library_missing") from None
        try:
            with open(self._key_path, "rb") as handle:
                private_key = serialization.load_pem_private_key(handle.read(), password=None)
            signature = private_key.sign(challenge.encode("utf-8"), padding.PKCS1v15(), hashes.SHA1())
        except (OSError, ValueError, TypeError):
            raise SeestarAuthError("key_unusable") from None
        return base64.b64encode(signature).decode("ascii")


def perform_handshake(
    exchange: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    authenticator: Authenticator,
) -> None:
    """Run the three-step handshake over ``exchange`` (send one message, return its reply)."""
    first = exchange({"id": HANDSHAKE_IDS[0], "method": "get_verify_str", "params": "verify"})
    if first.get("code") == _NO_AUTH_REQUIRED_CODE:
        return
    result = first.get("result")
    challenge = result.get("str") if isinstance(result, dict) else result
    if not isinstance(challenge, str) or not challenge:
        return  # no challenge offered: treated as firmware without the handshake
    signature = authenticator.sign(challenge)
    verdict = exchange(
        {"id": HANDSHAKE_IDS[1], "method": "verify_client", "params": {"sign": signature, "data": challenge}}
    )
    if verdict.get("code") != 0:
        raise SeestarAuthError("auth_rejected")
    exchange({"id": HANDSHAKE_IDS[2], "method": "pi_is_verified", "params": "verify"})
    # The confirmation reply is informational; a non-zero code is not fatal.


def require_mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise SeestarProtocolError("malformed_frame")
    return value
