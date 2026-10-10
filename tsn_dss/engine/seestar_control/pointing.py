"""Target-dependent pointing safety and the provisional coordinate convention for GoTo (DB-05b v2).

COORDINATE CONVENTION (PROVISIONAL, NOT VERIFIED ON THE FIRMWARE). A ``GotoTarget`` holds RA hours and Dec degrees in the *of-date*
frame (FK5 with the equinox of the observation time, "JNow"). This follows the practical approach of the third-party seestar_alp
project, which converts catalog (J2000/ICRS) input to the equinox of now before sending and uses device-reported coordinates as
of-date. No vendor source confirms it. The only conversion in TSN DSS is ``of_date_from_j2000`` below, called explicitly by the
caller; nothing converts silently, and an input that does not say which frame it is in is rejected by the CLI.

POINTING SAFETY. ``GotoSafety`` bundles a Site, two limits chosen by the owner and nothing else. There are NO default limits: both are
required, positive and finite. ``evaluate_pointing`` answers ``None`` (allowed) or a fixed token, and fails closed with
``pointing_evaluation_failed`` for any input it cannot evaluate (missing Site coordinates, bad time, an ephemeris error). The limits
are TSN DSS pointing policy proposals, not device limits: no device-supported movement limit is known or assumed.
The sky geometry (astropy, a declared project dependency) lives outside this stdlib-only package in ``tsn_dss.engine.pointing_geometry`` and
is injected as ``ephemeris``; it uses the Site model's latitude, longitude and elevation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .commands import GotoTarget

__all__ = ["GotoSafety", "evaluate_pointing"]

# injected: (target, site, when_utc) -> (target altitude in degrees, separation of the target from the Sun in degrees); both of-date, topocentric altitude
Ephemeris = Callable[[GotoTarget, Any, datetime], "tuple[float, float]"]


def _positive_finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number.")
    return float(value)


@dataclass(frozen=True)
class GotoSafety:
    """Site plus the two owner-approved limits. No defaults."""

    site: Any  # tsn_dss.domain.models.Site (latitude_deg, longitude_deg, elevation_m)
    min_altitude_deg: float
    min_sun_separation_deg: float
    ephemeris: Ephemeris = field(repr=False, compare=False)  # required: tsn_dss.engine.pointing_geometry in production, a stand-in in tests

    def __post_init__(self) -> None:
        _positive_finite(self.min_altitude_deg, "min_altitude_deg")
        _positive_finite(self.min_sun_separation_deg, "min_sun_separation_deg")
        if self.min_altitude_deg >= 90 or self.min_sun_separation_deg > 180:
            raise ValueError("limits are out of range.")
        lat, lon = getattr(self.site, "latitude_deg", None), getattr(self.site, "longitude_deg", None)
        for value in (lat, lon):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError("the Site needs a finite latitude and longitude.")
        if not -90 <= lat <= 90 or not -180 <= lon <= 360:
            raise ValueError("the Site latitude or longitude is out of range.")


def evaluate_pointing(target: object, safety: GotoSafety | None, when: object) -> str | None:
    """``None`` if the target may be commanded now, else a fixed token. Anything that cannot be evaluated is a block."""
    try:
        if safety is None:
            return "pointing_policy_not_configured"
        if not isinstance(target, GotoTarget):
            return "pointing_evaluation_failed"
        if not isinstance(when, datetime) or when.tzinfo is None or when.utcoffset() is None:
            return "pointing_evaluation_failed"
        site = safety.site
        for name in ("latitude_deg", "longitude_deg"):
            value = getattr(site, name, None)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                return "pointing_evaluation_failed"
        if not -90 <= site.latitude_deg <= 90 or not -180 <= site.longitude_deg <= 360:
            return "pointing_evaluation_failed"
        altitude, sun_separation = safety.ephemeris(target, site, when.astimezone(timezone.utc))
        if not (math.isfinite(altitude) and math.isfinite(sun_separation)):
            return "pointing_evaluation_failed"
        if altitude < safety.min_altitude_deg:
            return "target_below_minimum_altitude"
        if sun_separation < safety.min_sun_separation_deg:
            return "target_too_close_to_sun"
        return None
    except Exception:
        return "pointing_evaluation_failed"
