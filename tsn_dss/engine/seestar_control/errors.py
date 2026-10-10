"""Control-channel failure phases (DB-05 Slice 1). Messages are fixed category tokens, never device data."""

from __future__ import annotations

from ..seestar_provider.errors import SeestarError


class ControlPreSendError(SeestarError):
    """Failure before any byte of the command frame was handed to the socket. Structurally proves the command was not sent."""


class ControlPostSendError(SeestarError):
    """Failure at or after the point where the command frame may have started to be sent. The effect is unknown."""
