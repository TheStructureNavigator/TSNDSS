from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

from ..domain.models import Site


@dataclass(slots=True)
class SiteForecastCurrent:
    time: str | None
    temperature_c: float | None
    relative_humidity_pct: float | None
    dew_point_c: float | None
    dew_margin_c: float | None
    dew_risk: str | None
    apparent_temperature_c: float | None
    cloud_cover_pct: float | None
    cloud_cover_low_pct: float | None
    cloud_cover_mid_pct: float | None
    cloud_cover_high_pct: float | None
    visibility_m: float | None
    surface_pressure_hpa: float | None
    wind_speed_kmh: float | None
    wind_direction_deg: float | None
    wind_gusts_kmh: float | None
    precipitation_mm: float | None
    precipitation_probability_pct: float | None
    condition_code: int | None
    is_day: int | None
    time_utc: str | None = None


@dataclass(slots=True)
class SiteForecastHour:
    time: str
    temperature_c: float | None
    relative_humidity_pct: float | None
    dew_point_c: float | None
    dew_margin_c: float | None
    dew_risk: str | None
    apparent_temperature_c: float | None
    cloud_cover_pct: float | None
    cloud_cover_low_pct: float | None
    cloud_cover_mid_pct: float | None
    cloud_cover_high_pct: float | None
    visibility_m: float | None
    surface_pressure_hpa: float | None
    wind_speed_kmh: float | None
    wind_direction_deg: float | None
    wind_gusts_kmh: float | None
    precipitation_mm: float | None
    precipitation_probability_pct: float | None
    condition_code: int | None
    is_day: int | None
    time_utc: str | None = None


@dataclass(slots=True)
class SiteForecastSnapshot:
    site_id: str
    site_name: str
    latitude_deg: float
    longitude_deg: float
    timezone: str | None
    generated_at: str | None
    provider: str
    current: SiteForecastCurrent | None
    hourly: list[SiteForecastHour]
    utc_offset_seconds: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "site_id": self.site_id,
            "site_name": self.site_name,
            "latitude_deg": self.latitude_deg,
            "longitude_deg": self.longitude_deg,
            "timezone": self.timezone,
            "utc_offset_seconds": self.utc_offset_seconds,
            "generated_at": self.generated_at,
            "provider": self.provider,
            "current": None if self.current is None else {
                "time": self.current.time,
                "time_utc": self.current.time_utc,
                "temperature_c": self.current.temperature_c,
                "relative_humidity_pct": self.current.relative_humidity_pct,
                "dew_point_c": self.current.dew_point_c,
                "dew_margin_c": self.current.dew_margin_c,
                "dew_risk": self.current.dew_risk,
                "apparent_temperature_c": self.current.apparent_temperature_c,
                "cloud_cover_pct": self.current.cloud_cover_pct,
                "cloud_cover_low_pct": self.current.cloud_cover_low_pct,
                "cloud_cover_mid_pct": self.current.cloud_cover_mid_pct,
                "cloud_cover_high_pct": self.current.cloud_cover_high_pct,
                "visibility_m": self.current.visibility_m,
                "surface_pressure_hpa": self.current.surface_pressure_hpa,
                "wind_speed_kmh": self.current.wind_speed_kmh,
                "wind_direction_deg": self.current.wind_direction_deg,
                "wind_gusts_kmh": self.current.wind_gusts_kmh,
                "precipitation_mm": self.current.precipitation_mm,
                "precipitation_probability_pct": self.current.precipitation_probability_pct,
                "condition_code": self.current.condition_code,
                "is_day": self.current.is_day,
            },
            "hourly": [
                {
                    "time": hour.time,
                    "time_utc": hour.time_utc,
                    "temperature_c": hour.temperature_c,
                    "relative_humidity_pct": hour.relative_humidity_pct,
                    "dew_point_c": hour.dew_point_c,
                    "dew_margin_c": hour.dew_margin_c,
                    "dew_risk": hour.dew_risk,
                    "apparent_temperature_c": hour.apparent_temperature_c,
                    "cloud_cover_pct": hour.cloud_cover_pct,
                    "cloud_cover_low_pct": hour.cloud_cover_low_pct,
                    "cloud_cover_mid_pct": hour.cloud_cover_mid_pct,
                    "cloud_cover_high_pct": hour.cloud_cover_high_pct,
                    "visibility_m": hour.visibility_m,
                    "surface_pressure_hpa": hour.surface_pressure_hpa,
                    "wind_speed_kmh": hour.wind_speed_kmh,
                    "wind_direction_deg": hour.wind_direction_deg,
                    "wind_gusts_kmh": hour.wind_gusts_kmh,
                    "precipitation_mm": hour.precipitation_mm,
                    "precipitation_probability_pct": hour.precipitation_probability_pct,
                    "condition_code": hour.condition_code,
                    "is_day": hour.is_day,
                }
                for hour in self.hourly
            ],
        }


class OpenMeteoForecastClient:
    base_url = "https://api.open-meteo.com/v1/forecast"
    provider_name = "open-meteo"

    def fetch_site_forecast(self, site: Site, *, forecast_days: int = 1) -> SiteForecastSnapshot:
        if site.latitude_deg is None or site.longitude_deg is None:
            raise ValueError("Site must define latitude and longitude before forecast can be requested.")

        params = {
            "latitude": site.latitude_deg,
            "longitude": site.longitude_deg,
            "forecast_days": max(1, min(16, int(forecast_days))),
            "timezone": "auto",
            # Unix instants are unambiguous; provider-local labels are derived from them below.
            "timeformat": "unixtime",
            "temperature_unit": "celsius",
            "wind_speed_unit": "kmh",
            "precipitation_unit": "mm",
            "current": ",".join(
                [
                    "temperature_2m",
                    "relative_humidity_2m",
                    "dew_point_2m",
                    "apparent_temperature",
                    "cloud_cover",
                    "cloud_cover_low",
                    "cloud_cover_mid",
                    "cloud_cover_high",
                    "visibility",
                    "surface_pressure",
                    "wind_speed_10m",
                    "wind_direction_10m",
                    "wind_gusts_10m",
                    "precipitation",
                    "weather_code",
                    "is_day",
                ]
            ),
            "hourly": ",".join(
                [
                    "temperature_2m",
                    "relative_humidity_2m",
                    "dew_point_2m",
                    "apparent_temperature",
                    "cloud_cover",
                    "cloud_cover_low",
                    "cloud_cover_mid",
                    "cloud_cover_high",
                    "visibility",
                    "surface_pressure",
                    "wind_speed_10m",
                    "wind_direction_10m",
                    "wind_gusts_10m",
                    "precipitation",
                    "precipitation_probability",
                    "weather_code",
                    "is_day",
                ]
            ),
        }
        if site.elevation_m is not None:
            params["elevation"] = site.elevation_m

        url = f"{self.base_url}?{urlencode(params)}"
        with urlopen(url, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))

        utc_offset_seconds = _coerce_required_int(payload.get("utc_offset_seconds"), "utc_offset_seconds")

        current_payload = payload.get("current")
        current = None
        if isinstance(current_payload, dict):
            current_temperature_c = _coerce_optional_float(current_payload.get("temperature_2m"))
            current_dew_point_c = _coerce_optional_float(current_payload.get("dew_point_2m"))
            current_epoch = _coerce_optional_int(current_payload.get("time"))
            current = SiteForecastCurrent(
                time=_provider_local_label(current_epoch, utc_offset_seconds),
                time_utc=_epoch_to_utc_iso(current_epoch),
                temperature_c=current_temperature_c,
                relative_humidity_pct=_coerce_optional_float(current_payload.get("relative_humidity_2m")),
                dew_point_c=current_dew_point_c,
                dew_margin_c=calculate_dew_margin_c(current_temperature_c, current_dew_point_c),
                dew_risk=classify_dew_risk(calculate_dew_margin_c(current_temperature_c, current_dew_point_c)),
                apparent_temperature_c=_coerce_optional_float(current_payload.get("apparent_temperature")),
                cloud_cover_pct=_coerce_optional_float(current_payload.get("cloud_cover")),
                cloud_cover_low_pct=_coerce_optional_float(current_payload.get("cloud_cover_low")),
                cloud_cover_mid_pct=_coerce_optional_float(current_payload.get("cloud_cover_mid")),
                cloud_cover_high_pct=_coerce_optional_float(current_payload.get("cloud_cover_high")),
                visibility_m=_coerce_optional_float(current_payload.get("visibility")),
                surface_pressure_hpa=_coerce_optional_float(current_payload.get("surface_pressure")),
                wind_speed_kmh=_coerce_optional_float(current_payload.get("wind_speed_10m")),
                wind_direction_deg=_coerce_optional_float(current_payload.get("wind_direction_10m")),
                wind_gusts_kmh=_coerce_optional_float(current_payload.get("wind_gusts_10m")),
                precipitation_mm=_coerce_optional_float(current_payload.get("precipitation")),
                precipitation_probability_pct=_coerce_optional_float(current_payload.get("precipitation_probability")),
                condition_code=_coerce_optional_int(current_payload.get("weather_code")),
                is_day=_coerce_optional_int(current_payload.get("is_day")),
            )

        hourly_payload = payload.get("hourly")
        hourly: list[SiteForecastHour] = []
        if isinstance(hourly_payload, dict):
            epochs = _coerce_epoch_list(hourly_payload.get("time"))
            temperatures = _coerce_float_list(hourly_payload.get("temperature_2m"))
            humidities = _coerce_float_list(hourly_payload.get("relative_humidity_2m"))
            dew_points = _coerce_float_list(hourly_payload.get("dew_point_2m"))
            apparent_temperatures = _coerce_float_list(hourly_payload.get("apparent_temperature"))
            cloud_covers = _coerce_float_list(hourly_payload.get("cloud_cover"))
            cloud_covers_low = _coerce_float_list(hourly_payload.get("cloud_cover_low"))
            cloud_covers_mid = _coerce_float_list(hourly_payload.get("cloud_cover_mid"))
            cloud_covers_high = _coerce_float_list(hourly_payload.get("cloud_cover_high"))
            visibility_values = _coerce_float_list(hourly_payload.get("visibility"))
            surface_pressures = _coerce_float_list(hourly_payload.get("surface_pressure"))
            wind_speeds = _coerce_float_list(hourly_payload.get("wind_speed_10m"))
            wind_directions = _coerce_float_list(hourly_payload.get("wind_direction_10m"))
            wind_gusts = _coerce_float_list(hourly_payload.get("wind_gusts_10m"))
            precipitation = _coerce_float_list(hourly_payload.get("precipitation"))
            precipitation_probabilities = _coerce_float_list(hourly_payload.get("precipitation_probability"))
            condition_codes = _coerce_int_list(hourly_payload.get("weather_code"))
            is_day_values = _coerce_int_list(hourly_payload.get("is_day"))

            for index, epoch in enumerate(epochs):
                temperature_c = _value_at(temperatures, index)
                dew_point_c = _value_at(dew_points, index)
                dew_margin_c = calculate_dew_margin_c(temperature_c, dew_point_c)
                hourly.append(
                    SiteForecastHour(
                        time=_provider_local_label(epoch, utc_offset_seconds),
                        time_utc=_epoch_to_utc_iso(epoch),
                        temperature_c=temperature_c,
                        relative_humidity_pct=_value_at(humidities, index),
                        dew_point_c=dew_point_c,
                        dew_margin_c=dew_margin_c,
                        dew_risk=classify_dew_risk(dew_margin_c),
                        apparent_temperature_c=_value_at(apparent_temperatures, index),
                        cloud_cover_pct=_value_at(cloud_covers, index),
                        cloud_cover_low_pct=_value_at(cloud_covers_low, index),
                        cloud_cover_mid_pct=_value_at(cloud_covers_mid, index),
                        cloud_cover_high_pct=_value_at(cloud_covers_high, index),
                        visibility_m=_value_at(visibility_values, index),
                        surface_pressure_hpa=_value_at(surface_pressures, index),
                        wind_speed_kmh=_value_at(wind_speeds, index),
                        wind_direction_deg=_value_at(wind_directions, index),
                        wind_gusts_kmh=_value_at(wind_gusts, index),
                        precipitation_mm=_value_at(precipitation, index),
                        precipitation_probability_pct=_value_at(precipitation_probabilities, index),
                        condition_code=_value_at(condition_codes, index),
                        is_day=_value_at(is_day_values, index),
                    )
                )

        return SiteForecastSnapshot(
            site_id=site.id,
            site_name=site.name,
            latitude_deg=site.latitude_deg,
            longitude_deg=site.longitude_deg,
            timezone=_coerce_optional_string(payload.get("timezone")),
            generated_at=current.time if current is not None else (hourly[0].time if hourly else None),
            provider=self.provider_name,
            current=current,
            hourly=hourly,
            utc_offset_seconds=utc_offset_seconds,
        )


def calculate_dew_margin_c(
    temperature_c: float | None,
    dew_point_c: float | None,
) -> float | None:
    if temperature_c is None or dew_point_c is None:
        return None
    return temperature_c - dew_point_c


def classify_dew_risk(dew_margin_c: float | None) -> str | None:
    if dew_margin_c is None:
        return None
    if dew_margin_c <= 2.0:
        return "high"
    if dew_margin_c <= 5.0:
        return "moderate"
    return "low"


def _coerce_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _coerce_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _coerce_required_int(value: Any, field_name: str) -> int:
    if value is None:
        raise ValueError(f"Forecast provider response is missing {field_name}.")
    return int(value)


def _coerce_epoch_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    return [int(item) for item in value]


def _epoch_to_utc_iso(epoch_seconds: int | None) -> str | None:
    if epoch_seconds is None:
        return None
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _provider_local_label(epoch_seconds: int | None, utc_offset_seconds: int) -> str | None:
    """Open-Meteo's local wall-clock label: the instant shifted by the single response-level UTC offset.

    Open-Meteo applies one offset to the whole response, so after a DST change inside the forecast range
    these labels differ from civil time by the DST delta. Use time_utc for joins and calculations.
    """
    if epoch_seconds is None:
        return None
    shifted = datetime.fromtimestamp(epoch_seconds, timezone.utc) + timedelta(seconds=utc_offset_seconds)
    return shifted.strftime("%Y-%m-%dT%H:%M")


def _coerce_float_list(value: Any) -> list[float | None]:
    if not isinstance(value, list):
        return []
    return [None if item is None else float(item) for item in value]


def _coerce_int_list(value: Any) -> list[int | None]:
    if not isinstance(value, list):
        return []
    return [None if item is None else int(item) for item in value]


def _value_at(values: list[Any], index: int) -> Any:
    if index >= len(values):
        return None
    return values[index]
