from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tsn_dss.domain.models import Target
from tsn_dss.engine.catalog import normalize_catalog_alias
from tsn_dss.engine.openngc import (
    EXPECTED_SOURCE_FILES,
    OPENNGC_COMMIT_SHA,
    OPENNGC_LICENSE,
    OPENNGC_RECORDS_PATH,
    OPENNGC_RELEASE_TAG,
    OPENNGC_SOURCE_VERSION,
    OPENNGC_TYPE_MAP,
    REQUIRED_COLUMNS,
    build_openngc_records_from_sources,
    build_openngc_registration_plan,
    load_bundled_openngc_records,
    openngc_catalog_object_id,
    parse_dec_to_degrees,
    parse_ra_to_degrees,
    register_bundled_openngc_catalog,
)
from tsn_dss.engine.sqlite.catalog import CatalogRepository
from tsn_dss.engine.sqlite.db import initialize_database
from tsn_dss.engine.sqlite.planning import PlanningRepository
from tsn_dss.engine.sqlite.project_repository import ProjectRepository


class OpenNgcCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.records = load_bundled_openngc_records()
        cls.plan = build_openngc_registration_plan(cls.records)

    def test_pinned_snapshot_metadata_and_digest(self) -> None:
        manifest = json.loads(OPENNGC_RECORDS_PATH.with_name("manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["release_tag"], OPENNGC_RELEASE_TAG)
        self.assertEqual(manifest["commit_sha"], OPENNGC_COMMIT_SHA)
        self.assertEqual(manifest["license"], OPENNGC_LICENSE)
        self.assertEqual(manifest["source_version"], OPENNGC_SOURCE_VERSION)
        self.assertEqual(manifest["total_source_rows"], 14033)
        self.assertEqual(manifest["source_files"], EXPECTED_SOURCE_FILES)
        self.assertEqual(manifest["generated_artifact"]["records"], 13371)
        self.assertEqual(
            _sha256(OPENNGC_RECORDS_PATH),
            manifest["generated_artifact"]["sha256"],
        )

    def test_source_parser_validates_pinned_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_dir = root / "database_files"
            source_dir.mkdir()
            ngc = source_dir / "NGC.csv"
            addendum = source_dir / "addendum.csv"
            _write_source(
                ngc,
                [
                    {
                        "Name": "NGC1976",
                        "Type": "Cl+N",
                        "RA": "05:35:16.48",
                        "Dec": "-05:23:22.8",
                        "MajAx": "90.00",
                        "MinAx": "60.00",
                        "V-Mag": "4.00",
                        "M": "042",
                        "NGC": "",
                        "IC": "",
                        "Identifiers": "LBN 974",
                        "Common names": "Great Orion Nebula,Orion Nebula",
                    }
                ],
            )
            _write_source(addendum, [])
            expected = {
                "database_files/NGC.csv": {
                    "rows": 1,
                    "sha256": _sha256(ngc),
                    "url": "test://NGC.csv",
                },
                "database_files/addendum.csv": {
                    "rows": 0,
                    "sha256": _sha256(addendum),
                    "url": "test://addendum.csv",
                },
            }

            with patch("tsn_dss.engine.openngc.EXPECTED_SOURCE_FILES", expected):
                records = build_openngc_records_from_sources(root)

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].canonical_designation, "M42")
            self.assertEqual(records[0].display_name, "Great Orion Nebula")
            self.assertEqual(records[0].object_type, "cluster_nebula")

            ngc.write_text(ngc.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with patch("tsn_dss.engine.openngc.EXPECTED_SOURCE_FILES", expected):
                with self.assertRaises(ValueError):
                    build_openngc_records_from_sources(root)

    def test_coordinate_conversion_boundaries_and_representatives(self) -> None:
        self.assertEqual(parse_ra_to_degrees("00:00:00.00"), 0.0)
        self.assertEqual(parse_ra_to_degrees("23:59:59.99"), 359.99995833)
        self.assertEqual(parse_dec_to_degrees("+00:00:00.0"), 0.0)
        self.assertEqual(parse_dec_to_degrees("-90:00:00.0"), -90.0)
        self.assertEqual(parse_ra_to_degrees("05:35:16.48"), 83.81866667)
        self.assertEqual(parse_dec_to_degrees("-05:23:22.8"), -5.38966667)
        with self.assertRaises(ValueError):
            parse_ra_to_degrees("24:00:00.00")
        with self.assertRaises(ValueError):
            parse_dec_to_degrees("+90:00:00.1")

    def test_object_type_mapping_is_explicit(self) -> None:
        self.assertEqual(OPENNGC_TYPE_MAP["G"], "galaxy")
        self.assertEqual(OPENNGC_TYPE_MAP["Cl+N"], "cluster_nebula")
        self.assertNotIn("Dup", OPENNGC_TYPE_MAP)
        self.assertNotIn("NonEx", OPENNGC_TYPE_MAP)

    def test_deterministic_ids_canonical_designation_and_display_name(self) -> None:
        records = {record.canonical_designation: record for record in self.records}

        self.assertEqual(openngc_catalog_object_id("NGC1976"), "catalog-object:openngc:ngc1976")
        self.assertEqual(records["M42"].catalog_object_id, "catalog-object:openngc:ngc1976")
        self.assertEqual(records["M42"].display_name, "Great Orion Nebula")
        self.assertEqual(records["M31"].catalog_object_id, "catalog-object:openngc:ngc0224")
        self.assertEqual(records["M31"].display_name, "Andromeda Galaxy")
        self.assertEqual(records["M45"].source_path, "database_files/addendum.csv")
        self.assertEqual(records["M40"].source_path, "database_files/addendum.csv")

    def test_ngc_ic_and_messier_normalization(self) -> None:
        cases = {
            "M42": "messier:42",
            "M 42": "messier:42",
            "Messier 42": "messier:42",
            "NGC1976": "ngc:1976",
            "NGC 01976": "ngc:1976",
            "IC434": "ic:434",
            "IC 0434": "ic:434",
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(normalize_catalog_alias(value), expected)

    def test_full_catalog_counts_are_pinned(self) -> None:
        self.assertEqual(self.plan.total_source_rows, 14033)
        self.assertEqual(len(self.records), 13371)
        self.assertEqual(self.plan.exclusions_by_reason, {"duplicate": 652, "nonexistent": 10})
        self.assertEqual(self.plan.exact_aliases, 66065)
        self.assertEqual(len(self.plan.alias_collisions), 15)
        self.assertEqual(self.plan.messier_objects, 109)
        self.assertEqual(self.plan.objects_with_angular_dimensions, 12071)
        self.assertEqual(self.plan.objects_with_v_magnitude, 4268)
        self.assertEqual(
            self.plan.object_type_counts,
            {
                "cluster_nebula": 67,
                "dark_nebula": 2,
                "double_star": 244,
                "emission_nebula": 8,
                "galaxy": 10521,
                "galaxy_group": 13,
                "galaxy_pair": 231,
                "galaxy_triplet": 26,
                "globular_cluster": 208,
                "hii_region": 83,
                "nebula": 94,
                "nova": 3,
                "open_cluster": 663,
                "other": 419,
                "planetary_nebula": 130,
                "reflection_nebula": 38,
                "star": 546,
                "stellar_association": 64,
                "supernova_remnant": 11,
            },
        )

    def test_alias_collision_policy_skips_ambiguous_exact_aliases(self) -> None:
        collisions = {collision.normalized_alias: collision for collision in self.plan.alias_collisions}

        for alias in ("antennae galaxies", "eagle nebula", "eastern veil", "network nebula", "siamese twins"):
            self.assertIn(alias, collisions)
            self.assertGreater(len(collisions[alias].object_ids), 1)

        ic4703 = _item_by_designation(self.plan, "IC 4703")
        self.assertNotIn("eagle nebula", {alias.normalized_alias for alias in ic4703.aliases})
        self.assertIn("ic:4703", {alias.normalized_alias for alias in ic4703.aliases})

        for designation, normalized in (
            ("NGC 554", "ngc:554"),
            ("NGC 704", "ngc:704"),
            ("NGC 764", "ngc:764"),
            ("NGC 6839", "ngc:6839"),
            ("IC 1459", "ic:1459"),
        ):
            with self.subTest(designation=designation):
                item = _item_by_designation(self.plan, designation)
                aliases = {alias.normalized_alias for alias in item.aliases}
                self.assertIn(normalized, aliases)

    def test_full_registration_is_idempotent_and_resolves_representative_objects(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            connection = initialize_database(Path(temp) / "tsn.db")
            try:
                repository = CatalogRepository(connection)

                first, plan = register_bundled_openngc_catalog(repository)
                second, _ = register_bundled_openngc_catalog(repository)

                self.assertEqual(len(first.objects_created), 13371)
                self.assertEqual(len(first.aliases_created), plan.exact_aliases)
                self.assertEqual(first.conflicts, [])
                self.assertEqual(second.objects_created, [])
                self.assertEqual(len(second.objects_unchanged), 13371)
                self.assertEqual(second.aliases_created, [])
                self.assertEqual(len(second.aliases_unchanged), plan.exact_aliases)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM catalog_objects").fetchone()[0], 13371)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM catalog_object_aliases").fetchone()[0], 66065)

                self._assert_resolves(repository, ("M42", "M 42", "Messier 42", "NGC 1976", "Orion Nebula"), "M42")
                self._assert_resolves(repository, ("M31", "Messier 31", "NGC 224", "Andromeda Galaxy"), "M31")
                self._assert_resolves(repository, ("NGC 7000", "North America Nebula"), "NGC 7000")
                self._assert_resolves(repository, ("IC 4703",), "IC 4703")
                self.assertEqual(repository.resolve_alias("Eagle Nebula").status, "not_found")
            finally:
                connection.close()

    def test_importing_openngc_does_not_create_targets_or_modify_projects(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            connection = initialize_database(Path(temp) / "tsn.db")
            try:
                planning = PlanningRepository(connection)
                projects = ProjectRepository(connection)
                projects.register_project(dir_key="M42", target_label="M42")
                planning.create_target(
                    Target(
                        id="target:m31",
                        catalog="MESSIER",
                        catalog_id="M31",
                        name="Andromeda Galaxy",
                        ra_deg=10.6847,
                        dec_deg=41.2692,
                    )
                )
                before_project = projects.get_project_by_dir_key("M42")
                before_target_ids = [target.id for target in planning.list_targets()]
                relationship_counts = {
                    table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("acquisition_plans", "observations", "datasets")
                }

                register_bundled_openngc_catalog(CatalogRepository(connection))

                self.assertEqual([target.id for target in planning.list_targets()], before_target_ids)
                after_project = projects.get_project_by_dir_key("M42")
                assert before_project is not None and after_project is not None
                self.assertEqual((after_project.target_id, after_project.target_label), (None, "M42"))
                self.assertEqual((before_project.target_id, before_project.target_label), (None, "M42"))
                self.assertEqual(
                    relationship_counts,
                    {
                        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                        for table in ("acquisition_plans", "observations", "datasets")
                    },
                )
            finally:
                connection.close()

    def _assert_resolves(self, repository: CatalogRepository, aliases: tuple[str, ...], canonical: str) -> None:
        object_ids = set()
        for alias in aliases:
            with self.subTest(alias=alias):
                result = repository.resolve_alias(alias)
                self.assertEqual(result.status, "resolved")
                assert result.catalog_object is not None
                object_ids.add(result.catalog_object.id)
                self.assertEqual(result.catalog_object.canonical_designation, canonical)
        self.assertEqual(len(object_ids), 1)


def _item_by_designation(plan, designation: str):
    for item in plan.items:
        if item.catalog_object.canonical_designation == designation:
            return item
    raise AssertionError(f"Missing CatalogObject {designation}")


def _write_source(path: Path, rows: list[dict[str, str]]) -> None:
    columns = list(REQUIRED_COLUMNS)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, delimiter=";")
        writer.writeheader()
        writer.writerows(rows)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    unittest.main()
