from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from tsn_dss.domain.models import LocalHorizonPoint, Site, Target
from tsn_dss.engine.openngc import register_bundled_openngc_catalog
from tsn_dss.engine.sqlite.catalog import CatalogRepository
from tsn_dss.engine.sqlite.db import initialize_database
from tsn_dss.engine.sqlite.planning import PlanningRepository
from tsn_dss.mcp.server import create_mcp_server
from tsn_dss.mcp.tools.catalog import resolve_catalog_object, search_catalog
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

    def test_empty_catalog_returns_empty_search_and_not_found_resolution(self) -> None:
        search = search_catalog(self.connection, query="M42")
        resolved = resolve_catalog_object(self.connection, "M42")

        self.assertEqual(search["objects"], [])
        self.assertEqual(resolved["resolution"]["status"], "not_found")
        self.assertIsNone(resolved["resolution"]["catalog_object"])

    def test_mcp_server_registers_expected_tools_and_returns_structured_content(self) -> None:
        async def run_check() -> None:
            server = create_mcp_server(database_path=self.db_path)
            tools = await server.list_tools()
            result = await server.call_tool("get_sites", {})

            self.assertEqual(
                sorted(tool.name for tool in tools),
                [
                    "get_site",
                    "get_sites",
                    "resolve_catalog_object",
                    "search_catalog",
                    "search_targets",
                    "target_visibility_at",
                ],
            )
            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content["sites"][0]["id"], "site:yard")

        asyncio.run(run_check())

    def test_mcp_catalog_tools_do_not_auto_register_openngc(self) -> None:
        async def run_check() -> None:
            before = self.connection.execute("SELECT COUNT(*) FROM catalog_objects").fetchone()[0]
            server = create_mcp_server(database_path=self.db_path)
            result = await server.call_tool("search_catalog", {"query": "M42"})
            after = self.connection.execute("SELECT COUNT(*) FROM catalog_objects").fetchone()[0]

            self.assertFalse(result.is_error)
            self.assertEqual(result.structured_content["objects"], [])
            self.assertEqual((before, after), (0, 0))

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


class McpCatalogToolsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp_dir.name) / "catalog.db"
        cls.connection = initialize_database(cls.db_path)
        register_bundled_openngc_catalog(CatalogRepository(cls.connection))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.connection.close()
        cls.temp_dir.cleanup()

    def test_exact_aliases_resolve_to_same_catalog_object(self) -> None:
        m42_ids = {
            self._resolved_id(alias)
            for alias in ("M42", "M 42", "Messier 42", "NGC 1976", "Orion Nebula")
        }
        m31_ids = {self._resolved_id(alias) for alias in ("M31", "NGC 224", "Andromeda Galaxy")}

        self.assertEqual(m42_ids, {"catalog-object:openngc:ngc1976"})
        self.assertEqual(m31_ids, {"catalog-object:openngc:ngc0224"})
        self.assertEqual(self._resolved_id("NGC 7000"), "catalog-object:openngc:ngc7000")
        self.assertEqual(self._resolved_id("North America Nebula"), "catalog-object:openngc:ngc7000")
        self.assertEqual(self._resolved_id("IC 4703"), "catalog-object:openngc:ic4703")

    def test_resolved_payload_contains_machine_friendly_object_and_aliases(self) -> None:
        payload = resolve_catalog_object(self.connection, "M42")
        resolved = payload["resolution"]["catalog_object"]

        self.assertEqual(payload["catalog_scope"], "canonical_tsn_dss_catalog_objects")
        self.assertEqual(payload["resolution"]["status"], "resolved")
        self.assertEqual(resolved["catalog_object_id"], "catalog-object:openngc:ngc1976")
        self.assertEqual(resolved["canonical_designation"], "M42")
        self.assertIsInstance(resolved["ra_deg"], float)
        self.assertIsInstance(resolved["dec_deg"], float)
        self.assertIn("source_provider", resolved)
        self.assertIn("source_version", resolved)
        self.assertIn("aliases", resolved)
        self.assertIn("messier:42", {alias["normalized_alias"] for alias in resolved["aliases"]})

    def test_eagle_nebula_collision_and_missing_query_do_not_resolve(self) -> None:
        eagle = resolve_catalog_object(self.connection, "Eagle Nebula")
        missing = resolve_catalog_object(self.connection, "Definitely Not A Catalog Object 999")

        self.assertEqual(eagle["resolution"]["status"], "not_found")
        self.assertIsNone(eagle["resolution"]["catalog_object"])
        self.assertEqual(missing["resolution"]["status"], "not_found")
        self.assertIsNone(missing["resolution"]["catalog_object"])

    def test_search_catalog_text_and_object_type_filters(self) -> None:
        m42 = search_catalog(self.connection, query="M42", limit=10)
        messier = search_catalog(self.connection, query="Messier 42", limit=10)
        north = search_catalog(self.connection, query="North America", limit=10)
        galaxies = search_catalog(self.connection, object_type="galaxy", limit=3)
        planetary = search_catalog(self.connection, object_type="planetary_nebula", limit=3)

        self.assertEqual([item["catalog_object_id"] for item in m42["objects"]], ["catalog-object:openngc:ngc1976"])
        self.assertEqual([item["catalog_object_id"] for item in messier["objects"]], ["catalog-object:openngc:ngc1976"])
        self.assertEqual([item["catalog_object_id"] for item in north["objects"]], ["catalog-object:openngc:ngc7000"])
        self.assertEqual(galaxies["object_type"], "galaxy")
        self.assertLessEqual(len(galaxies["objects"]), 3)
        self.assertTrue(all(item["object_type"] == "galaxy" for item in galaxies["objects"]))
        self.assertTrue(all(item["object_type"] == "planetary_nebula" for item in planetary["objects"]))

    def test_search_catalog_bounds_limit_and_orders_deterministically(self) -> None:
        payload = search_catalog(self.connection, object_type="galaxy", limit=500)
        repeated = search_catalog(self.connection, object_type="galaxy", limit=500)
        designations = [item["canonical_designation"] for item in payload["objects"]]

        self.assertEqual(payload["limit"], 100)
        self.assertEqual(payload["objects"], repeated["objects"])
        self.assertEqual(designations, sorted(designations))

    def test_search_catalog_rejects_invalid_limit(self) -> None:
        with self.assertRaisesRegex(ValueError, "limit must be >= 1"):
            search_catalog(self.connection, limit=0)

    def test_catalog_queries_do_not_modify_database(self) -> None:
        before = self._catalog_counts()

        search_catalog(self.connection, query="M42")
        resolve_catalog_object(self.connection, "M42")
        search_catalog(self.connection, object_type="galaxy", limit=5)

        self.assertEqual(self._catalog_counts(), before)

    def test_mcp_server_catalog_tool_acceptance(self) -> None:
        async def run_check() -> None:
            server = create_mcp_server(database_path=self.db_path)
            resolved = await server.call_tool("resolve_catalog_object", {"query": "M42"})
            search = await server.call_tool("search_catalog", {"query": "North America", "limit": 5})

            self.assertFalse(resolved.is_error)
            self.assertFalse(search.is_error)
            self.assertEqual(
                resolved.structured_content["resolution"]["catalog_object"]["catalog_object_id"],
                "catalog-object:openngc:ngc1976",
            )
            self.assertEqual(search.structured_content["objects"][0]["catalog_object_id"], "catalog-object:openngc:ngc7000")

        asyncio.run(run_check())

    def _resolved_id(self, alias: str) -> str:
        payload = resolve_catalog_object(self.connection, alias)
        self.assertEqual(payload["resolution"]["status"], "resolved")
        catalog_object = payload["resolution"]["catalog_object"]
        assert catalog_object is not None
        return catalog_object["catalog_object_id"]

    def _catalog_counts(self) -> tuple[int, int]:
        return (
            self.connection.execute("SELECT COUNT(*) FROM catalog_objects").fetchone()[0],
            self.connection.execute("SELECT COUNT(*) FROM catalog_object_aliases").fetchone()[0],
        )


if __name__ == "__main__":
    unittest.main()
