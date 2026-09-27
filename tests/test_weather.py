from __future__ import annotations

import io
import json
import unittest
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from tsn_dss.domain.models import Site
from tsn_dss.engine.weather import OpenMeteoForecastClient, calculate_dew_margin_c, classify_dew_risk


class WeatherClientTests(unittest.TestCase):
    def test_calculate_dew_margin_and_risk(self) -> None:
        self.assertEqual(calculate_dew_margin_c(10.0, 8.0), 2.0)
        self.assertEqual(classify_dew_risk(1.5), "high")
        self.assertEqual(classify_dew_risk(3.0), "moderate")
        self.assertEqual(classify_dew_risk(6.0), "low")
        self.assertIsNone(classify_dew_risk(None))

    def test_fetch_site_forecast_maps_provider_payload_to_normalized_snapshot(self) -> None:
        site = Site(
            id="site:test",
            name="Remote Ridge",
            latitude_deg=49.245,
            longitude_deg=22.511,
            elevation_m=640.0,
            sqm_mag_arcsec2=None,
            bortle_class=None,
            south_horizon_open=False,
            notes=None,
        )
        payload = {
            "timezone": "Europe/Warsaw",
            "utc_offset_seconds": 7200,
            "current": {
                "time": _epoch("2026-08-30T18:00:00Z"),
                "temperature_2m": 11.2,
                "relative_humidity_2m": 78,
                "dew_point_2m": 8.9,
                "apparent_temperature": 10.1,
                "cloud_cover": 22,
                "cloud_cover_low": 8,
                "cloud_cover_mid": 12,
                "cloud_cover_high": 35,
                "visibility": 18000,
                "surface_pressure": 943.6,
                "wind_speed_10m": 9.4,
                "wind_direction_10m": 225,
                "wind_gusts_10m": 15.2,
                "precipitation": 0.0,
                "weather_code": 1,
                "is_day": 0,
            },
            "hourly": {
                "time": [_epoch("2026-08-30T18:00:00Z"), _epoch("2026-08-30T19:00:00Z")],
                "temperature_2m": [11.2, 10.6],
                "relative_humidity_2m": [78, 82],
                "dew_point_2m": [8.9, 8.8],
                "apparent_temperature": [10.1, 9.2],
                "cloud_cover": [22, 40],
                "cloud_cover_low": [8, 15],
                "cloud_cover_mid": [12, 20],
                "cloud_cover_high": [35, 62],
                "visibility": [18000, 16000],
                "surface_pressure": [943.6, 944.1],
                "wind_speed_10m": [9.4, 8.0],
                "wind_direction_10m": [225, 230],
                "wind_gusts_10m": [15.2, 13.3],
                "precipitation": [0.0, 0.1],
                "precipitation_probability": [5, 15],
                "weather_code": [1, 2],
                "is_day": [0, 0],
            },
        }

        with patch("tsn_dss.engine.weather.urlopen", return_value=_FakeResponse(payload)) as mocked_urlopen:
            snapshot = OpenMeteoForecastClient().fetch_site_forecast(site, forecast_days=16)

        self.assertEqual(snapshot.provider, "open-meteo")
        requested_query = parse_qs(urlsplit(mocked_urlopen.call_args.args[0]).query)
        self.assertEqual(requested_query["forecast_days"], ["16"])
        self.assertEqual(requested_query["timeformat"], ["unixtime"])
        self.assertEqual(snapshot.utc_offset_seconds, 7200)
        self.assertEqual(snapshot.current.time, "2026-08-30T20:00")
        self.assertEqual(snapshot.current.time_utc, "2026-08-30T18:00:00Z")
        self.assertEqual(snapshot.generated_at, "2026-08-30T20:00")
        self.assertEqual([hour.time for hour in snapshot.hourly], ["2026-08-30T20:00", "2026-08-30T21:00"])
        self.assertEqual([hour.time_utc for hour in snapshot.hourly], ["2026-08-30T18:00:00Z", "2026-08-30T19:00:00Z"])
        self.assertEqual(snapshot.to_dict()["hourly"][0]["time_utc"], "2026-08-30T18:00:00Z")
        self.assertEqual(snapshot.to_dict()["utc_offset_seconds"], 7200)
        self.assertNotIn("forecast_hours", requested_query)
        self.assertEqual(snapshot.site_id, site.id)
        self.assertAlmostEqual(snapshot.current.dew_margin_c or 0.0, 2.3, places=3)
        self.assertEqual(snapshot.current.dew_risk, "moderate")
        self.assertEqual(snapshot.current.cloud_cover_high_pct, 35.0)
        self.assertEqual(snapshot.current.visibility_m, 18000.0)
        self.assertEqual(snapshot.current.surface_pressure_hpa, 943.6)
        self.assertEqual(snapshot.current.wind_direction_deg, 225.0)
        self.assertEqual(snapshot.current.condition_code, 1)
        self.assertEqual(snapshot.current.is_day, 0)
        self.assertEqual(len(snapshot.hourly), 2)
        self.assertEqual(snapshot.hourly[1].dew_risk, "high")
        self.assertEqual(snapshot.hourly[1].cloud_cover_low_pct, 15.0)
        self.assertEqual(snapshot.hourly[1].precipitation_probability_pct, 15.0)

    def test_provider_local_labels_use_single_response_offset_across_dst(self) -> None:
        # Open-Meteo labels every hour with the response-level offset. Around the Europe/Warsaw
        # fall-back (2026-10-25 01:00Z) civil time repeats 02:00, but instants and labels stay distinct.
        instants = ["2026-10-24T23:00:00Z", "2026-10-25T00:00:00Z", "2026-10-25T01:00:00Z", "2026-10-25T02:00:00Z"]
        payload = {
            "timezone": "Europe/Warsaw",
            "utc_offset_seconds": 7200,
            "hourly": {"time": [_epoch(value) for value in instants], "temperature_2m": [5, 4, 3, 2]},
        }
        with patch("tsn_dss.engine.weather.urlopen", return_value=_FakeResponse(payload)):
            snapshot = OpenMeteoForecastClient().fetch_site_forecast(_site(), forecast_days=1)

        self.assertEqual([hour.time_utc for hour in snapshot.hourly], instants)
        self.assertEqual(
            [hour.time for hour in snapshot.hourly],
            ["2026-10-25T01:00", "2026-10-25T02:00", "2026-10-25T03:00", "2026-10-25T04:00"],
        )
        self.assertEqual(len({hour.time_utc for hour in snapshot.hourly}), 4)

    def test_missing_utc_offset_is_rejected_as_malformed(self) -> None:
        payload = {"timezone": "Europe/Warsaw", "hourly": {"time": [_epoch("2026-08-30T18:00:00Z")]}}
        with patch("tsn_dss.engine.weather.urlopen", return_value=_FakeResponse(payload)):
            with self.assertRaisesRegex(ValueError, "utc_offset_seconds"):
                OpenMeteoForecastClient().fetch_site_forecast(_site(), forecast_days=1)


def _epoch(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp())


def _site() -> Site:
    return Site(id="site:test", name="Remote Ridge", latitude_deg=49.245, longitude_deg=22.511)


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._buffer = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def read(self) -> bytes:
        return self._buffer.read()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
