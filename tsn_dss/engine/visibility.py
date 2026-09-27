from __future__ import annotations

"""Target visibility services.

The service composes existing TSN DSS astronomy, target repository, and Local Horizon
capabilities. It owns deterministic visibility semantics; MCP merely exposes them.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from typing import Protocol
from typing import Any

from ..domain.local_horizon import get_local_horizon_altitude
from ..domain.models import LocalHorizonPoint, Site
from .astronomy import (
    AstronomicalConditionsService,
    AstronomicalTargetContext,
    _build_fixed_target,
    _build_observer,
    normalize_utc_datetime,
    to_utc_iso,
)
from .sqlite.planning import PlanningRepository
from .target_resolution import TargetResolutionRequest, resolve_astronomical_target_context

_STATE_DETECTION_STEP = timedelta(seconds=30)
_TRANSITION_TOLERANCE = timedelta(seconds=30)
_MAX_ALTITUDE_SAMPLE_STEP = timedelta(seconds=60)


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


@dataclass(frozen=True, slots=True)
class VisibilityPointEvaluation:
    time_utc: datetime
    altitude_deg: float | None
    azimuth_deg: float | None
    min_target_altitude_deg: float
    above_geometric_horizon: bool | None
    above_minimum_altitude: bool | None
    local_horizon: LocalHorizonAnalysis
    visible: bool


@dataclass(frozen=True, slots=True)
class VisibilityWindow:
    start_utc: str
    end_utc: str
    starts_at_interval_start: bool
    ends_at_interval_end: bool
    max_altitude_deg: float | None
    max_altitude_time_utc: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_utc": self.start_utc,
            "end_utc": self.end_utc,
            "starts_at_interval_start": self.starts_at_interval_start,
            "ends_at_interval_end": self.ends_at_interval_end,
            "max_altitude_deg": self.max_altitude_deg,
            "max_altitude_time_utc": self.max_altitude_time_utc,
        }


@dataclass(frozen=True, slots=True)
class VisibilityWindowDiagnostics:
    """Small diagnostic summary derived from evaluated visibility states.

    These flags describe the deterministic evaluation grid plus refined transition
    points. They are not proofs that the opposite condition never occurred between
    evaluated instants.
    """

    samples_evaluated: int
    local_horizon_available: bool
    any_above_geometric_horizon: bool
    any_above_minimum_altitude: bool
    any_altitude_constraints_satisfied: bool
    any_clear_of_local_horizon: bool
    any_visible: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "samples_evaluated": self.samples_evaluated,
            "local_horizon_available": self.local_horizon_available,
            "any_above_geometric_horizon": self.any_above_geometric_horizon,
            "any_above_minimum_altitude": self.any_above_minimum_altitude,
            "any_altitude_constraints_satisfied": self.any_altitude_constraints_satisfied,
            "any_clear_of_local_horizon": self.any_clear_of_local_horizon,
            "any_visible": self.any_visible,
        }


@dataclass(frozen=True, slots=True)
class VisibilityWindowResult:
    interval_start_utc: str
    interval_end_utc: str
    min_target_altitude_deg: float
    windows: tuple[VisibilityWindow, ...]
    diagnostics: VisibilityWindowDiagnostics

    def to_dict(self) -> dict[str, Any]:
        return {
            "interval_start_utc": self.interval_start_utc,
            "interval_end_utc": self.interval_end_utc,
            "min_target_altitude_deg": self.min_target_altitude_deg,
            "windows": [window.to_dict() for window in self.windows],
            "diagnostics": self.diagnostics.to_dict(),
        }


class VisibilityEvaluator(Protocol):
    def altitude_azimuth_at(self, moment_utc: datetime) -> tuple[float | None, float | None]:
        """Return target altitude and azimuth at a normalized UTC instant."""


class _AstropyVisibilityEvaluator:
    def __init__(self, site: Site, target: AstronomicalTargetContext) -> None:
        self.observer = _build_observer(site)
        self.fixed_target = _build_fixed_target(target)

    def altitude_azimuth_at(self, moment_utc: datetime) -> tuple[float | None, float | None]:
        from astropy.time import Time

        altaz = self.observer.altaz(Time(moment_utc), self.fixed_target.coord)
        return _optional_float(altaz.alt.deg), _optional_float(altaz.az.deg)


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


def visibility_windows(
    site: Site,
    target: AstronomicalTargetContext,
    *,
    start_time_utc: datetime | str,
    end_time_utc: datetime | str,
    min_target_altitude_deg: float = 30.0,
    evaluator: VisibilityEvaluator | None = None,
) -> VisibilityWindowResult:
    """Calculate deterministic geometric/constraint visibility windows.

    The interval is evaluated as a closed UTC interval and returned windows are clipped to
    its boundaries. Twilight, weather, Moon constraints and planning/ranking concerns are
    intentionally outside this predicate.
    """
    _validate_window_inputs(site, target, min_target_altitude_deg)
    start = normalize_utc_datetime(start_time_utc)
    end = normalize_utc_datetime(end_time_utc)
    if end <= start:
        raise ValueError("end_time_utc must be later than start_time_utc.")

    point_evaluator = evaluator or _AstropyVisibilityEvaluator(site, target)
    sample_times = _sample_interval(start, end, step=_STATE_DETECTION_STEP)
    samples = [
        _evaluate_visibility_at_time(
            site,
            point_evaluator,
            moment,
            min_target_altitude_deg=min_target_altitude_deg,
        )
        for moment in sample_times
    ]

    windows: list[VisibilityWindow] = []
    active_start: datetime | None = start if samples[0].visible else None
    transition_points: list[VisibilityPointEvaluation] = []

    for previous, current in zip(samples, samples[1:]):
        if not previous.visible and current.visible:
            active_start = _refine_transition(
                site,
                point_evaluator,
                previous.time_utc,
                current.time_utc,
                target_state=True,
                min_target_altitude_deg=min_target_altitude_deg,
            )
            transition_points.append(
                _evaluate_visibility_at_time(
                    site,
                    point_evaluator,
                    active_start,
                    min_target_altitude_deg=min_target_altitude_deg,
                )
            )
        elif previous.visible and not current.visible:
            active_end = _refine_transition(
                site,
                point_evaluator,
                previous.time_utc,
                current.time_utc,
                target_state=False,
                min_target_altitude_deg=min_target_altitude_deg,
            )
            if active_start is None:
                active_start = start
            windows.append(
                _build_window(
                    point_evaluator,
                    start=active_start,
                    end=active_end,
                    interval_start=start,
                    interval_end=end,
                )
            )
            transition_points.append(
                _evaluate_visibility_at_time(
                    site,
                    point_evaluator,
                    active_end,
                    min_target_altitude_deg=min_target_altitude_deg,
                )
            )
            active_start = None

    if active_start is not None:
        windows.append(
            _build_window(
                point_evaluator,
                start=active_start,
                end=end,
                interval_start=start,
                interval_end=end,
            )
        )

    diagnostics = _summarize_window_diagnostics(site.horizon_profile, [*samples, *transition_points])
    return VisibilityWindowResult(
        interval_start_utc=to_utc_iso(start),
        interval_end_utc=to_utc_iso(end),
        min_target_altitude_deg=float(min_target_altitude_deg),
        windows=tuple(windows),
        diagnostics=diagnostics,
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

        evaluation = _evaluate_visibility_components(
            site.horizon_profile,
            time_utc=evaluated_time,
            target_altitude_deg=_optional_float(current_target.get("altitude_deg")),
            target_azimuth_deg=_optional_float(current_target.get("azimuth_deg")),
            min_target_altitude_deg=snapshot.min_target_altitude_deg,
        )

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
            "altitude_deg": evaluation.altitude_deg,
            "azimuth_deg": evaluation.azimuth_deg,
            "airmass": current_target.get("airmass"),
            "sky_state": snapshot.current.sky_state,
            "min_target_altitude_deg": snapshot.min_target_altitude_deg,
            "above_geometric_horizon": evaluation.above_geometric_horizon,
            "above_minimum_altitude": evaluation.above_minimum_altitude,
            "local_horizon_available": evaluation.local_horizon.profile_available,
            "local_horizon_altitude_deg": evaluation.local_horizon.local_horizon_altitude_deg,
            "local_horizon_clearance_deg": evaluation.local_horizon.clearance_deg,
            "above_local_horizon": evaluation.local_horizon.above_local_horizon,
            "moon_altitude_deg": snapshot.current.moon_altitude_deg,
            "moon_azimuth_deg": snapshot.current.moon_azimuth_deg,
            "moon_illumination_pct": snapshot.current.moon_illumination_pct,
            "moon_phase_label": snapshot.current.moon_phase_label,
            "moon_target_separation_deg": current_target.get("moon_separation_deg"),
            "visible": evaluation.visible,
            "visible_rule": (
                "above_geometric_horizon AND above_minimum_altitude AND "
                "(no Local Horizon profile OR above_local_horizon); weather is not included"
            ),
        }


def _evaluate_visibility_at_time(
    site: Site,
    evaluator: VisibilityEvaluator,
    moment_utc: datetime,
    *,
    min_target_altitude_deg: float,
) -> VisibilityPointEvaluation:
    altitude, azimuth = evaluator.altitude_azimuth_at(moment_utc)
    return _evaluate_visibility_components(
        site.horizon_profile,
        time_utc=moment_utc,
        target_altitude_deg=altitude,
        target_azimuth_deg=azimuth,
        min_target_altitude_deg=min_target_altitude_deg,
    )


def _evaluate_visibility_components(
    profile: list[LocalHorizonPoint],
    *,
    time_utc: datetime,
    target_altitude_deg: float | None,
    target_azimuth_deg: float | None,
    min_target_altitude_deg: float,
) -> VisibilityPointEvaluation:
    altitude = _optional_float(target_altitude_deg)
    azimuth = _optional_float(target_azimuth_deg)
    local_horizon = analyze_local_horizon_visibility(
        profile,
        target_altitude_deg=altitude,
        target_azimuth_deg=azimuth,
    )
    above_geometric = None if altitude is None else altitude > 0.0
    above_minimum = None if altitude is None else altitude >= min_target_altitude_deg
    local_horizon_allows = (
        not local_horizon.profile_available
        or local_horizon.above_local_horizon is True
    )
    return VisibilityPointEvaluation(
        time_utc=time_utc,
        altitude_deg=altitude,
        azimuth_deg=azimuth,
        min_target_altitude_deg=float(min_target_altitude_deg),
        above_geometric_horizon=above_geometric,
        above_minimum_altitude=above_minimum,
        local_horizon=local_horizon,
        visible=bool(above_geometric is True and above_minimum is True and local_horizon_allows),
    )


def _validate_window_inputs(
    site: Site,
    target: AstronomicalTargetContext,
    min_target_altitude_deg: float,
) -> None:
    if site.latitude_deg is None or site.longitude_deg is None:
        raise ValueError("Site must define latitude and longitude before visibility can be calculated.")
    if not _is_finite_in_range(float(site.latitude_deg), -90.0, 90.0):
        raise ValueError("Site latitude_deg must be in [-90, 90].")
    if not _is_finite_in_range(float(site.longitude_deg), -180.0, 180.0):
        raise ValueError("Site longitude_deg must be in [-180, 180].")
    if not _is_finite_in_range(float(target.ra_deg), 0.0, 360.0, upper_inclusive=False):
        raise ValueError("Target ra_deg must be in [0, 360).")
    if not _is_finite_in_range(float(target.dec_deg), -90.0, 90.0):
        raise ValueError("Target dec_deg must be in [-90, 90].")
    if not _is_finite_in_range(float(min_target_altitude_deg), 0.0, 90.0):
        raise ValueError("min_target_altitude_deg must be in [0, 90].")


def _sample_interval(start: datetime, end: datetime, *, step: timedelta) -> list[datetime]:
    samples = [start]
    current = start + step
    while current < end:
        samples.append(current)
        current += step
    if samples[-1] != end:
        samples.append(end)
    return samples


def _refine_transition(
    site: Site,
    evaluator: VisibilityEvaluator,
    left: datetime,
    right: datetime,
    *,
    target_state: bool,
    min_target_altitude_deg: float,
) -> datetime:
    left_visible = _evaluate_visibility_at_time(
        site,
        evaluator,
        left,
        min_target_altitude_deg=min_target_altitude_deg,
    ).visible
    while right - left > _TRANSITION_TOLERANCE:
        midpoint = left + (right - left) / 2
        midpoint_visible = _evaluate_visibility_at_time(
            site,
            evaluator,
            midpoint,
            min_target_altitude_deg=min_target_altitude_deg,
        ).visible
        if midpoint_visible == left_visible:
            left = midpoint
        else:
            right = midpoint
    return right if target_state else left


def _build_window(
    evaluator: VisibilityEvaluator,
    *,
    start: datetime,
    end: datetime,
    interval_start: datetime,
    interval_end: datetime,
) -> VisibilityWindow:
    max_altitude, max_time = _find_max_altitude(evaluator, start, end)
    return VisibilityWindow(
        start_utc=to_utc_iso(start),
        end_utc=to_utc_iso(end),
        starts_at_interval_start=start == interval_start,
        ends_at_interval_end=end == interval_end,
        max_altitude_deg=max_altitude,
        max_altitude_time_utc=to_utc_iso(max_time) if max_time is not None else None,
    )


def _find_max_altitude(
    evaluator: VisibilityEvaluator,
    start: datetime,
    end: datetime,
) -> tuple[float | None, datetime | None]:
    samples = _sample_interval(start, end, step=_MAX_ALTITUDE_SAMPLE_STEP)
    best_altitude: float | None = None
    best_time: datetime | None = None
    for moment in samples:
        altitude, _ = evaluator.altitude_azimuth_at(moment)
        if altitude is None:
            continue
        if best_altitude is None or altitude > best_altitude:
            best_altitude = altitude
            best_time = moment
    return best_altitude, best_time


def _summarize_window_diagnostics(
    profile: list[LocalHorizonPoint],
    evaluations: list[VisibilityPointEvaluation],
) -> VisibilityWindowDiagnostics:
    local_horizon_available = bool(profile)
    any_above_geometric = any(item.above_geometric_horizon is True for item in evaluations)
    any_above_minimum = any(item.above_minimum_altitude is True for item in evaluations)
    any_altitude_constraints = any(
        item.above_geometric_horizon is True and item.above_minimum_altitude is True
        for item in evaluations
    )
    any_clear_local = (
        True
        if not local_horizon_available
        else any(item.local_horizon.above_local_horizon is True for item in evaluations)
    )
    return VisibilityWindowDiagnostics(
        samples_evaluated=len(evaluations),
        local_horizon_available=local_horizon_available,
        any_above_geometric_horizon=any_above_geometric,
        any_above_minimum_altitude=any_above_minimum,
        any_altitude_constraints_satisfied=any_altitude_constraints,
        any_clear_of_local_horizon=any_clear_local,
        any_visible=any(item.visible for item in evaluations),
    )


def _is_finite_in_range(
    value: float,
    lower: float,
    upper: float,
    *,
    upper_inclusive: bool = True,
) -> bool:
    if not math.isfinite(value) or value < lower:
        return False
    return value <= upper if upper_inclusive else value < upper


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except Exception:
        return None
    return number if math.isfinite(number) else None
