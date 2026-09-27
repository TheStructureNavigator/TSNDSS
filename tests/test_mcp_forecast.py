from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from tsn_dss.domain.models import Site
from tsn_dss.engine.sqlite.db import initialize_database
from tsn_dss.engine.sqlite.planning import PlanningRepository
from tsn_dss.engine.weather import OpenMeteoForecastClient
from tsn_dss.mcp import bootstrap
from tsn_dss.mcp.server import create_mcp_server

try:
    from mcp.server.mcpserver.exceptions import ToolError
except ModuleNotFoundError:  # pragma: no cover - dependency is required for these tests
    ToolError = RuntimeError  # type: ignore[assignment,misc]


PROVIDER_PAYLOAD = {
    "timezone": "Europe/Warsaw",
    "current": {
        "time": "2026-09-27T20:15",
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
        "time": ["2026-09-27T00:00", "2026-09-27T01:00"],
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


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def _provider_response(payload: object) -> _FakeResponse:
    return _FakeResponse(json.dumps(payload).encode("utf-8"))


class McpSiteForecastTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "forecast.db"
        connection = initialize_database(self.db_path)
        try:
            repository = PlanningRepository(connection)
            repository.create_site(
                Site(id="site:kitchen", name="TSNKitchen", latitude_deg=52.1, longitude_deg=21.0, elevation_m=110.0)
            )
            repository.create_site(Site(id="site:dup-a", name="Twin Field", latitude_deg=50.0, longitude_deg=19.0))
            repository.create_site(Site(id="site:dup-b", name="TWIN FIELD", latitude_deg=50.1, longitude_deg=19.1))
            repository.create_site(Site(id="site:nocoords", name="Nowhere", latitude_deg=None, longitude_deg=None))
        finally:
            connection.close()
        # Real Open-Meteo client with its network boundary patched: no live network in these tests.
        self.urlopen_patcher = patch("tsn_dss.engine.weather.urlopen")
        self.urlopen = self.urlopen_patcher.start()
        self.urlopen.side_effect = lambda *args, **kwargs: _provider_response(PROVIDER_PAYLOAD)

    def tearDown(self) -> None:
        self.urlopen_patcher.stop()
        self.temp_dir.cleanup()

    def _call(self, arguments: dict, **server_kwargs):
        async def run():
            server = create_mcp_server(database_path=self.db_path, **server_kwargs)
            return await server.call_tool("get_site_forecast", arguments)

        return asyncio.run(run())

    def _requested_forecast_days(self) -> str:
        url = self.urlopen.call_args.args[0]
        return url.split("forecast_days=")[1].split("&")[0]

    def _database_fingerprint(self) -> dict[str, str]:
        directory = self.db_path.parent
        return {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(directory.iterdir())
        }

    def test_tool_is_registered(self) -> None:
        async def run():
            server = create_mcp_server(database_path=self.db_path)
            return {tool.name: tool for tool in await server.list_tools()}

        tools = asyncio.run(run())
        self.assertIn("get_site_forecast", tools)
        self.assertIn("Open-Meteo", tools["get_site_forecast"].description)

    def test_resolves_site_by_stable_id(self) -> None:
        result = self._call({"site_id": "site:kitchen"})

        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["site_forecast"]["site"]["id"], "site:kitchen")

    def test_resolves_site_by_exact_case_insensitive_name(self) -> None:
        result = self._call({"site_id": "tsnkitchen"})

        self.assertEqual(result.structured_content["site_forecast"]["site"]["id"], "site:kitchen")

    def test_does_not_fuzzy_match_site_name(self) -> None:
        with self.assertRaisesRegex(ToolError, "Unknown site_id"):
            self._call({"site_id": "Kitchen"})
        self.urlopen.assert_not_called()

    def test_unknown_site_is_rejected_without_network(self) -> None:
        with self.assertRaisesRegex(ToolError, "Unknown site_id: site:missing"):
            self._call({"site_id": "site:missing"})
        self.urlopen.assert_not_called()

    def test_empty_site_id_is_rejected(self) -> None:
        with self.assertRaisesRegex(ToolError, "site_id is required"):
            self._call({"site_id": "   "})
        self.urlopen.assert_not_called()

    def test_ambiguous_exact_site_name_is_rejected(self) -> None:
        with self.assertRaisesRegex(ToolError, "Site name is ambiguous"):
            self._call({"site_id": "twin field"})
        self.urlopen.assert_not_called()

    def test_site_without_coordinates_is_rejected_without_network(self) -> None:
        with self.assertRaisesRegex(ToolError, "latitude and longitude"):
            self._call({"site_id": "site:nocoords"})
        self.urlopen.assert_not_called()

    def test_default_forecast_days_is_two(self) -> None:
        result = self._call({"site_id": "site:kitchen"})

        self.assertEqual(self._requested_forecast_days(), "2")
        self.assertEqual(result.structured_content["site_forecast"]["forecast_days"], 2)

    def test_explicit_forecast_days_is_passed_through(self) -> None:
        result = self._call({"site_id": "site:kitchen", "forecast_days": 7})

        self.assertEqual(self._requested_forecast_days(), "7")
        self.assertEqual(result.structured_content["site_forecast"]["forecast_days"], 7)

    def test_forecast_days_is_clamped_like_existing_client(self) -> None:
        high = self._call({"site_id": "site:kitchen", "forecast_days": 40})
        self.assertEqual(self._requested_forecast_days(), "16")
        low = self._call({"site_id": "site:kitchen", "forecast_days": 0})
        self.assertEqual(self._requested_forecast_days(), "1")

        self.assertEqual(high.structured_content["site_forecast"]["forecast_days"], 16)
        self.assertEqual(low.structured_content["site_forecast"]["forecast_days"], 1)

    def test_successful_serialization(self) -> None:
        forecast = self._call({"site_id": "site:kitchen"}).structured_content["site_forecast"]

        self.assertEqual(forecast["provider"], "open-meteo")
        self.assertEqual(forecast["timezone"], "Europe/Warsaw")
        self.assertEqual(forecast["time_semantics"], "provider_local_wall_clock_without_utc_offset")
        self.assertEqual(forecast["provider_current_time_local"], "2026-09-27T20:15")
        self.assertNotIn("generated_at", forecast)
        self.assertIn("NOT fetched_at", forecast["field_semantics"]["provider_current_time_local"])

        current = forecast["current"]
        self.assertEqual(current["time_local"], "2026-09-27T20:15")
        self.assertNotIn("time", current)
        self.assertNotIn("time_utc", current)
        self.assertEqual(current["cloud_cover_pct"], 22.0)
        self.assertEqual(current["wind_direction_deg"], 225.0)
        self.assertEqual(current["condition_code"], 1)

        self.assertEqual([hour["time_local"] for hour in forecast["hourly"]], ["2026-09-27T00:00", "2026-09-27T01:00"])
        self.assertEqual(forecast["hourly"][1]["precipitation_probability_pct"], 15.0)
        self.assertTrue(all("time_utc" not in hour for hour in forecast["hourly"]))

        self.assertEqual(forecast["units"]["wind_speed_kmh"], "km/h")
        self.assertEqual(forecast["units"]["surface_pressure_hpa"], "hPa")
        self.assertEqual(forecast["network"]["coordinates_sent_to_provider"]["latitude_deg"], 52.1)
        self.assertTrue(forecast["network"]["live_external_request"])
        self.assertFalse(forecast["network"]["cache"])
        self.assertFalse(forecast["network"]["fallback_provider"])
        for excluded in ("astronomical_target_visibility", "twilight_darkness", "moon_constraints", "seeing",
                         "transparency", "observability_ranking"):
            self.assertIn(excluded, forecast["excluded"])
        self.assertNotIn("assessment", json.dumps(forecast).lower())

    def test_dew_fields_are_exposed(self) -> None:
        forecast = self._call({"site_id": "site:kitchen"}).structured_content["site_forecast"]

        self.assertAlmostEqual(forecast["current"]["dew_margin_c"], 2.3, places=3)
        self.assertEqual(forecast["current"]["dew_risk"], "moderate")
        self.assertEqual(forecast["hourly"][1]["dew_risk"], "high")
        self.assertIn("temperature_c - dew_point_c", forecast["field_semantics"]["dew_margin_c"])

    def test_provider_network_error_becomes_tool_error(self) -> None:
        self.urlopen.side_effect = URLError("Name or service not known")

        with self.assertRaisesRegex(ToolError, "forecast provider unavailable"):
            self._call({"site_id": "site:kitchen"})

    def test_provider_http_error_becomes_tool_error(self) -> None:
        self.urlopen.side_effect = HTTPError("https://api.open-meteo.com", 503, "Service Unavailable", {}, None)

        with self.assertRaisesRegex(ToolError, "forecast provider unavailable"):
            self._call({"site_id": "site:kitchen"})

    def test_provider_timeout_becomes_tool_error(self) -> None:
        for error in (TimeoutError("timed out"), URLError(TimeoutError("timed out"))):
            with self.subTest(error=type(error).__name__):
                self.urlopen.side_effect = error
                with self.assertRaisesRegex(ToolError, "timed out"):
                    self._call({"site_id": "site:kitchen"})

    def test_malformed_provider_response_becomes_tool_error(self) -> None:
        bad_responses = [
            lambda *args, **kwargs: _FakeResponse(b"<html>not json</html>"),
            lambda *args, **kwargs: _provider_response(["not", "an", "object"]),
            lambda *args, **kwargs: _provider_response({"current": {"temperature_2m": "warm"}}),
        ]
        for side_effect in bad_responses:
            with self.subTest():
                self.urlopen.side_effect = side_effect
                with self.assertRaisesRegex(ToolError, "malformed response"):
                    self._call({"site_id": "site:kitchen"})

    def test_database_is_unchanged(self) -> None:
        before = self._database_fingerprint()

        self._call({"site_id": "site:kitchen"})
        self._call({"site_id": "TSNKitchen", "forecast_days": 16})
        with self.assertRaises(ToolError):
            self._call({"site_id": "site:missing"})

        self.assertEqual(self._database_fingerprint(), before)

    def test_database_connection_is_closed_before_network_fetch(self) -> None:
        opened: list[sqlite3.Connection] = []
        original_open = bootstrap.open_readonly_database

        def recording_open(**kwargs):
            connection = original_open(**kwargs)
            opened.append(connection)
            return connection

        states_during_fetch: list[bool] = []

        class ProbingClient:
            provider_name = "open-meteo"

            def fetch_site_forecast(self, site, *, forecast_days=1):
                states_during_fetch.extend(_is_closed(connection) for connection in opened)
                return OpenMeteoForecastClient().fetch_site_forecast(site, forecast_days=forecast_days)

        with patch.object(bootstrap, "open_readonly_database", side_effect=recording_open):
            result = self._call({"site_id": "site:kitchen"}, weather_client=ProbingClient())

        self.assertFalse(result.is_error)
        self.assertEqual(len(opened), 1)
        self.assertEqual(states_during_fetch, [True])


def _is_closed(connection: sqlite3.Connection) -> bool:
    try:
        connection.execute("SELECT 1;")
    except sqlite3.ProgrammingError:
        return True
    return False


if __name__ == "__main__":
    unittest.main()
