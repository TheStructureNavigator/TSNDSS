from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from tsn_dss.domain.models import LocalHorizonPoint, Site, Target
from tsn_dss.engine.sqlite.db import initialize_database
from tsn_dss.engine.sqlite.planning import PlanningRepository
from tsn_dss.mcp.server import create_mcp_server
from tsn_dss.mcp.tools.sites import get_site, get_sites
from tsn_dss.mcp.tools.targets import search_targets


class McpToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "tools.db"
        self.connection = initialize_database(self.db_path)
        self.repository = PlanningRepository(self.connection)
        self.repository.create_site(
            Site(
                id="site:yard",
                name="Back Yard",
                latitude_deg=52.1,
                longitude_deg=21.0,
                elevation_m=110.0,
                bortle_class=5,
                south_horizon_open=True,
                horizon_profile=[
                    LocalHorizonPoint(azimuth_deg=0, min_altitude_deg=10),
                    LocalHorizonPoint(azimuth_deg=180, min_altitude_deg=20),
                ],
            )
        )
        self.repository.create_target(
            Target(
                id="target:m31",
                catalog="Messier",
                catalog_id="M31",
                name="Andromeda Galaxy",
                ra_deg=10.6847,
                dec_deg=41.2692,
                object_type="galaxy",
            )
        )
        self.repository.create_target(
            Target(
                id="target:m42",
                catalog="Messier",
                catalog_id="M42",
                name="Orion Nebula",
                ra_deg=83.8221,
                dec_deg=-5.3911,
                object_type="nebula",
            )
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def test_get_sites_returns_concise_summaries(self) -> None:
        payload = get_sites(self.connection)

        self.assertEqual(len(payload["sites"]), 1)
        self.assertEqual(payload["sites"][0]["id"], "site:yard")
        self.assertTrue(payload["sites"][0]["has_horizon_profile"])
        self.assertNotIn("horizon_profile", payload["sites"][0])

    def test_get_site_returns_horizon_profile_detail(self) -> None:
        payload = get_site(self.connection, "site:yard")

        self.assertEqual(payload["site"]["name"], "Back Yard")
        self.assertTrue(payload["site"]["south_horizon_open"])
        self.assertEqual(
            payload["site"]["horizon_profile"],
            [
                {"azimuth_deg": 0.0, "min_altitude_deg": 10.0},
                {"azimuth_deg": 180.0, "min_altitude_deg": 20.0},
            ],
        )

    def test_get_site_unknown_raises_value_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown site_id"):
            get_site(self.connection, "site:missing")

    def test_search_targets_lists_local_catalog_and_bounds_limit(self) -> None:
        payload = search_targets(self.connection, limit=200)

        self.assertEqual(payload["catalog_scope"], "local_tsn_dss_database")
        self.assertEqual(payload["limit"], 100)
        self.assertEqual([target["id"] for target in payload["targets"]], ["target:m31", "target:m42"])

    def test_search_targets_matches_query_without_external_lookup(self) -> None:
        matched = search_targets(self.connection, query="M31", limit=5)
        missing = search_targets(self.connection, query="Not In Catalog", limit=5)

        self.assertEqual([target["id"] for target in matched["targets"]], ["target:m31"])
        self.assertEqual(missing["targets"], [])

    def test_search_targets_rejects_invalid_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "limit must be >= 1"):
            search_targets(self.connection, limit=0)

    def test_mcp_server_registers_expected_tools_and_returns_structured_content(self) -> None:
        async def run_check() -> None:
            server = create_mcp_server(database_path=self.db_path)
            tools = await server.list_tools()
            result = await server.call_tool("get_sites", {})

            self.assertEqual(
                sorted(tool.name for tool in tools),
                ["get_site", "get_sites", "search_targets", "target_visibility_at"],
            )
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content["sites"][0]["id"], "site:yard")

        asyncio.run(run_check())

    def test_mcp_visibility_accepts_explicit_coordinates(self) -> None:
        async def run_check() -> None:
            server = create_mcp_server(database_path=self.db_path)
            result = await server.call_tool(
                "target_visibility_at",
                {
                    "site_id": "site:yard",
                    "time_utc": "2026-09-25T20:00:00Z",
                    "target_ra_deg": 83.8221,
                    "target_dec_deg": -5.3911,
                    "min_target_altitude_deg": 15.0,
                },
            )

            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content["visibility"]["target"]["source_kind"], "manual")
            self.assertEqual(result.structured_content["visibility"]["target"]["ra_deg"], 83.8221)

        asyncio.run(run_check())


if __name__ == "__main__":
    unittest.main()
