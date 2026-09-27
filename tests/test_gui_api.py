from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from base64 import b64decode
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tsn_dss.domain.models import Target
from tsn_dss.engine.projects import ProjectStorage
from tsn_dss.engine.sqlite import PlanningRepository, connect_database
from tsn_dss.mcp.tools.visibility import target_visibility_windows as mcp_target_visibility_windows
from tsn_dss.gui.http_api import create_http_server


class FakeWeatherClient:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def fetch_site_forecast(self, site, *, forecast_days=1, past_days=0):
        self.requests.append({"site_id": site.id, "forecast_days": forecast_days, "past_days": past_days})
        return type(
            "ForecastPayload",
            (),
            {
                "to_dict": lambda self: {
                    "site_id": site.id,
                    "site_name": site.name,
                    "latitude_deg": site.latitude_deg,
                    "longitude_deg": site.longitude_deg,
                    "timezone": "Europe/Warsaw",
                    "generated_at": "2026-08-30T20:00",
                    "provider": "open-meteo",
                    "current": {
                        "time": "2026-08-30T20:00",
                        "temperature_c": 11.2,
                        "relative_humidity_pct": 78.0,
                        "dew_point_c": 8.9,
                        "dew_margin_c": 2.3,
                        "dew_risk": "moderate",
                        "apparent_temperature_c": 10.1,
                        "cloud_cover_pct": 22.0,
                        "cloud_cover_low_pct": 8.0,
                        "cloud_cover_mid_pct": 12.0,
                        "cloud_cover_high_pct": 35.0,
                        "visibility_m": 18000.0,
                        "surface_pressure_hpa": 943.6,
                        "wind_speed_kmh": 9.4,
                        "wind_direction_deg": 225.0,
                        "wind_gusts_kmh": 15.2,
                        "precipitation_mm": 0.0,
                        "precipitation_probability_pct": None,
                        "condition_code": 1,
                        "is_day": 0,
                    },
                    "hourly": [
                        {
                            "time": "2026-08-30T20:00",
                            "temperature_c": 11.2,
                            "relative_humidity_pct": 78.0,
                            "dew_point_c": 8.9,
                            "dew_margin_c": 2.3,
                            "dew_risk": "moderate",
                            "apparent_temperature_c": 10.1,
                            "cloud_cover_pct": 22.0,
                            "cloud_cover_low_pct": 8.0,
                            "cloud_cover_mid_pct": 12.0,
                            "cloud_cover_high_pct": 35.0,
                            "visibility_m": 18000.0,
                            "surface_pressure_hpa": 943.6,
                            "wind_speed_kmh": 9.4,
                            "wind_direction_deg": 225.0,
                            "wind_gusts_kmh": 15.2,
                            "precipitation_mm": 0.0,
                            "precipitation_probability_pct": 5.0,
                            "condition_code": 1,
                            "is_day": 0,
                        }
                    ],
                }
            },
        )()


class ProjectStorageListingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.projects_root = Path(self.temp_dir.name) / "projects"
        self.storage = ProjectStorage(self.projects_root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_list_projects_returns_capture_and_run_counts(self) -> None:
        layout = self.storage.ensure_project("orion_nebula")
        (layout.captures_dir / "OrionNebula").mkdir(parents=True, exist_ok=True)
        (layout.runs_dir / "processing_m42_v001").mkdir(parents=True, exist_ok=True)

        projects = self.storage.list_projects()

        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0].slug, "orion_nebula")
        self.assertEqual(projects[0].capture_names, ("OrionNebula",))
        self.assertEqual(projects[0].run_names, ("processing_m42_v001",))

    def test_get_project_returns_exact_project(self) -> None:
        self.storage.ensure_project("orion_nebula")

        project = self.storage.get_project("orion_nebula")

        assert project is not None
        self.assertEqual(project.slug, "orion_nebula")

    def test_create_project_creates_directories(self) -> None:
        project = self.storage.create_project("m31_new")

        self.assertEqual(project.slug, "m31_new")
        self.assertTrue((self.projects_root / "m31_new" / "captures").is_dir())
        self.assertTrue((self.projects_root / "m31_new" / "runs").is_dir())

    def test_set_project_sky_target_persists_metadata(self) -> None:
        self.storage.create_project("m31_new")

        project = self.storage.set_project_sky_target("m31_new", "M31")

        self.assertEqual(project.sky_target, "M31")
        self.assertTrue((self.projects_root / "m31_new" / "project.json").exists())


class GuiApiServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.projects_root = Path(self.temp_dir.name) / "projects"
        self.source_capture = Path(self.temp_dir.name) / "OrionNebula"
        for relative_path in ("biases/bias_001.CR2", "darks/dark_001.CR2", "flats/flat_001.CR2", "lights/light_001.CR2"):
            destination = self.source_capture / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("x", encoding="utf-8")
        preview_image = self.source_capture / "lights" / "light_preview.png"
        preview_image.write_bytes(
            b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jx5QAAAAASUVORK5CYII="
            )
        )
        self.fake_script = Path(self.temp_dir.name) / "OSC_Preprocessing.ssf"
        self.fake_script.write_text("# fake script\n", encoding="utf-8")
        self.fake_siril_cli = Path(self.temp_dir.name) / "fake_siril_cli.py"
        self.fake_siril_cli.write_text(
            "\n".join(
                [
                    "import argparse",
                    "import pathlib",
                    "import sys",
                    "import time",
                    "parser = argparse.ArgumentParser(add_help=False)",
                    "parser.add_argument('-d')",
                    "parser.add_argument('-s')",
                    "args = parser.parse_args()",
                    "workdir = pathlib.Path(args.d)",
                    "print('Converting lights...', flush=True)",
                    "time.sleep(0.05)",
                    "print('Calibration step...', flush=True)",
                    "time.sleep(0.05)",
                    "print('Registration step...', flush=True)",
                    "time.sleep(0.05)",
                    "print('Stacking step...', flush=True)",
                    "time.sleep(0.05)",
                    "(workdir / 'process').mkdir(parents=True, exist_ok=True)",
                    "(workdir / 'process' / 'stacking.tmp').write_text('large intermediate', encoding='utf-8')",
                    "print('result_120s.fit written', flush=True)",
                    "(workdir / 'result_120s.fit').write_text('fake fits', encoding='utf-8')",
                    "sys.exit(0)",
                ]
            ),
            encoding="utf-8",
        )
        storage = ProjectStorage(self.projects_root)
        layout = storage.ensure_project("orion_nebula")
        self._copy_capture_tree(self.source_capture, layout.captures_dir / "OrionNebula")
        (layout.runs_dir / "processing_m42_v001").mkdir(parents=True, exist_ok=True)

        self.weather_client = FakeWeatherClient()
        self.server = create_http_server(
            host="127.0.0.1",
            port=0,
            projects_root=self.projects_root,
            weather_client=self.weather_client,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp_dir.cleanup()

    def test_health_endpoint_reports_ok(self) -> None:
        payload = self._read_json("/api/health")

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["service"], "tsn-dss-api")
        self.assertEqual(payload["projects_root"], str(self.projects_root.resolve()))

    def test_core_content_endpoint_returns_versioned_frontend_content(self) -> None:
        payload = self._read_json("/api/core-content")

        self.assertGreaterEqual(len(payload["releases"]), 1)
        self.assertEqual(payload["current_version"], payload["releases"][0]["version"])
        self.assertIn("todo", payload)

    def test_light_pollution_endpoint_reports_missing_local_dataset(self) -> None:
        payload = self._read_json("/api/light-pollution?lat=50.0&lon=18.0")

        light_pollution = payload["light_pollution"]
        self.assertEqual(light_pollution["status"], "dataset unavailable")
        self.assertEqual(light_pollution["dataset_name"], "New World Atlas")
        self.assertEqual(light_pollution["source"], "Falchi et al. 2016")
        self.assertIsNone(light_pollution["measurement"])

    def test_light_pollution_endpoint_rejects_invalid_coordinates(self) -> None:
        with self.assertRaises(HTTPError) as context:
            self._read_json("/api/light-pollution?lat=95.0&lon=18.0")

        self.assertEqual(context.exception.code, 400)

    def test_sites_endpoint_supports_crud_and_active_site(self) -> None:
        created = self._send_json(
            "/api/sites",
            {
                "name": "Bieszczady",
                "latitude_deg": 49.2,
                "longitude_deg": 22.5,
                "elevation_m": 640,
                "bortle_class": 2,
                "lp_artificial_brightness_mcd_m2": 0.1785,
                "lp_natural_sky_ratio": 1.04,
                "lp_estimated_total_brightness_mcd_m2": 0.3497,
                "lp_estimated_sqm_mag_arcsec2": 21.22,
                "lp_estimated_bortle_class": 4,
                "lp_dataset_name": "New World Atlas",
                "lp_provider_name": "local-raster",
                "lp_source": "Falchi et al. 2016",
                "lp_source_unit": "mcd/m²",
                "lp_data_kind": "modeled",
                "lp_updated_at": "2026-09-20T19:00:00Z",
                "horizon_profile": [
                    {"azimuth_deg": 180, "min_altitude_deg": 40},
                    {"azimuth_deg": 0, "min_altitude_deg": 12},
                ],
            },
        )
        site_id = created["site"]["id"]
        self.assertEqual(created["site"]["name"], "Bieszczady")
        self.assertEqual(created["site"]["lp_dataset_name"], "New World Atlas")
        self.assertEqual(created["site"]["lp_estimated_bortle_class"], 4)
        self.assertEqual(created["site"]["lp_updated_at"], "2026-09-20T19:00:00Z")
        self.assertEqual(
            created["site"]["horizon_profile"],
            [
                {"azimuth_deg": 0.0, "min_altitude_deg": 12.0},
                {"azimuth_deg": 180.0, "min_altitude_deg": 40.0},
            ],
        )

        listed = self._read_json("/api/sites")
        self.assertEqual(len(listed["sites"]), 1)
        self.assertIsNone(listed["active_site_id"])

        activated = self._send_json("/api/sites/active", {"site_id": site_id})
        self.assertEqual(activated["active_site_id"], site_id)
        self.assertEqual(activated["snapshot"]["active_site"]["name"], "Bieszczady")
        self.assertEqual(activated["snapshot"]["telescope_state"]["site_lat_deg"], 49.2)

        updated = self._send_json(
            f"/api/sites/{site_id}",
            {
                "name": "Bieszczady Remote",
                "sqm_mag_arcsec2": 21.7,
            },
        )
        self.assertEqual(updated["site"]["name"], "Bieszczady Remote")
        self.assertEqual(updated["site"]["sqm_mag_arcsec2"], 21.7)
        self.assertEqual(updated["site"]["lp_source"], "Falchi et al. 2016")
        self.assertEqual(len(updated["site"]["horizon_profile"]), 2)

        horizon_updated = self._send_json(
            f"/api/sites/{site_id}",
            {
                "horizon_profile": [
                    {"azimuth_deg": 90, "min_altitude_deg": 25},
                    {"azimuth_deg": 45, "min_altitude_deg": 18},
                ],
            },
        )
        self.assertEqual(
            horizon_updated["site"]["horizon_profile"],
            [
                {"azimuth_deg": 45.0, "min_altitude_deg": 18.0},
                {"azimuth_deg": 90.0, "min_altitude_deg": 25.0},
            ],
        )

        horizon_cleared = self._send_json(
            f"/api/sites/{site_id}",
            {
                "horizon_profile": [],
            },
        )
        self.assertEqual(horizon_cleared["site"]["horizon_profile"], [])

        lp_updated = self._send_json(
            f"/api/sites/{site_id}",
            {
                "lp_artificial_brightness_mcd_m2": 0.25,
                "lp_natural_sky_ratio": 1.46,
                "lp_estimated_total_brightness_mcd_m2": 0.4212,
                "lp_estimated_sqm_mag_arcsec2": 21.02,
                "lp_estimated_bortle_class": 4,
                "lp_dataset_name": "New World Atlas",
                "lp_provider_name": "local-raster",
                "lp_source": "Falchi et al. 2016",
                "lp_source_unit": "mcd/m²",
                "lp_data_kind": "modeled",
                "lp_updated_at": "2026-09-20T20:00:00Z",
            },
        )
        self.assertEqual(lp_updated["site"]["sqm_mag_arcsec2"], 21.7)
        self.assertEqual(lp_updated["site"]["bortle_class"], 2)
        self.assertEqual(lp_updated["site"]["lp_artificial_brightness_mcd_m2"], 0.25)
        self.assertEqual(lp_updated["site"]["lp_updated_at"], "2026-09-20T20:00:00Z")

        deleted = self._send_delete(f"/api/sites/{site_id}")
        self.assertTrue(deleted["deleted"])
        self.assertEqual(deleted["site_id"], site_id)

        listed_after_delete = self._read_json("/api/sites")
        self.assertEqual(listed_after_delete["sites"], [])
        self.assertIsNone(listed_after_delete["active_site_id"])

    def test_site_forecast_endpoint_returns_weather_for_selected_site(self) -> None:
        created = self._send_json(
            "/api/sites",
            {
                "name": "Remote Ridge",
                "latitude_deg": 49.245,
                "longitude_deg": 22.511,
                "elevation_m": 640,
            },
        )
        site_id = created["site"]["id"]

        payload = self._read_json(f"/api/site-forecast?site_id={site_id}&forecast_days=16")

        forecast = payload["forecast"]
        self.assertEqual(self.weather_client.requests[-1]["forecast_days"], 16)
        self.assertEqual(self.weather_client.requests[-1]["past_days"], 0)
        self.assertEqual(forecast["site_id"], site_id)
        self.assertEqual(forecast["provider"], "open-meteo")
        self.assertEqual(forecast["current"]["cloud_cover_pct"], 22.0)
        self.assertEqual(forecast["current"]["dew_risk"], "moderate")
        self.assertEqual(forecast["current"]["cloud_cover_high_pct"], 35.0)
        self.assertEqual(forecast["current"]["wind_direction_deg"], 225.0)
        self.assertEqual(len(forecast["hourly"]), 1)

    def test_site_forecast_endpoint_accepts_past_days(self) -> None:
        site_id = self._send_json(
            "/api/sites",
            {"name": "Remote Ridge", "latitude_deg": 49.245, "longitude_deg": 22.511},
        )["site"]["id"]

        self._read_json(f"/api/site-forecast?site_id={site_id}&forecast_days=2&past_days=1")
        self.assertEqual(self.weather_client.requests[-1], {"site_id": site_id, "forecast_days": 2, "past_days": 1})

        self._read_json(f"/api/site-forecast?site_id={site_id}&past_days=5")
        self.assertEqual(self.weather_client.requests[-1]["past_days"], 1)

        with self.assertRaises(HTTPError) as context:
            self._read_json(f"/api/site-forecast?site_id={site_id}&past_days=yesterday")
        self.assertEqual(context.exception.code, 400)
        self.assertEqual(json.loads(context.exception.read())["error"], "invalid_site_forecast_request")

    def test_site_forecast_endpoint_rejects_site_without_coordinates(self) -> None:
        created = self._send_json(
            "/api/sites",
            {
                "name": "Manual site only",
            },
        )
        site_id = created["site"]["id"]

        with self.assertRaises(HTTPError) as context:
            self._read_json(f"/api/site-forecast?site_id={site_id}")

        self.assertEqual(context.exception.code, 400)

    def test_astronomical_conditions_endpoint_returns_site_and_planned_target_context(self) -> None:
        created = self._send_json(
            "/api/sites",
            {
                "name": "Remote Ridge",
                "latitude_deg": 49.245,
                "longitude_deg": 22.511,
                "elevation_m": 640,
            },
        )
        site_id = created["site"]["id"]
        self._send_json("/api/sites/active", {"site_id": site_id})
        self._send_json(
            "/api/telescope/planned-pointing",
            {
                "ra_hours": 0.71231389,
                "dec_deg": 41.26875,
                "target_name": "M31",
                "source_kind": "manual",
            },
        )

        payload = self._read_json(
            f"/api/astronomical-conditions?site_id={site_id}&use_planned_pointing=1&time_utc=2026-10-15T20:00:00Z&forecast_hours=3"
        )

        conditions = payload["conditions"]
        self.assertEqual(conditions["site_id"], site_id)
        self.assertEqual(conditions["provider"], "tsn-dss-astronomy")
        self.assertEqual(conditions["target"]["target_name"], "M31")
        self.assertEqual(conditions["target"]["source_kind"], "manual")
        self.assertEqual(conditions["current"]["target"]["target_name"], "M31")
        self.assertEqual(len(conditions["hourly"]), 3)
        self.assertIn("moon_azimuth_deg", conditions["hourly"][0])
        self.assertNotIn("night", conditions)
        self.assertEqual(conditions["hourly"][0]["time_utc"], "2026-10-15T20:00:00Z")

    def test_astronomical_conditions_endpoint_night_scope(self) -> None:
        site_id = self._send_json(
            "/api/sites",
            {"name": "Remote Ridge", "latitude_deg": 49.245, "longitude_deg": 22.511},
        )["site"]["id"]

        payload = self._read_json(
            f"/api/astronomical-conditions?site_id={site_id}&scope=night&time_utc=2026-10-15T23:30:00Z&forecast_hours=3"
        )

        conditions = payload["conditions"]
        night = conditions["night"]
        self.assertEqual(night["status"], "ok")
        self.assertTrue(night["sunset_utc"].startswith("2026-10-15T"))
        self.assertTrue(night["sunrise_utc"].startswith("2026-10-16T"))
        self.assertLessEqual(conditions["hourly"][0]["time_utc"], night["sunset_utc"])
        self.assertGreaterEqual(conditions["hourly"][-1]["time_utc"], night["sunrise_utc"])
        self.assertGreater(len(conditions["hourly"]), 3)
        self.assertEqual(conditions["current"]["time_utc"], "2026-10-15T23:30:00Z")

        with self.assertRaises(HTTPError) as context:
            self._read_json(f"/api/astronomical-conditions?site_id={site_id}&scope=week")
        self.assertEqual(context.exception.code, 400)
        self.assertEqual(json.loads(context.exception.read())["error"], "invalid_astronomical_conditions_request")

    def test_astronomical_conditions_endpoint_rejects_missing_coordinates(self) -> None:
        created = self._send_json(
            "/api/sites",
            {
                "name": "Manual site only",
            },
        )
        site_id = created["site"]["id"]

        with self.assertRaises(HTTPError) as context:
            self._read_json(f"/api/astronomical-conditions?site_id={site_id}")

        self.assertEqual(context.exception.code, 400)

    def test_astronomical_conditions_endpoint_resolves_project_sky_target_name(self) -> None:
        connection = connect_database(self.projects_root / "tsn_dss.db")
        try:
            PlanningRepository(connection).create_target(
                Target(
                    id="target:m31",
                    catalog="Messier",
                    catalog_id="M31",
                    name="Andromeda Galaxy",
                    ra_deg=10.6847083,
                    dec_deg=41.26875,
                )
            )
        finally:
            connection.close()

        self._send_json(
            "/api/projects/orion_nebula/sky-target",
            {"sky_target": "M31"},
        )
        created = self._send_json(
            "/api/sites",
            {
                "name": "Remote Ridge",
                "latitude_deg": 49.245,
                "longitude_deg": 22.511,
                "elevation_m": 640,
            },
        )
        site_id = created["site"]["id"]

        payload = self._read_json(
            f"/api/astronomical-conditions?site_id={site_id}&target_name=M31&time_utc=2026-10-15T20:00:00Z&forecast_hours=2"
        )

        conditions = payload["conditions"]
        self.assertEqual(conditions["target"]["target_name"], "Andromeda Galaxy")
        self.assertEqual(conditions["target"]["source_kind"], "target")
        self.assertEqual(conditions["current"]["target"]["source_id"], "target:m31")

    def test_visibility_windows_endpoint_returns_geometric_window_without_weather(self) -> None:
        site_id = self._create_visibility_site()
        before_weather_requests = list(self.weather_client.requests)
        before_counts = self._planning_counts()

        payload = self._read_json(
            f"/api/visibility-windows?site_id={site_id}&start_time_utc=2026-10-15T18:00:00Z"
            "&end_time_utc=2026-10-15T19:00:00Z&target_ra_deg=10.6847083&target_dec_deg=41.26875"
            "&min_target_altitude_deg=20"
        )

        visibility = payload["visibility_windows"]
        self.assertEqual(visibility["site"]["id"], site_id)
        self.assertEqual(visibility["target"]["source_kind"], "manual")
        self.assertEqual(visibility["interval_start_utc"], "2026-10-15T18:00:00Z")
        self.assertEqual(visibility["interval_end_utc"], "2026-10-15T19:00:00Z")
        self.assertEqual(visibility["min_target_altitude_deg"], 20.0)
        self.assertEqual(len(visibility["windows"]), 1)
        self.assertTrue(visibility["windows"][0]["starts_at_interval_start"])
        self.assertTrue(visibility["windows"][0]["ends_at_interval_end"])
        self.assertIsNotNone(visibility["windows"][0]["max_altitude_deg"])
        self.assertIsNotNone(visibility["windows"][0]["max_altitude_time_utc"])
        self.assertIn("weather", visibility["excluded_constraints"])
        self.assertEqual(visibility["diagnostic_semantics"], "evaluation_grid_derived_not_continuous_proof")
        self.assertEqual(self.weather_client.requests, before_weather_requests)
        self.assertEqual(self._planning_counts(), before_counts)

    def test_visibility_windows_endpoint_returns_zero_windows(self) -> None:
        site_id = self._create_visibility_site()

        payload = self._read_json(
            f"/api/visibility-windows?site_id={site_id}&start_time_utc=2026-10-15T18:00:00Z"
            "&end_time_utc=2026-10-15T19:00:00Z&target_ra_deg=0&target_dec_deg=-80"
        )

        visibility = payload["visibility_windows"]
        self.assertEqual(visibility["windows"], [])
        self.assertFalse(visibility["diagnostics"]["any_visible"])

    def test_visibility_windows_endpoint_uses_local_horizon_blocking(self) -> None:
        site_id = self._create_visibility_site(
            horizon_profile=[
                {"azimuth_deg": 0, "min_altitude_deg": 89},
                {"azimuth_deg": 180, "min_altitude_deg": 89},
            ],
        )

        payload = self._read_json(
            f"/api/visibility-windows?site_id={site_id}&start_time_utc=2026-10-15T18:00:00Z"
            "&end_time_utc=2026-10-15T19:00:00Z&target_ra_deg=10.6847083&target_dec_deg=41.26875"
            "&min_target_altitude_deg=20"
        )

        diagnostics = payload["visibility_windows"]["diagnostics"]
        self.assertEqual(payload["visibility_windows"]["windows"], [])
        self.assertTrue(diagnostics["local_horizon_available"])
        self.assertTrue(diagnostics["any_altitude_constraints_satisfied"])
        self.assertFalse(diagnostics["any_clear_of_local_horizon"])
        self.assertFalse(diagnostics["any_visible"])

    def test_visibility_windows_endpoint_local_horizon_unblocks_inside_interval(self) -> None:
        site_id = self._create_visibility_site(
            horizon_profile=[
                {"azimuth_deg": 0, "min_altitude_deg": 0},
                {"azimuth_deg": 60, "min_altitude_deg": 0},
                {"azimuth_deg": 90, "min_altitude_deg": 89},
                {"azimuth_deg": 120, "min_altitude_deg": 0},
                {"azimuth_deg": 359, "min_altitude_deg": 0},
            ],
        )

        payload = self._read_json(
            f"/api/visibility-windows?site_id={site_id}&start_time_utc=2026-10-15T18:00:00Z"
            "&end_time_utc=2026-10-15T20:00:00Z&target_ra_deg=10.6847083&target_dec_deg=41.26875"
            "&min_target_altitude_deg=20"
        )

        visibility = payload["visibility_windows"]
        self.assertEqual(len(visibility["windows"]), 1)
        self.assertFalse(visibility["windows"][0]["starts_at_interval_start"])
        self.assertTrue(visibility["windows"][0]["ends_at_interval_end"])
        self.assertTrue(visibility["diagnostics"]["local_horizon_available"])
        self.assertTrue(visibility["diagnostics"]["any_visible"])

    def test_visibility_windows_endpoint_accepts_custom_minimum_altitude(self) -> None:
        site_id = self._create_visibility_site()

        payload = self._read_json(
            f"/api/visibility-windows?site_id={site_id}&start_time_utc=2026-10-15T18:00:00Z"
            "&end_time_utc=2026-10-15T19:00:00Z&target_ra_deg=10.6847083&target_dec_deg=41.26875"
            "&min_target_altitude_deg=85"
        )

        visibility = payload["visibility_windows"]
        self.assertEqual(visibility["min_target_altitude_deg"], 85.0)
        self.assertEqual(visibility["windows"], [])
        self.assertFalse(visibility["diagnostics"]["any_above_minimum_altitude"])

    def test_visibility_windows_endpoint_rejects_invalid_site_target_and_interval(self) -> None:
        site_id = self._create_visibility_site()

        with self.assertRaises(HTTPError) as missing_site:
            self._read_json(
                "/api/visibility-windows?site_id=site:missing&start_time_utc=2026-10-15T18:00:00Z"
                "&end_time_utc=2026-10-15T19:00:00Z&target_ra_deg=10&target_dec_deg=20"
            )
        self.assertEqual(missing_site.exception.code, 404)

        with self.assertRaises(HTTPError) as unresolved:
            self._read_json(
                f"/api/visibility-windows?site_id={site_id}&start_time_utc=2026-10-15T18:00:00Z"
                "&end_time_utc=2026-10-15T19:00:00Z&target_name=NoSuchTarget"
            )
        self.assertEqual(unresolved.exception.code, 400)
        self.assertEqual(json.loads(unresolved.exception.read())["error"], "invalid_visibility_windows_request")

        with self.assertRaises(HTTPError) as invalid_time:
            self._read_json(
                f"/api/visibility-windows?site_id={site_id}&start_time_utc=not-a-time"
                "&end_time_utc=2026-10-15T19:00:00Z&target_ra_deg=10&target_dec_deg=20"
            )
        self.assertEqual(invalid_time.exception.code, 400)

        with self.assertRaises(HTTPError) as reversed_interval:
            self._read_json(
                f"/api/visibility-windows?site_id={site_id}&start_time_utc=2026-10-15T19:00:00Z"
                "&end_time_utc=2026-10-15T18:00:00Z&target_ra_deg=10&target_dec_deg=20"
            )
        self.assertEqual(reversed_interval.exception.code, 400)

    def test_visibility_windows_endpoint_resolves_existing_target_contexts(self) -> None:
        connection = connect_database(self.projects_root / "tsn_dss.db")
        try:
            PlanningRepository(connection).create_target(
                Target(
                    id="target:m31",
                    catalog="Messier",
                    catalog_id="M31",
                    name="Andromeda Galaxy",
                    ra_deg=10.6847083,
                    dec_deg=41.26875,
                )
            )
        finally:
            connection.close()
        site_id = self._create_visibility_site()

        by_target = self._read_json(
            f"/api/visibility-windows?site_id={site_id}&start_time_utc=2026-10-15T18:00:00Z"
            "&end_time_utc=2026-10-15T19:00:00Z&target_id=target:m31"
        )["visibility_windows"]
        self.assertEqual(by_target["target"]["source_id"], "target:m31")

        self._send_json(
            "/api/telescope/planned-pointing",
            {"ra_hours": 0.71231389, "dec_deg": 41.26875, "target_name": "M31", "source_kind": "manual"},
        )
        by_planned = self._read_json(
            f"/api/visibility-windows?site_id={site_id}&start_time_utc=2026-10-15T18:00:00Z"
            "&end_time_utc=2026-10-15T19:00:00Z&use_planned_pointing=1"
        )["visibility_windows"]
        self.assertEqual(by_planned["target"]["target_name"], "M31")
        self.assertEqual(by_planned["target"]["source_kind"], "manual")

    def test_visibility_windows_endpoint_matches_mcp_domain_payload(self) -> None:
        site_id = self._create_visibility_site()
        path = (
            f"/api/visibility-windows?site_id={site_id}&start_time_utc=2026-10-15T18:00:00Z"
            "&end_time_utc=2026-10-15T19:00:00Z&target_ra_deg=10.6847083&target_dec_deg=41.26875"
            "&min_target_altitude_deg=20"
        )
        http_visibility = self._read_json(path)["visibility_windows"]

        connection = connect_database(self.projects_root / "tsn_dss.db")
        try:
            mcp_visibility = mcp_target_visibility_windows(
                connection,
                site_id=site_id,
                start_time_utc="2026-10-15T18:00:00Z",
                end_time_utc="2026-10-15T19:00:00Z",
                target_ra_deg=10.6847083,
                target_dec_deg=41.26875,
                min_target_altitude_deg=20.0,
            )["visibility_windows"]
        finally:
            connection.close()

        for key in ("interval_start_utc", "interval_end_utc", "min_target_altitude_deg", "windows", "diagnostics"):
            self.assertEqual(http_visibility[key], mcp_visibility[key])

    def test_telescope_adapters_endpoint_lists_simulator_and_seestar(self) -> None:
        payload = self._read_json("/api/telescope/adapters")

        self.assertEqual(payload["active_adapter_id"], "simulator")
        self.assertEqual([adapter["adapter_id"] for adapter in payload["adapters"]], ["seestar", "simulator"])
        seestar = payload["adapters"][0]
        self.assertEqual(seestar["source_kind"], "seestar")
        self.assertTrue(seestar["capabilities"]["can_slew_to_coordinates"])
        self.assertTrue(seestar["capabilities"]["can_stream_preview"])

    def test_active_telescope_adapter_can_be_switched_via_api(self) -> None:
        payload = self._send_json("/api/telescope/active-adapter", {"adapter_id": "seestar"})

        self.assertEqual(payload["active_adapter_id"], "seestar")
        self.assertEqual(payload["snapshot"]["telescope_state"]["adapter_id"], "seestar")
        self.assertEqual(payload["snapshot"]["telescope_state"]["source_kind"], "seestar")
        self.assertEqual(payload["snapshot"]["imaging_profile"]["profile_id"], "seestar_s30_pro_tele")
        self.assertFalse(payload["capabilities"]["can_manual_pointing"])

    def test_switching_active_adapter_rejects_unknown_id(self) -> None:
        with self.assertRaises(HTTPError) as context:
            self._send_json("/api/telescope/active-adapter", {"adapter_id": "unknown"})

        self.assertEqual(context.exception.code, 400)

    def test_simulator_update_is_rejected_when_non_manual_adapter_is_active(self) -> None:
        self._send_json("/api/telescope/active-adapter", {"adapter_id": "seestar"})

        with self.assertRaises(HTTPError) as context:
            self._send_json("/api/telescope/simulator/state", {"ra_hours": 5.5, "dec_deg": -5.4})

        self.assertEqual(context.exception.code, 400)

    def test_projects_endpoint_lists_local_projects(self) -> None:
        payload = self._read_json("/api/projects")

        self.assertEqual(len(payload["projects"]), 1)
        project = payload["projects"][0]
        self.assertEqual(project["slug"], "orion_nebula")
        self.assertEqual(project["capture_count"], 1)
        self.assertEqual(project["run_count"], 1)
        self.assertEqual(project["capture_names"], ["OrionNebula"])
        self.assertEqual(project["run_names"], ["processing_m42_v001"])

    def test_project_detail_endpoint_returns_selected_project(self) -> None:
        payload = self._read_json("/api/projects/orion_nebula")

        project = payload["project"]
        self.assertEqual(project["slug"], "orion_nebula")
        self.assertEqual(project["captures_dir"], str((self.projects_root / "orion_nebula" / "captures")))
        self.assertEqual(project["runs_dir"], str((self.projects_root / "orion_nebula" / "runs")))
        self.assertIsNone(project["sky_target"])

    def test_capture_detail_endpoint_returns_folder_structure(self) -> None:
        payload = self._read_json("/api/projects/orion_nebula/captures/OrionNebula")

        capture = payload["capture"]
        self.assertEqual(capture["project_slug"], "orion_nebula")
        self.assertEqual(capture["capture_name"], "OrionNebula")
        self.assertEqual(len(capture["folders"]), 4)
        lights_folder = next(folder for folder in capture["folders"] if folder["name"] == "lights")
        self.assertEqual(lights_folder["file_count"], 2)
        self.assertEqual(lights_folder["files"][0]["name"], "light_001.CR2")

    def test_capture_file_endpoint_serves_capture_file(self) -> None:
        response = self._read_response("/api/projects/orion_nebula/captures/OrionNebula/files/lights/light_001.CR2")

        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), b"x")

    def test_capture_thumbnail_endpoint_serves_cached_jpeg_thumbnail(self) -> None:
        response = self._read_response(
            "/api/projects/orion_nebula/captures/OrionNebula/thumbnails/lights/light_preview.png?size=256"
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers.get_content_type(), "image/jpeg")
        body = response.read()
        self.assertGreater(len(body), 0)

        cache_root = self.projects_root / "orion_nebula" / ".cache" / "capture_thumbnails" / "OrionNebula" / "lights"
        cached_files = list(cache_root.glob("light_preview__*.jpg"))
        self.assertEqual(len(cached_files), 1)

    def test_create_project_endpoint_creates_new_project(self) -> None:
        payload = self._send_json("/api/projects", {"slug": "m42_gui"})

        project = payload["project"]
        self.assertEqual(project["slug"], "m42_gui")
        self.assertTrue((self.projects_root / "m42_gui" / "captures").is_dir())
        self.assertTrue((self.projects_root / "m42_gui" / "runs").is_dir())

    def test_delete_project_endpoint_removes_entire_project_tree(self) -> None:
        self.assertTrue((self.projects_root / "orion_nebula").exists())

        payload = self._send_delete("/api/projects/orion_nebula")

        self.assertTrue(payload["deleted"])
        self.assertEqual(payload["project_slug"], "orion_nebula")
        self.assertFalse((self.projects_root / "orion_nebula").exists())

        projects_payload = self._read_json("/api/projects")
        self.assertEqual(projects_payload["projects"], [])

    def test_import_capture_endpoint_copies_capture_into_project(self) -> None:
        self._send_json("/api/projects", {"slug": "m42_gui"})

        payload = self._send_json(
            "/api/import-capture",
            {
                "project_slug": "m42_gui",
                "capture_name": "OrionNebula",
                "source_dir": str(self.source_capture),
                "move": False,
            },
        )

        project = payload["project"]
        self.assertEqual(project["slug"], "m42_gui")
        self.assertTrue(
            (self.projects_root / "m42_gui" / "captures" / "OrionNebula" / "lights" / "light_001.CR2").exists()
        )
        self.assertTrue(self.source_capture.exists())

    def test_project_run_endpoint_executes_and_reports_logs(self) -> None:
        payload = self._send_json(
            "/api/project-runs",
            {
                "project_slug": "orion_nebula",
                "capture_name": "OrionNebula",
                "script_path": str(self.fake_script),
                "executable_parts": [sys.executable, str(self.fake_siril_cli)],
            },
        )

        run_id = payload["run"]["id"]
        terminal_payload = self._wait_for_run(run_id)
        run = terminal_payload["run"]

        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["progress_pct"], 100)
        self.assertIn("Stacking step", run["combined_log"])
        self.assertTrue(Path(run["output_path"]).exists())
        self.assertTrue(Path(run["status_path"]).exists())
        self.assertTrue(Path(run["stdout_log_path"]).exists())

    def test_project_runs_can_be_listed(self) -> None:
        self._send_json(
            "/api/project-runs",
            {
                "project_slug": "orion_nebula",
                "capture_name": "OrionNebula",
                "script_path": str(self.fake_script),
                "executable_parts": [sys.executable, str(self.fake_siril_cli)],
            },
        )

        payload = self._read_json("/api/project-runs?project_slug=orion_nebula")

        self.assertGreaterEqual(len(payload["runs"]), 1)
        self.assertEqual(payload["runs"][0]["project_slug"], "orion_nebula")

    def test_project_runs_can_be_filtered_by_capture_name(self) -> None:
        second_capture_root = self.projects_root / "orion_nebula" / "captures" / "OrionNebulaWide"
        self._copy_capture_tree(self.source_capture, second_capture_root)

        self._send_json(
            "/api/project-runs",
            {
                "project_slug": "orion_nebula",
                "capture_name": "OrionNebula",
                "script_path": str(self.fake_script),
                "executable_parts": [sys.executable, str(self.fake_siril_cli)],
            },
        )
        self._send_json(
            "/api/project-runs",
            {
                "project_slug": "orion_nebula",
                "capture_name": "OrionNebulaWide",
                "script_path": str(self.fake_script),
                "executable_parts": [sys.executable, str(self.fake_siril_cli)],
            },
        )

        payload = self._read_json("/api/project-runs?project_slug=orion_nebula&capture_name=OrionNebulaWide")

        self.assertEqual(len(payload["runs"]), 1)
        self.assertEqual(payload["runs"][0]["capture_name"], "OrionNebulaWide")

    def test_project_run_removes_process_directory_by_default(self) -> None:
        payload = self._send_json(
            "/api/project-runs",
            {
                "project_slug": "orion_nebula",
                "capture_name": "OrionNebula",
                "script_path": str(self.fake_script),
                "executable_parts": [sys.executable, str(self.fake_siril_cli)],
            },
        )

        run = self._wait_for_run(payload["run"]["id"])["run"]
        process_dir = Path(run["workspace_dir"]) / "process"

        self.assertFalse(process_dir.exists())

    def test_project_run_can_keep_process_directory_when_requested(self) -> None:
        payload = self._send_json(
            "/api/project-runs",
            {
                "project_slug": "orion_nebula",
                "capture_name": "OrionNebula",
                "script_path": str(self.fake_script),
                "executable_parts": [sys.executable, str(self.fake_siril_cli)],
                "keep_process_dir": True,
            },
        )

        run = self._wait_for_run(payload["run"]["id"])["run"]
        process_dir = Path(run["workspace_dir"]) / "process"

        self.assertTrue(process_dir.exists())

    def test_project_run_output_endpoint_serves_fits_artifact(self) -> None:
        payload = self._send_json(
            "/api/project-runs",
            {
                "project_slug": "orion_nebula",
                "capture_name": "OrionNebula",
                "script_path": str(self.fake_script),
                "executable_parts": [sys.executable, str(self.fake_siril_cli)],
            },
        )

        run_id = payload["run"]["id"]
        self._wait_for_run(run_id)

        response = self._read_response(f"/api/project-runs/{run_id}/output")

        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers.get_content_type(), "application/octet-stream")
        self.assertEqual(response.read(), b"fake fits")

    def test_project_run_lists_and_serves_user_edited_artifact_images(self) -> None:
        payload = self._send_json(
            "/api/project-runs",
            {
                "project_slug": "orion_nebula",
                "capture_name": "OrionNebula",
                "script_path": str(self.fake_script),
                "executable_parts": [sys.executable, str(self.fake_siril_cli)],
            },
        )

        run_id = payload["run"]["id"]
        completed_payload = self._wait_for_run(run_id)
        run = completed_payload["run"]
        artifacts_dir = Path(run["artifacts_dir"])
        edited_image_path = artifacts_dir / "m42_edited.png"
        edited_image_path.write_bytes(
            b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jx5QAAAAASUVORK5CYII="
            )
        )

        refreshed_payload = self._read_json(f"/api/project-runs/{run_id}")
        refreshed_run = refreshed_payload["run"]
        artifact_images = refreshed_run["artifact_images"]
        self.assertEqual(len(artifact_images), 1)
        self.assertEqual(artifact_images[0]["name"], "m42_edited.png")

        response = self._read_response(f"/api/project-runs/{run_id}/artifacts/m42_edited.png")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers.get_content_type(), "image/png")

    def test_generate_preview_endpoint_returns_preview_error_and_log(self) -> None:
        payload = self._send_json(
            "/api/project-runs",
            {
                "project_slug": "orion_nebula",
                "capture_name": "OrionNebula",
                "script_path": str(self.fake_script),
                "executable_parts": [sys.executable, str(self.fake_siril_cli)],
            },
        )

        run_id = payload["run"]["id"]
        self._wait_for_run(run_id)

        retry_payload = self._send_json(f"/api/project-runs/{run_id}/generate-preview", {})
        run = retry_payload["run"]

        self.assertIsNone(run["preview_path"])
        self.assertIn("not Siril", run["preview_error"])
        self.assertTrue(Path(run["preview_log_path"]).exists())

        response = self._read_response(f"/api/project-runs/{run_id}/preview-log")
        self.assertEqual(response.status, 200)
        self.assertIn("Preview export skipped", response.read().decode("utf-8"))

    def test_delete_run_endpoint_removes_run_tree_and_unregisters_run(self) -> None:
        payload = self._send_json(
            "/api/project-runs",
            {
                "project_slug": "orion_nebula",
                "capture_name": "OrionNebula",
                "script_path": str(self.fake_script),
                "executable_parts": [sys.executable, str(self.fake_siril_cli)],
            },
        )

        run_id = payload["run"]["id"]
        completed = self._wait_for_run(run_id)
        run_root = Path(completed["run"]["status_path"]).parent
        self.assertTrue(run_root.exists())

        delete_payload = self._send_delete(f"/api/project-runs/{run_id}")
        self.assertTrue(delete_payload["deleted"])
        self.assertEqual(delete_payload["run_id"], run_id)
        self.assertFalse(run_root.exists())

        runs_payload = self._read_json("/api/project-runs?project_slug=orion_nebula")
        self.assertEqual(runs_payload["runs"], [])

    def test_completed_runs_are_restored_after_server_restart(self) -> None:
        payload = self._send_json(
            "/api/project-runs",
            {
                "project_slug": "orion_nebula",
                "capture_name": "OrionNebula",
                "script_path": str(self.fake_script),
                "executable_parts": [sys.executable, str(self.fake_siril_cli)],
            },
        )

        run_id = payload["run"]["id"]
        self._wait_for_run(run_id)

        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

        self.server = create_http_server(
            host="127.0.0.1",
            port=0,
            projects_root=self.projects_root,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

        restored_payload = self._read_json("/api/project-runs?project_slug=orion_nebula")
        restored_runs = restored_payload["runs"]
        self.assertEqual(restored_runs[0]["id"], run_id)
        self.assertEqual(restored_runs[0]["status"], "completed")

        retry_payload = self._send_json(f"/api/project-runs/{run_id}/generate-preview", {})
        self.assertEqual(retry_payload["run"]["id"], run_id)

    def test_project_sky_target_can_be_updated_via_api(self) -> None:
        payload = self._send_json(
            "/api/projects/orion_nebula/sky-target",
            {"sky_target": "M42"},
        )

        project = payload["project"]
        self.assertEqual(project["sky_target"], "M42")

        detail = self._read_json("/api/projects/orion_nebula")
        self.assertEqual(detail["project"]["sky_target"], "M42")

    def test_mosaic_plan_can_be_created_and_listed_via_api(self) -> None:
        payload = self._send_json(
            "/api/mosaics",
            {
                "project_slug": "orion_nebula",
                "name": "Cygnus Loop",
                "target_name": "Cygnus Loop",
                "observation_type": "dual-band imaging",
                "filter": "L-eXtreme",
                "imaging_profile_id": "seestar_s30_pro",
                "imaging_profile_label": "Seestar S30 Pro",
                "fov_width_deg": 2.59,
                "fov_height_deg": 1.47,
                "center_ra_deg": 312.5,
                "center_dec_deg": 31.0,
                "region_width_deg": 3.0,
                "region_height_deg": 3.0,
                "overlap_percent": 25.0,
            },
        )

        created = payload["mosaic"]
        self.assertEqual(created["project_slug"], "orion_nebula")
        self.assertEqual(created["name"], "Cygnus Loop")
        self.assertEqual(created["observation_type"], "dual-band imaging")
        self.assertEqual(created["filter"], "L-eXtreme")

        listed = self._read_json("/api/mosaics?project_slug=orion_nebula")
        self.assertEqual(len(listed["mosaics"]), 1)
        self.assertEqual(listed["mosaics"][0]["id"], created["id"])
        self.assertEqual(listed["mosaics"][0]["observation_type"], "dual-band imaging")
        self.assertEqual(listed["mosaics"][0]["filter"], "L-eXtreme")

    def test_mosaic_plan_can_be_updated_via_api(self) -> None:
        created = self._send_json(
            "/api/mosaics",
            {
                "project_slug": "orion_nebula",
                "name": "Rosette",
                "target_name": "Rosette Nebula",
                "imaging_profile_id": "seestar_s30_pro",
                "imaging_profile_label": "Seestar S30 Pro",
                "fov_width_deg": 2.59,
                "fov_height_deg": 1.47,
                "center_ra_deg": 97.0,
                "center_dec_deg": 4.95,
                "region_width_deg": 3.0,
                "region_height_deg": 3.0,
                "overlap_percent": 25.0,
            },
        )["mosaic"]

        updated = self._send_json(
            f"/api/mosaics/{created['id']}",
            {
                "observation_type": "broadband imaging",
                "filter": "UV/IR Cut",
                "status": "ready",
            },
        )["mosaic"]

        self.assertEqual(updated["observation_type"], "broadband imaging")
        self.assertEqual(updated["filter"], "UV/IR Cut")
        self.assertEqual(updated["status"], "ready")

        reloaded = self._read_json(f"/api/mosaics/{created['id']}")["mosaic"]
        self.assertEqual(reloaded["observation_type"], "broadband imaging")
        self.assertEqual(reloaded["filter"], "UV/IR Cut")

    def test_mosaic_panels_can_be_generated_and_selected_via_api(self) -> None:
        payload = self._send_json(
            "/api/mosaics",
            {
                "project_slug": "orion_nebula",
                "name": "Cygnus Loop",
                "target_name": "Cygnus Loop",
                "imaging_profile_id": "seestar_s30_pro",
                "imaging_profile_label": "Seestar S30 Pro",
                "fov_width_deg": 2.59,
                "fov_height_deg": 1.47,
                "center_ra_deg": 312.5,
                "center_dec_deg": 31.0,
                "region_width_deg": 3.0,
                "region_height_deg": 3.0,
                "overlap_percent": 25.0,
            },
        )
        mosaic_id = payload["mosaic"]["id"]

        generated = self._send_json(f"/api/mosaics/{mosaic_id}/generate-panels", {})
        panels = generated["panels"]
        self.assertEqual(len(panels), 6)

        selected_panel_id = panels[-1]["id"]
        selected = self._send_json(
            f"/api/mosaics/{mosaic_id}/select-panel",
            {"panel_id": selected_panel_id},
        )
        self.assertEqual(selected["mosaic"]["selected_panel_id"], selected_panel_id)

        panel_payload = self._read_json(f"/api/mosaic-panels/{selected_panel_id}")
        self.assertEqual(panel_payload["panel"]["id"], selected_panel_id)

    def _read_json(self, path: str) -> dict[str, object]:
        with self._read_response(path) as response:
            return json.loads(response.read().decode("utf-8"))

    def _read_response(self, path: str):
        return urlopen(f"{self.base_url}{path}")

    def _send_json(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))

    def _send_delete(self, path: str) -> dict[str, object]:
        request = Request(
            f"{self.base_url}{path}",
            method="DELETE",
        )
        with urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))

    def _create_visibility_site(self, *, horizon_profile: list[dict[str, float]] | None = None) -> str:
        return self._send_json(
            "/api/sites",
            {
                "name": "Visibility Ridge",
                "latitude_deg": 49.245,
                "longitude_deg": 22.511,
                "elevation_m": 640,
                "horizon_profile": horizon_profile or [],
            },
        )["site"]["id"]

    def _planning_counts(self) -> tuple[int, int, int, int]:
        connection = connect_database(self.projects_root / "tsn_dss.db")
        try:
            return (
                connection.execute("SELECT COUNT(*) FROM sites").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM targets").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM mosaic_plans").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM mosaic_panels").fetchone()[0],
            )
        finally:
            connection.close()

    def _wait_for_run(self, run_id: str, timeout_s: float = 5.0) -> dict[str, object]:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            payload = self._read_json(f"/api/project-runs/{run_id}")
            if payload["run"]["status"] in {"completed", "failed"}:
                return payload
            time.sleep(0.05)
        self.fail(f"Run did not finish before timeout: {run_id}")

    def _copy_capture_tree(self, source_root: Path, destination_root: Path) -> None:
        for source in source_root.rglob("*"):
            if not source.is_file():
                continue
            relative = source.relative_to(source_root)
            destination = destination_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())


if __name__ == "__main__":
    unittest.main()
