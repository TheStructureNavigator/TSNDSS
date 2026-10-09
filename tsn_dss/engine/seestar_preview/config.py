"""Preview configuration and safe stream-address construction.

The host is connection data. It is never printed, repr'd or put in an error
message. Ports and the stream path are fixed by the DB-02 availability rule
(``seestar_provider.normalize.PREVIEW_PORTS``) and cannot be overridden, so a
configuration can only point the integration at the one camera endpoint the
device itself reports as working.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..seestar_provider.config import validate_host
from ..seestar_provider.errors import SeestarConfigError
from ..seestar_provider.normalize import PREVIEW_PORTS

CAMERAS = ("main", "wide")
STREAM_PATH = "stream"
MAX_READINESS_AGE_S = 60.0


@dataclass(slots=True, frozen=True)
class StreamEndpoint:
    """Where one camera's stream would be read from. The address is redacted everywhere."""

    camera: str
    address: str = field(repr=False)

    def __repr__(self) -> str:
        return f"StreamEndpoint(camera={self.camera!r}, address=<redacted>)"

    __str__ = __repr__


@dataclass(slots=True, frozen=True)
class SeestarPreviewConfig:
    host: str = field(repr=False)
    cameras: tuple[str, ...] = CAMERAS
    readiness_max_age_s: float = 10.0

    def __post_init__(self) -> None:
        try:
            validate_host(self.host)
        except SeestarConfigError:
            raise SeestarConfigError("invalid_host") from None
        if (
            not isinstance(self.cameras, tuple)
            or not 1 <= len(self.cameras) <= len(CAMERAS)
            or len(set(self.cameras)) != len(self.cameras)
            or any(camera not in CAMERAS for camera in self.cameras)
        ):
            raise SeestarConfigError("invalid_cameras")
        age = self.readiness_max_age_s
        if isinstance(age, bool) or not isinstance(age, (int, float)) or not 0 < age <= MAX_READINESS_AGE_S:
            raise SeestarConfigError("invalid_readiness_age")

    def __repr__(self) -> str:
        return f"SeestarPreviewConfig(host=<set>, cameras={self.cameras!r})"

    __str__ = __repr__

    def redacted(self) -> dict[str, object]:
        return {"host": "<set>", "cameras": list(self.cameras), "readiness_max_age_s": self.readiness_max_age_s}

    def endpoint(self, camera: str) -> StreamEndpoint:
        """Address of ``camera``'s stream. Building it opens nothing."""
        if camera not in self.cameras:
            raise SeestarConfigError("camera_not_configured")
        host = f"[{self.host}]" if ":" in self.host else self.host
        return StreamEndpoint(camera, f"rtsp://{host}:{PREVIEW_PORTS[camera]}/{STREAM_PATH}")
