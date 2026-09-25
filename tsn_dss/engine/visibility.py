from __future__ import annotations

"""Point-in-time target visibility service.

The service composes existing TSN DSS astronomy, target repository, and Local Horizon
capabilities. It owns the deterministic WZRD-MCP-1 visibility boolean; MCP merely exposes it.
"""

import sqlite3
from dataclasses import dataclass
from typing import Any

from ..domain.local_horizon import get_local_horizon_altitude
from ..domain.models import LocalHorizonPoint
from .astronomy import AstronomicalConditionsService, normalize_utc_datetime, to_utc_iso
from .sqlite.planning import PlanningRepository
from .target_resolution import TargetResolutionRequest, resolve_astronomical_target_context


@dataclass(frozen=True, slots=True)
class LocalHorizonAnalysis:
    profile_available: bool
    target_altitude_deg: float | None
    target_azimuth_deg: float | None
    local_horizon_altitude_deg: float | None
    clearance_deg: float | None
    above_local_horizon: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_available": self.profile_available,
            "target_altitude_deg": self.target_altitude_deg,
            "target_azimuth_deg": self.target_azimuth_deg,
            "local_horizon_altitude_deg": self.local_horizon_altitude_deg,
            "clearance_deg": self.clearance_deg,
            "above_local_horizon": self.above_local_horizon,
        }


def analyze_local_horizon_visibility(
    profile: list[LocalHorizonPoint],
    *,
    target_altitude_deg: float | None,
    target_azimuth_deg: float | None,
) -> LocalHorizonAnalysis:
    """Evaluate target clearance against a Site Local Horizon profile.

    No profile means the Local Horizon is unavailable/not configured and does not block
    WZRD-MCP-1 visibility. Equality with the horizon is not above it, matching the current
    frontend `clearance > 0` behavior.
    """
    if not profile:
        return LocalHorizonAnalysis(
            profile_available=False,
            target_altitude_deg=target_altitude_deg,
            target_azimuth_deg=target_azimuth_deg,
            local_horizon_altitude_deg=None,
            clearance_deg=None,
            above_local_horizon=None,
        )
    if target_altitude_deg is None or target_azimuth_deg is None:
        return LocalHorizonAnalysis(
            profile_available=True,
            target_altitude_deg=target_altitude_deg,
            target_azimuth_deg=target_azimuth_deg,
            local_horizon_altitude_deg=None,
            clearance_deg=None,
            above_local_horizon=None,
        )

    local_horizon_altitude = get_local_horizon_altitude(profile, target_azimuth_deg)
    clearance = None if local_horizon_altitude is None else target_altitude_deg - local_horizon_altitude
    return LocalHorizonAnalysis(
        profile_available=True,
        target_altitude_deg=target_altitude_deg,
        target_azimuth_deg=target_azimuth_deg,
        local_horizon_altitude_deg=local_horizon_altitude,
        clearance_deg=clearance,
        above_local_horizon=None if clearance is None else clearance > 0,
    )


class TargetVisibilityService:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        astronomy_service: AstronomicalConditionsService | None = None,
    ) -> None:
        self.connection = connection
        self.planning = PlanningRepository(connection)
        self.astronomy = astronomy_service or AstronomicalConditionsService()

    def target_visibility_at(
        self,
        *,
        site_id: str,
        time_utc: str,
        target_id: str | None = None,
        target_query: str | None = None,
        target_ra_deg: float | None = None,
        target_dec_deg: float | None = None,
        min_target_altitude_deg: float = 30.0,
    ) -> dict[str, Any]:
        site = self.planning.get_site(site_id)
        if site is None:
            raise ValueError(f"Unknown site_id: {site_id}")
        if site.latitude_deg is None or site.longitude_deg is None:
            raise ValueError("Site must define latitude and longitude before visibility can be calculated.")

        target = resolve_astronomical_target_context(
            TargetResolutionRequest(
                target_id=target_id,
                target_name=target_query,
                target_ra_deg=target_ra_deg,
                target_dec_deg=target_dec_deg,
            ),
            planning_repository=self.planning,
        )
        if target is None:
            raise ValueError("Provide target_id, target_query, or explicit target coordinates.")

        evaluated_time = normalize_utc_datetime(time_utc)
        snapshot = self.astronomy.fetch_conditions(
            site,
            reference_time_utc=evaluated_time,
            target=target,
            min_target_altitude_deg=min_target_altitude_deg,
            forecast_hours=1,
        )
        current_target = snapshot.current.target
        if current_target is None:
            raise RuntimeError("Astronomy service did not return target context.")

        altitude = _optional_float(current_target.get("altitude_deg"))
        azimuth = _optional_float(current_target.get("azimuth_deg"))
        local_horizon = analyze_local_horizon_visibility(
            site.horizon_profile,
            target_altitude_deg=altitude,
            target_azimuth_deg=azimuth,
        )
        above_geometric = current_target.get("above_horizon")
        above_minimum = current_target.get("above_observation_threshold")
        local_horizon_allows = (
            not local_horizon.profile_available
            or local_horizon.above_local_horizon is True
        )
        visible = bool(above_geometric is True and above_minimum is True and local_horizon_allows)

        return {
            "site": {
                "id": site.id,
                "name": site.name,
                "latitude_deg": site.latitude_deg,
                "longitude_deg": site.longitude_deg,
                "elevation_m": site.elevation_m,
            },
            "target": target.to_dict(),
            "evaluated_time_utc": to_utc_iso(evaluated_time),
            "altitude_deg": altitude,
            "azimuth_deg": azimuth,
            "airmass": current_target.get("airmass"),
            "sky_state": snapshot.current.sky_state,
            "min_target_altitude_deg": snapshot.min_target_altitude_deg,
            "above_geometric_horizon": above_geometric,
            "above_minimum_altitude": above_minimum,
            "local_horizon_available": local_horizon.profile_available,
            "local_horizon_altitude_deg": local_horizon.local_horizon_altitude_deg,
            "local_horizon_clearance_deg": local_horizon.clearance_deg,
            "above_local_horizon": local_horizon.above_local_horizon,
            "moon_altitude_deg": snapshot.current.moon_altitude_deg,
            "moon_azimuth_deg": snapshot.current.moon_azimuth_deg,
            "moon_illumination_pct": snapshot.current.moon_illumination_pct,
            "moon_phase_label": snapshot.current.moon_phase_label,
            "moon_target_separation_deg": current_target.get("moon_separation_deg"),
            "visible": visible,
            "visible_rule": (
                "above_geometric_horizon AND above_minimum_altitude AND "
                "(no Local Horizon profile OR above_local_horizon); weather is not included"
            ),
        }


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)
