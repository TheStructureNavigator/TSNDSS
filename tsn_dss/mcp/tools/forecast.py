from __future__ import annotations

"""Read-only MCP exposure of the existing TSN DSS Site forecast (Open-Meteo) path."""

import http.client
import sqlite3
from typing import Any, Protocol

from ...domain.models import Site
from ...engine.sqlite.planning import PlanningRepository
from ...engine.weather import SiteForecastSnapshot
from ..serializers import site_summary_to_dict
from .site_resolution import resolve_site

DEFAULT_MCP_FORECAST_DAYS = 2
# Same range and clamping as OpenMeteoForecastClient and the HTTP /api/site-forecast endpoint.
MIN_FORECAST_DAYS = 1
MAX_FORECAST_DAYS = 16

FORECAST_UNITS = {
    "temperature_c": "degC",
    "apparent_temperature_c": "degC",
    "dew_point_c": "degC",
    "dew_margin_c": "degC",
    "relative_humidity_pct": "percent",
    "cloud_cover_pct": "percent",
    "cloud_cover_low_pct": "percent",
    "cloud_cover_mid_pct": "percent",
    "cloud_cover_high_pct": "percent",
    "precipitation_probability_pct": "percent",
    "precipitation_mm": "mm",
    "visibility_m": "m",
    "surface_pressure_hpa": "hPa",
    "wind_speed_kmh": "km/h",
    "wind_gusts_kmh": "km/h",
    "wind_direction_deg": "deg",
}

FIELD_SEMANTICS = {
    "time_local": (
        "Provider-local wall-clock label in `timezone`, without UTC offset. Open-Meteo applies the single "
        "response-level `utc_offset_seconds` to every label, so after a DST change inside the forecast range "
        "labels differ from civil time by the DST delta. Use `time_utc` to identify the real instant."
    ),
    "time_utc": "Unambiguous UTC instant of the sample, from the provider's Unix timestamp.",
    "utc_offset_seconds": "The single UTC offset Open-Meteo used for all `time_local` labels in this response.",
    "provider_current_time_local": (
        "Provider-local time of the Open-Meteo current-conditions slot (the TSN DSS client's `generated_at`). "
        "It is NOT fetched_at, NOT a model run time and NOT a forecast generation time."
    ),
    "dew_margin_c": "Derived by TSN DSS: temperature_c - dew_point_c.",
    "dew_risk": "Derived by TSN DSS from dew_margin_c: high <= 2.0 degC, moderate <= 5.0 degC, otherwise low.",
    "visibility_m": "Atmospheric horizontal visibility from the provider. Not astronomical target visibility.",
    "wind_direction_deg": "Meteorological wind direction reported by the provider.",
    "condition_code": "Provider weather code (Open-Meteo `weather_code`, WMO interpretation code).",
    "is_day": "Provider day/night flag: 1 day, 0 night.",
}

EXCLUDED = [
    "astronomical_target_visibility",
    "twilight_darkness",
    "moon_constraints",
    "seeing",
    "transparency",
    "observability_ranking",
]

KNOWN_LIMITATIONS = [
    "current.precipitation_probability_pct is not requested from the provider for current conditions and is null.",
    "No forecast model name, model run time or fetched_at timestamp is recorded.",
]


class SiteForecastClient(Protocol):
    provider_name: str

    def fetch_site_forecast(self, site: Site, *, forecast_days: int = 1) -> SiteForecastSnapshot: ...


class ForecastProviderError(RuntimeError):
    """The TSN DSS forecast provider could not return a usable forecast."""


def resolve_forecast_site(connection: sqlite3.Connection, site_id: str) -> Site:
    """Resolve the Site and validate it can be forecast, using only the read-only database."""
    site = resolve_site(PlanningRepository(connection), site_id)
    if site.latitude_deg is None or site.longitude_deg is None:
        raise ValueError("Site must define latitude and longitude before forecast can be requested.")
    return site


def coerce_forecast_days(forecast_days: int) -> int:
    return max(MIN_FORECAST_DAYS, min(MAX_FORECAST_DAYS, int(forecast_days)))


def get_site_forecast(
    site: Site,
    *,
    weather_client: SiteForecastClient,
    forecast_days: int = DEFAULT_MCP_FORECAST_DAYS,
) -> dict[str, Any]:
    """Fetch the existing TSN DSS Site forecast. Performs an outbound HTTP read; no database access."""
    effective_days = coerce_forecast_days(forecast_days)
    try:
        snapshot = weather_client.fetch_site_forecast(site, forecast_days=effective_days)
    except (OSError, http.client.HTTPException) as error:
        raise ForecastProviderError(_describe_provider_failure(error)) from error
    except (ValueError, TypeError, AttributeError, KeyError) as error:
        raise ForecastProviderError(f"TSN DSS forecast provider returned a malformed response: {error}") from error

    return {"site_forecast": _serialize_forecast(site, snapshot, effective_days)}


def _serialize_forecast(site: Site, snapshot: SiteForecastSnapshot, forecast_days: int) -> dict[str, Any]:
    data = snapshot.to_dict()
    current = data["current"]
    return {
        "site": site_summary_to_dict(site),
        "provider": data["provider"],
        "timezone": data["timezone"],
        "utc_offset_seconds": data["utc_offset_seconds"],
        "time_semantics": "provider_local_wall_clock_without_utc_offset",
        "forecast_days": forecast_days,
        "provider_current_time_local": data["generated_at"],
        "current": None if current is None else _localize_time_key(current),
        "hourly": [_localize_time_key(hour) for hour in data["hourly"]],
        "units": dict(FORECAST_UNITS),
        "field_semantics": dict(FIELD_SEMANTICS),
        "network": {
            "live_external_request": True,
            "cache": False,
            "fallback_provider": False,
            "coordinates_sent_to_provider": {
                "latitude_deg": data["latitude_deg"],
                "longitude_deg": data["longitude_deg"],
                "elevation_m": site.elevation_m,
            },
        },
        "excluded": list(EXCLUDED),
        "known_limitations": list(KNOWN_LIMITATIONS),
    }


def _localize_time_key(entry: dict[str, Any]) -> dict[str, Any]:
    fields = dict(entry)
    return {"time_local": fields.pop("time"), **fields}


def _describe_provider_failure(error: BaseException) -> str:
    reason = getattr(error, "reason", None)
    if isinstance(error, TimeoutError) or isinstance(reason, TimeoutError):
        return "TSN DSS forecast provider request timed out."
    return f"TSN DSS forecast provider unavailable: {error}"
