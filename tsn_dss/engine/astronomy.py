from __future__ import annotations

"""Astronomical observing conditions derived from Site plus optional target context."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
import warnings

import numpy as np
from astropy import units as u
from astropy.coordinates import EarthLocation, NonRotationTransformationWarning, SkyCoord, get_body, solar_system_ephemeris
from astropy.time import Time
from astropy.utils import iers
from astroplan import FixedTarget, Observer, TargetAlwaysUpWarning, TargetNeverUpWarning, moon_illumination, moon_phase_angle

from ..domain.models import Site


iers.conf.auto_download = False
iers.conf.auto_max_age = None
solar_system_ephemeris.set("builtin")
warnings.filterwarnings("ignore", category=NonRotationTransformationWarning)
warnings.filterwarnings("ignore", category=TargetAlwaysUpWarning)
warnings.filterwarnings("ignore", category=TargetNeverUpWarning)

CONDITIONS_SCOPES = ("rolling", "night")
_MOON_EVENT_SCAN_STEP = timedelta(minutes=2)
_MOON_EVENT_TOLERANCE = timedelta(seconds=1)


@dataclass(slots=True)
class AstronomicalTargetContext:
    target_name: str | None
    ra_deg: float
    dec_deg: float
    source_kind: str
    source_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_name": self.target_name,
            "ra_deg": self.ra_deg,
            "dec_deg": self.dec_deg,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
        }


@dataclass(slots=True)
class AstronomicalConditionsCurrent:
    time_utc: str
    sky_state: str
    sun_altitude_deg: float | None
    sun_azimuth_deg: float | None
    sunrise_utc: str | None
    sunset_utc: str | None
    civil_twilight_evening_start_utc: str | None
    civil_twilight_evening_end_utc: str | None
    civil_twilight_morning_start_utc: str | None
    civil_twilight_morning_end_utc: str | None
    nautical_twilight_evening_start_utc: str | None
    nautical_twilight_evening_end_utc: str | None
    nautical_twilight_morning_start_utc: str | None
    nautical_twilight_morning_end_utc: str | None
    astronomical_twilight_evening_start_utc: str | None
    astronomical_twilight_evening_end_utc: str | None
    astronomical_twilight_morning_start_utc: str | None
    astronomical_twilight_morning_end_utc: str | None
    astronomical_night_start_utc: str | None
    astronomical_night_end_utc: str | None
    moon_altitude_deg: float | None
    moon_azimuth_deg: float | None
    moon_illumination_fraction: float | None
    moon_illumination_pct: float | None
    moon_phase_angle_deg: float | None
    moon_phase_label: str | None
    moonrise_utc: str | None
    moonset_utc: str | None
    target: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "time_utc": self.time_utc,
            "sky_state": self.sky_state,
            "sun_altitude_deg": self.sun_altitude_deg,
            "sun_azimuth_deg": self.sun_azimuth_deg,
            "sunrise_utc": self.sunrise_utc,
            "sunset_utc": self.sunset_utc,
            "civil_twilight_evening_start_utc": self.civil_twilight_evening_start_utc,
            "civil_twilight_evening_end_utc": self.civil_twilight_evening_end_utc,
            "civil_twilight_morning_start_utc": self.civil_twilight_morning_start_utc,
            "civil_twilight_morning_end_utc": self.civil_twilight_morning_end_utc,
            "nautical_twilight_evening_start_utc": self.nautical_twilight_evening_start_utc,
            "nautical_twilight_evening_end_utc": self.nautical_twilight_evening_end_utc,
            "nautical_twilight_morning_start_utc": self.nautical_twilight_morning_start_utc,
            "nautical_twilight_morning_end_utc": self.nautical_twilight_morning_end_utc,
            "astronomical_twilight_evening_start_utc": self.astronomical_twilight_evening_start_utc,
            "astronomical_twilight_evening_end_utc": self.astronomical_twilight_evening_end_utc,
            "astronomical_twilight_morning_start_utc": self.astronomical_twilight_morning_start_utc,
            "astronomical_twilight_morning_end_utc": self.astronomical_twilight_morning_end_utc,
            "astronomical_night_start_utc": self.astronomical_night_start_utc,
            "astronomical_night_end_utc": self.astronomical_night_end_utc,
            "moon_altitude_deg": self.moon_altitude_deg,
            "moon_azimuth_deg": self.moon_azimuth_deg,
            "moon_illumination_fraction": self.moon_illumination_fraction,
            "moon_illumination_pct": self.moon_illumination_pct,
            "moon_phase_angle_deg": self.moon_phase_angle_deg,
            "moon_phase_label": self.moon_phase_label,
            "moonrise_utc": self.moonrise_utc,
            "moonset_utc": self.moonset_utc,
            "target": self.target,
        }


@dataclass(slots=True)
class AstronomicalConditionsHour:
    time_utc: str
    sky_state: str
    sun_altitude_deg: float | None
    moon_altitude_deg: float | None
    moon_azimuth_deg: float | None
    moon_illumination_pct: float | None
    target_altitude_deg: float | None
    target_azimuth_deg: float | None
    target_airmass: float | None
    moon_target_separation_deg: float | None
    target_above_horizon: bool | None
    target_above_observation_threshold: bool | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "time_utc": self.time_utc,
            "sky_state": self.sky_state,
            "sun_altitude_deg": self.sun_altitude_deg,
            "moon_altitude_deg": self.moon_altitude_deg,
            "moon_azimuth_deg": self.moon_azimuth_deg,
            "moon_illumination_pct": self.moon_illumination_pct,
            "target_altitude_deg": self.target_altitude_deg,
            "target_azimuth_deg": self.target_azimuth_deg,
            "target_airmass": self.target_airmass,
            "moon_target_separation_deg": self.moon_target_separation_deg,
            "target_above_horizon": self.target_above_horizon,
            "target_above_observation_threshold": self.target_above_observation_threshold,
        }


@dataclass(slots=True)
class AstronomicalMoonEvent:
    kind: str
    time_utc: str

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "time_utc": self.time_utc}


@dataclass(slots=True)
class AstronomicalNight:
    """One observing night: sunset to the following sunrise, with exact boundaries.

    Boundary fields use the same names and meanings as `AstronomicalConditionsCurrent`; null means the
    phase is not reached during this night. `status` is `ok`, `sun_does_not_set`, `sun_does_not_rise`
    (no sunset within the search horizon) or `incomplete` (sunset found, no following sunrise).
    """

    status: str
    boundaries: dict[str, str | None]
    moon_up_at_sunset: bool | None
    moon_events: list[AstronomicalMoonEvent]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            **self.boundaries,
            "moon_up_at_sunset": self.moon_up_at_sunset,
            "moon_events": [event.to_dict() for event in self.moon_events],
        }


@dataclass(slots=True)
class AstronomicalConditionsSnapshot:
    site_id: str
    site_name: str
    latitude_deg: float
    longitude_deg: float
    elevation_m: float | None
    generated_at_utc: str
    provider: str
    min_target_altitude_deg: float
    target: dict[str, Any] | None
    current: AstronomicalConditionsCurrent
    hourly: list[AstronomicalConditionsHour]
    night: AstronomicalNight | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "site_id": self.site_id,
            "site_name": self.site_name,
            "latitude_deg": self.latitude_deg,
            "longitude_deg": self.longitude_deg,
            "elevation_m": self.elevation_m,
            "generated_at_utc": self.generated_at_utc,
            "provider": self.provider,
            "min_target_altitude_deg": self.min_target_altitude_deg,
            "target": self.target,
            "current": self.current.to_dict(),
            "hourly": [hour.to_dict() for hour in self.hourly],
        }
        if self.night is not None:
            payload["night"] = self.night.to_dict()
        return payload


@dataclass(slots=True)
class _NightWindow:
    sunrise: Time | None
    sunset: Time | None
    civil_evening_start: Time | None
    civil_evening_end: Time | None
    civil_morning_start: Time | None
    civil_morning_end: Time | None
    nautical_evening_start: Time | None
    nautical_evening_end: Time | None
    nautical_morning_start: Time | None
    nautical_morning_end: Time | None
    astronomical_evening_start: Time | None
    astronomical_evening_end: Time | None
    astronomical_morning_start: Time | None
    astronomical_morning_end: Time | None
    astronomical_night_start: Time | None
    astronomical_night_end: Time | None


class AstronomicalConditionsService:
    provider_name = "tsn-dss-astronomy"

    def fetch_conditions(
        self,
        site: Site,
        *,
        reference_time_utc: datetime | str | None = None,
        target: AstronomicalTargetContext | None = None,
        min_target_altitude_deg: float = 30.0,
        forecast_hours: int = 24,
        scope: str = "rolling",
    ) -> AstronomicalConditionsSnapshot:
        """Astronomy at a reference time plus an hourly series.

        scope="rolling" (default): hourly from the reference hour for `forecast_hours`.
        scope="night": hourly covers the observing night (floor(sunset) .. ceil(sunrise)) anchored like the
        night window (daytime -> next night; otherwise the night that began at the previous sunset), and a
        `night` block is added. `forecast_hours` is ignored for scope="night".
        """
        if site.latitude_deg is None or site.longitude_deg is None:
            raise ValueError("Site must define latitude and longitude before astronomical conditions can be calculated.")
        if scope not in CONDITIONS_SCOPES:
            raise ValueError(f"scope must be one of: {', '.join(CONDITIONS_SCOPES)}.")

        reference_dt = normalize_utc_datetime(reference_time_utc)
        observer = _build_observer(site)
        reference_time = Time(reference_dt)

        current_sun_altaz = observer.sun_altaz(reference_time)
        sun_altitude_deg = _coerce_optional_float(current_sun_altaz.alt.deg)
        sky_state = classify_sky_state(sun_altitude_deg)
        night_window = _compute_night_window(observer, reference_time, sky_state)

        current_moon_altaz = observer.moon_altaz(reference_time)
        current_target = _compute_target_current(
            observer,
            reference_time,
            target,
            min_target_altitude_deg=min_target_altitude_deg,
            night_window=night_window,
        )
        current = AstronomicalConditionsCurrent(
            time_utc=to_utc_iso(reference_dt),
            sky_state=sky_state,
            sun_altitude_deg=sun_altitude_deg,
            sun_azimuth_deg=_coerce_optional_float(current_sun_altaz.az.deg),
            sunrise_utc=_time_to_utc_iso(night_window.sunrise),
            sunset_utc=_time_to_utc_iso(night_window.sunset),
            civil_twilight_evening_start_utc=_time_to_utc_iso(night_window.civil_evening_start),
            civil_twilight_evening_end_utc=_time_to_utc_iso(night_window.civil_evening_end),
            civil_twilight_morning_start_utc=_time_to_utc_iso(night_window.civil_morning_start),
            civil_twilight_morning_end_utc=_time_to_utc_iso(night_window.civil_morning_end),
            nautical_twilight_evening_start_utc=_time_to_utc_iso(night_window.nautical_evening_start),
            nautical_twilight_evening_end_utc=_time_to_utc_iso(night_window.nautical_evening_end),
            nautical_twilight_morning_start_utc=_time_to_utc_iso(night_window.nautical_morning_start),
            nautical_twilight_morning_end_utc=_time_to_utc_iso(night_window.nautical_morning_end),
            astronomical_twilight_evening_start_utc=_time_to_utc_iso(night_window.astronomical_evening_start),
            astronomical_twilight_evening_end_utc=_time_to_utc_iso(night_window.astronomical_evening_end),
            astronomical_twilight_morning_start_utc=_time_to_utc_iso(night_window.astronomical_morning_start),
            astronomical_twilight_morning_end_utc=_time_to_utc_iso(night_window.astronomical_morning_end),
            astronomical_night_start_utc=_time_to_utc_iso(night_window.astronomical_night_start),
            astronomical_night_end_utc=_time_to_utc_iso(night_window.astronomical_night_end),
            moon_altitude_deg=_coerce_optional_float(current_moon_altaz.alt.deg),
            moon_azimuth_deg=_coerce_optional_float(current_moon_altaz.az.deg),
            moon_illumination_fraction=_compute_moon_illumination_fraction(reference_time),
            moon_illumination_pct=_fraction_to_pct(_compute_moon_illumination_fraction(reference_time)),
            moon_phase_angle_deg=_compute_moon_phase_angle_deg(reference_time),
            moon_phase_label=describe_moon_phase(reference_time),
            moonrise_utc=_time_to_utc_iso(_safe_observer_time(observer.moon_rise_time, reference_time, which="next")),
            moonset_utc=_time_to_utc_iso(_safe_observer_time(observer.moon_set_time, reference_time, which="next")),
            target=current_target,
        )

        night = None
        if scope == "night":
            night, hourly_times = _compute_observing_night(observer, reference_time, sky_state)
        else:
            hourly_start = reference_dt.replace(minute=0, second=0, microsecond=0)
            hourly_times = [hourly_start + timedelta(hours=index) for index in range(max(1, forecast_hours))]
        hourly = [
            _compute_hour(
                observer,
                moment,
                target=target,
                min_target_altitude_deg=min_target_altitude_deg,
            )
            for moment in hourly_times
        ]

        return AstronomicalConditionsSnapshot(
            site_id=site.id,
            site_name=site.name,
            latitude_deg=site.latitude_deg,
            longitude_deg=site.longitude_deg,
            elevation_m=site.elevation_m,
            generated_at_utc=to_utc_iso(reference_dt),
            provider=self.provider_name,
            min_target_altitude_deg=min_target_altitude_deg,
            target=target.to_dict() if target else None,
            current=current,
            hourly=hourly,
            night=night,
        )


def normalize_utc_datetime(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc).replace(microsecond=0)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc, microsecond=0)
        return value.astimezone(timezone.utc).replace(microsecond=0)

    text = str(value).strip()
    if not text:
        return datetime.now(timezone.utc).replace(microsecond=0)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def to_utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def classify_sky_state(sun_altitude_deg: float | None) -> str:
    if sun_altitude_deg is None:
        return "unknown"
    if sun_altitude_deg >= 0.0:
        return "day"
    if sun_altitude_deg >= -6.0:
        return "civil_twilight"
    if sun_altitude_deg >= -12.0:
        return "nautical_twilight"
    if sun_altitude_deg >= -18.0:
        return "astronomical_twilight"
    return "astronomical_night"


def describe_moon_phase(reference_time: Time) -> str | None:
    illumination = _compute_moon_illumination_fraction(reference_time)
    if illumination is None:
        return None
    future_illumination = _compute_moon_illumination_fraction(reference_time + 6 * u.hour)
    waxing = future_illumination is not None and future_illumination >= illumination

    if illumination <= 0.05:
        return "new moon"
    if illumination >= 0.95:
        return "full moon"
    if 0.45 <= illumination <= 0.55:
        return "first quarter" if waxing else "last quarter"
    if illumination < 0.45:
        return "waxing crescent" if waxing else "waning crescent"
    return "waxing gibbous" if waxing else "waning gibbous"


def _build_observer(site: Site) -> Observer:
    location = EarthLocation.from_geodetic(
        lon=float(site.longitude_deg) * u.deg,
        lat=float(site.latitude_deg) * u.deg,
        height=float(site.elevation_m or 0.0) * u.m,
    )
    return Observer(location=location, name=site.name, timezone="UTC")


def _compute_hour(
    observer: Observer,
    moment_dt: datetime,
    *,
    target: AstronomicalTargetContext | None,
    min_target_altitude_deg: float,
) -> AstronomicalConditionsHour:
    moment = Time(moment_dt)
    sun_altaz = observer.sun_altaz(moment)
    sun_altitude_deg = _coerce_optional_float(sun_altaz.alt.deg)
    moon_altaz = observer.moon_altaz(moment)

    target_altitude_deg = None
    target_azimuth_deg = None
    target_airmass = None
    target_above_horizon = None
    target_above_threshold = None
    moon_target_separation_deg = None

    if target is not None:
        fixed_target = _build_fixed_target(target)
        moon_coord = get_body("moon", moment, location=observer.location).transform_to(fixed_target.coord.frame)
        target_altaz = observer.altaz(moment, fixed_target.coord)
        target_altitude_deg = _coerce_optional_float(target_altaz.alt.deg)
        target_azimuth_deg = _coerce_optional_float(target_altaz.az.deg)
        target_airmass = _coerce_airmass(target_altaz)
        target_above_horizon = None if target_altitude_deg is None else target_altitude_deg > 0.0
        target_above_threshold = None if target_altitude_deg is None else target_altitude_deg >= min_target_altitude_deg
        moon_target_separation_deg = _coerce_optional_float(moon_coord.separation(fixed_target.coord).deg)

    return AstronomicalConditionsHour(
        time_utc=to_utc_iso(moment_dt),
        sky_state=classify_sky_state(sun_altitude_deg),
        sun_altitude_deg=sun_altitude_deg,
        moon_altitude_deg=_coerce_optional_float(moon_altaz.alt.deg),
        moon_azimuth_deg=_coerce_optional_float(moon_altaz.az.deg),
        moon_illumination_pct=_fraction_to_pct(_compute_moon_illumination_fraction(moment)),
        target_altitude_deg=target_altitude_deg,
        target_azimuth_deg=target_azimuth_deg,
        target_airmass=target_airmass,
        moon_target_separation_deg=moon_target_separation_deg,
        target_above_horizon=target_above_horizon,
        target_above_observation_threshold=target_above_threshold,
    )


def _compute_target_current(
    observer: Observer,
    reference_time: Time,
    target: AstronomicalTargetContext | None,
    *,
    min_target_altitude_deg: float,
    night_window: _NightWindow,
) -> dict[str, Any] | None:
    if target is None:
        return None

    fixed_target = _build_fixed_target(target)
    target_altaz = observer.altaz(reference_time, fixed_target.coord)
    altitude_deg = _coerce_optional_float(target_altaz.alt.deg)
    above_horizon = None if altitude_deg is None else altitude_deg > 0.0
    above_threshold = None if altitude_deg is None else altitude_deg >= min_target_altitude_deg
    moon_coord = get_body("moon", reference_time, location=observer.location).transform_to(fixed_target.coord.frame)

    observation_window = _derive_target_window(
        observer,
        fixed_target,
        night_window.astronomical_night_start,
        night_window.astronomical_night_end,
        threshold_deg=min_target_altitude_deg,
    )
    horizon_window = _derive_target_window(
        observer,
        fixed_target,
        night_window.astronomical_night_start,
        night_window.astronomical_night_end,
        threshold_deg=0.0,
    )

    return {
        **target.to_dict(),
        "altitude_deg": altitude_deg,
        "azimuth_deg": _coerce_optional_float(target_altaz.az.deg),
        "airmass": _coerce_airmass(target_altaz),
        "transit_time_utc": _pick_transit_time_utc(
            observer,
            fixed_target,
            reference_time,
            night_window.astronomical_night_start,
            night_window.astronomical_night_end,
        ),
        "max_altitude_deg": observation_window["max_altitude_deg"],
        "moon_separation_deg": _coerce_optional_float(moon_coord.separation(fixed_target.coord).deg),
        "above_horizon": above_horizon,
        "above_observation_threshold": above_threshold,
        "above_horizon_window_start_utc": horizon_window["start_utc"],
        "above_horizon_window_end_utc": horizon_window["end_utc"],
        "above_horizon_window_status": horizon_window["status"],
        "observation_window_start_utc": observation_window["start_utc"],
        "observation_window_end_utc": observation_window["end_utc"],
        "observation_window_status": observation_window["status"],
    }


def _derive_target_window(
    observer: Observer,
    target: FixedTarget,
    start: Time | None,
    end: Time | None,
    *,
    threshold_deg: float,
) -> dict[str, Any]:
    if start is None or end is None:
        return {"start_utc": None, "end_utc": None, "status": "no_astronomical_night", "max_altitude_deg": None}

    start_dt = _time_to_datetime(start)
    end_dt = _time_to_datetime(end)
    if start_dt is None or end_dt is None or end_dt <= start_dt:
        return {"start_utc": None, "end_utc": None, "status": "no_astronomical_night", "max_altitude_deg": None}

    total_seconds = max(int((end_dt - start_dt).total_seconds()), 1)
    samples = max(12, min(289, total_seconds // 300 + 1))
    offsets = np.linspace(0, total_seconds, samples) * u.s
    sample_times = start + offsets
    altitudes = observer.altaz(sample_times, target.coord).alt.deg
    altitudes_array = np.asarray(altitudes, dtype=float)
    visible_flags = altitudes_array >= threshold_deg

    if not np.any(visible_flags):
        return {
            "start_utc": None,
            "end_utc": None,
            "status": "never_up" if threshold_deg <= 0.0 else "not_observable",
            "max_altitude_deg": float(np.max(altitudes_array)) if altitudes_array.size else None,
        }

    first_index = int(np.argmax(visible_flags))
    last_index = int(len(visible_flags) - np.argmax(visible_flags[::-1]) - 1)
    status = "windowed"
    if np.all(visible_flags):
        status = "always_up" if threshold_deg <= 0.0 else "always_observable"

    return {
        "start_utc": _time_to_utc_iso(sample_times[first_index]),
        "end_utc": _time_to_utc_iso(sample_times[last_index]),
        "status": status,
        "max_altitude_deg": float(np.max(altitudes_array)) if altitudes_array.size else None,
    }


def _compute_night_window(observer: Observer, reference_time: Time, sky_state: str) -> _NightWindow:
    _, window = _select_observing_night_window(observer, reference_time, sky_state)
    return window


def _compute_observing_night(
    observer: Observer,
    reference_time: Time,
    sky_state: str,
) -> tuple[AstronomicalNight, list[datetime]]:
    """The observing night for a reference time, with every boundary searched forward from its sunset.

    Anchoring matches the night window: daytime -> next sunset; otherwise the previous sunset. Unlike the
    rolling current block, evening boundaries are the next events after that sunset and must precede the
    following sunrise, so they always belong to the same night.
    """
    status, window = _select_observing_night_window(observer, reference_time, sky_state)
    if status != "ok":
        return AstronomicalNight(status, _night_boundaries_to_dict(window), None, []), []

    sunset = window.sunset
    sunrise = window.sunrise
    if sunset is None or sunrise is None:
        return AstronomicalNight("incomplete", _night_boundaries_to_dict(window), None, []), []

    moon_up_at_sunset, moon_events = _compute_moon_events(observer, sunset, sunrise)
    night = AstronomicalNight("ok", _night_boundaries_to_dict(window), moon_up_at_sunset, moon_events)
    return night, _hourly_times_covering(sunset, sunrise)


def _select_observing_night_window(
    observer: Observer,
    reference_time: Time,
    sky_state: str,
) -> tuple[str, _NightWindow]:
    sunset = _safe_observer_time(observer.sun_set_time, reference_time, which="next" if sky_state == "day" else "previous")
    if sunset is None:
        status = "sun_does_not_set" if sky_state == "day" else "sun_does_not_rise"
        return status, _empty_night_window()

    sunrise = _safe_observer_time(observer.sun_rise_time, sunset, which="next")
    night_end_bound = sunrise if sunrise is not None else sunset + 24 * u.hour

    def within_night(value: Time | None) -> Time | None:
        return value if value is not None and sunset < value < night_end_bound else None

    civil_evening_end = within_night(_safe_observer_time(observer.twilight_evening_civil, sunset, which="next"))
    nautical_evening_end = within_night(_safe_observer_time(observer.twilight_evening_nautical, sunset, which="next"))
    astronomical_evening_end = within_night(_safe_observer_time(observer.twilight_evening_astronomical, sunset, which="next"))

    def morning_after(evening_end: Time | None, method: Any) -> Time | None:
        if evening_end is None:
            return None
        return within_night(_safe_observer_time(method, evening_end, which="next"))

    civil_morning_start = morning_after(civil_evening_end, observer.twilight_morning_civil)
    nautical_morning_start = morning_after(nautical_evening_end, observer.twilight_morning_nautical)
    astronomical_morning_start = morning_after(astronomical_evening_end, observer.twilight_morning_astronomical)

    window = _NightWindow(
        sunrise=sunrise,
        sunset=sunset,
        civil_evening_start=sunset,
        civil_evening_end=civil_evening_end,
        civil_morning_start=civil_morning_start,
        civil_morning_end=sunrise,
        nautical_evening_start=civil_evening_end,
        nautical_evening_end=nautical_evening_end,
        nautical_morning_start=nautical_morning_start,
        nautical_morning_end=civil_morning_start,
        astronomical_evening_start=nautical_evening_end,
        astronomical_evening_end=astronomical_evening_end,
        astronomical_morning_start=astronomical_morning_start,
        astronomical_morning_end=nautical_morning_start,
        astronomical_night_start=astronomical_evening_end,
        astronomical_night_end=astronomical_morning_start,
    )
    if sunrise is None:
        return "incomplete", window
    return "ok", window


def _empty_night_window() -> _NightWindow:
    return _NightWindow(*([None] * 16))


def _night_boundaries_to_dict(window: _NightWindow) -> dict[str, str | None]:
    return {
        "sunset_utc": _time_to_utc_iso(window.sunset),
        "sunrise_utc": _time_to_utc_iso(window.sunrise),
        "civil_twilight_evening_start_utc": _time_to_utc_iso(window.civil_evening_start),
        "civil_twilight_evening_end_utc": _time_to_utc_iso(window.civil_evening_end),
        "civil_twilight_morning_start_utc": _time_to_utc_iso(window.civil_morning_start),
        "civil_twilight_morning_end_utc": _time_to_utc_iso(window.civil_morning_end),
        "nautical_twilight_evening_start_utc": _time_to_utc_iso(window.nautical_evening_start),
        "nautical_twilight_evening_end_utc": _time_to_utc_iso(window.nautical_evening_end),
        "nautical_twilight_morning_start_utc": _time_to_utc_iso(window.nautical_morning_start),
        "nautical_twilight_morning_end_utc": _time_to_utc_iso(window.nautical_morning_end),
        "astronomical_twilight_evening_start_utc": _time_to_utc_iso(window.astronomical_evening_start),
        "astronomical_twilight_evening_end_utc": _time_to_utc_iso(window.astronomical_evening_end),
        "astronomical_twilight_morning_start_utc": _time_to_utc_iso(window.astronomical_morning_start),
        "astronomical_twilight_morning_end_utc": _time_to_utc_iso(window.astronomical_morning_end),
        "astronomical_night_start_utc": _time_to_utc_iso(window.astronomical_night_start),
        "astronomical_night_end_utc": _time_to_utc_iso(window.astronomical_night_end),
    }


def _hourly_times_covering(start: Time, end: Time) -> list[datetime]:
    start_dt = _time_to_datetime(start)
    end_dt = _time_to_datetime(end)
    if start_dt is None or end_dt is None:
        return []
    first = start_dt.replace(minute=0, second=0, microsecond=0)
    last = end_dt.replace(minute=0, second=0, microsecond=0)
    if last < end_dt:
        last += timedelta(hours=1)
    hours = int((last - first).total_seconds() // 3600)
    return [first + timedelta(hours=index) for index in range(hours + 1)]


def _compute_moon_events(observer: Observer, start: Time, end: Time) -> tuple[bool | None, list[AstronomicalMoonEvent]]:
    """Moonrise/moonset inside [start, end] from the Moon's true altitude (centre at 0 deg, no refraction).

    astroplan's moon_rise_time/moon_set_time can miss a crossing shortly after the anchor, so crossings are
    found on a fixed scan grid and refined by bisection.
    """
    start_dt = _time_to_datetime(start)
    end_dt = _time_to_datetime(end)
    if start_dt is None or end_dt is None or end_dt <= start_dt:
        return None, []

    step_seconds = _MOON_EVENT_SCAN_STEP.total_seconds()
    total_seconds = (end_dt - start_dt).total_seconds()
    offsets = list(np.arange(0.0, total_seconds, step_seconds)) + [total_seconds]
    sample_times = start + np.asarray(offsets) * u.s
    altitudes = np.asarray(observer.moon_altaz(sample_times).alt.deg, dtype=float)
    above = altitudes > 0.0

    events: list[AstronomicalMoonEvent] = []
    for index in range(len(offsets) - 1):
        if above[index] == above[index + 1]:
            continue
        low, high = offsets[index], offsets[index + 1]
        while high - low > _MOON_EVENT_TOLERANCE.total_seconds():
            middle = (low + high) / 2.0
            if (float(observer.moon_altaz(start + middle * u.s).alt.deg) > 0.0) == above[index]:
                low = middle
            else:
                high = middle
        crossing = _time_to_utc_iso(start + high * u.s)
        if crossing is not None:
            events.append(AstronomicalMoonEvent("moonset" if above[index] else "moonrise", crossing))
    return bool(above[0]), events


def _safe_observer_time(method: Any, anchor: Time, *, which: str) -> Time | None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", TargetAlwaysUpWarning)
            warnings.simplefilter("ignore", TargetNeverUpWarning)
            return _coerce_time_or_none(method(anchor, which=which))
    except Exception:
        return None


def _pick_transit_time_utc(
    observer: Observer,
    target: FixedTarget,
    reference_time: Time,
    night_start: Time | None,
    night_end: Time | None,
) -> str | None:
    candidates = [
        _safe_target_time(observer.target_meridian_transit_time, reference_time, target, which="previous"),
        _safe_target_time(observer.target_meridian_transit_time, reference_time, target, which="next"),
    ]
    if night_start is not None and night_end is not None:
        night_start_dt = _time_to_datetime(night_start)
        night_end_dt = _time_to_datetime(night_end)
        if night_start_dt is not None and night_end_dt is not None:
            for candidate in candidates:
                candidate_dt = _time_to_datetime(candidate)
                if candidate_dt is not None and night_start_dt <= candidate_dt <= night_end_dt:
                    return _time_to_utc_iso(candidate)
            return None

    for candidate in candidates:
        serialized = _time_to_utc_iso(candidate)
        if serialized is not None:
            return serialized
    return None


def _safe_target_time(method: Any, anchor: Time, target: FixedTarget, *, which: str) -> Time | None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", TargetAlwaysUpWarning)
            warnings.simplefilter("ignore", TargetNeverUpWarning)
            return _coerce_time_or_none(method(anchor, target, which=which))
    except Exception:
        return None


def _build_fixed_target(target: AstronomicalTargetContext) -> FixedTarget:
    return FixedTarget(
        coord=SkyCoord(ra=float(target.ra_deg) * u.deg, dec=float(target.dec_deg) * u.deg, frame="icrs"),
        name=target.target_name or target.source_id or "target",
    )


def _compute_moon_illumination_fraction(moment: Time) -> float | None:
    try:
        return float(moon_illumination(moment))
    except Exception:
        return None


def _compute_moon_phase_angle_deg(moment: Time) -> float | None:
    try:
        return float(moon_phase_angle(moment).to_value(u.deg))
    except Exception:
        return None


def _fraction_to_pct(value: float | None) -> float | None:
    if value is None:
        return None
    return value * 100.0


def _coerce_airmass(altaz: SkyCoord) -> float | None:
    try:
        secz = getattr(altaz, "secz", None)
        if secz is None:
            return None
        value = float(secz.value)
        if not np.isfinite(value) or value < 1.0:
            return None
        return value
    except Exception:
        return None


def _coerce_time_or_none(value: Any) -> Time | None:
    if value is None:
        return None
    try:
        mask = getattr(value, "mask", None)
        if mask is not None and bool(np.any(mask)):
            return None
    except Exception:
        return None
    try:
        return value if isinstance(value, Time) else Time(value)
    except Exception:
        return None


def _time_to_utc_iso(value: Time | None) -> str | None:
    dt = _time_to_datetime(value)
    if dt is None:
        return None
    return to_utc_iso(dt)


def _time_to_datetime(value: Time | None) -> datetime | None:
    if value is None:
        return None
    try:
        return value.to_datetime(timezone.utc).replace(microsecond=0)
    except Exception:
        return None


def _coerce_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except Exception:
        return None
    return number if np.isfinite(number) else None
