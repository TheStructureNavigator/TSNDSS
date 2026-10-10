"""Validates the DB-04 DSS-CTR-013 REQ-ID traceability manifest against the roadmap, contract, code and tests."""

from __future__ import annotations

import importlib
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tests" / "device_command_traceability.json"
HISTORICAL = ROOT / "tests" / "device_runtime_traceability.json"
ROADMAP = ROOT / "ROADMAP_DEVICE_BACKEND.md"
STATUSES = {"verified", "partially_verified", "boundary_verified"}
REQ_RE = re.compile(r"REQ-(\d{3})(?:\s+through\s+REQ-(\d{3}))?")


def _load() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _ids(text: str) -> set[int]:
    found: set[int] = set()
    for match in REQ_RE.finditer(text):
        low = int(match.group(1))
        found.update(range(low, int(match.group(2) or low) + 1))
    return found


def _roadmap_db04_coverage() -> dict[str, set[int]]:
    text = ROADMAP.read_text(encoding="utf-8")
    stage = text.split("## DB-04", 1)[1].split("\n## DB-05", 1)[0]
    block = stage.split("### Contract requirement coverage", 1)[1].split("\n### ", 1)[0]
    coverage: dict[str, set[int]] = {}
    for line in block.splitlines():
        for prefix, key in (("Primary:", "primary"), ("Boundary:", "boundary")):
            if line.startswith(prefix):
                coverage[key] = _ids(line)
    return coverage


def _resolve(dotted: str):
    parts = dotted.split(".")
    for split in range(len(parts), 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:split]))
        except ModuleNotFoundError:
            continue
        for attribute in parts[split:]:
            obj = getattr(obj, attribute)
        return obj
    raise AssertionError(f"cannot resolve {dotted}")


class CommandTraceabilityTests(unittest.TestCase):
    def test_manifest_matches_the_contract_snapshot(self) -> None:
        manifest = _load()
        text = (ROOT / manifest["source_path"]).read_text(encoding="utf-8")
        self.assertIn("**Status:** Draft", text)
        self.assertIn("**Version:** 0.2", text)
        in_contract = set(re.findall(r"DSS-CTR-013-REQ-\d{3}", text))
        self.assertTrue(set(manifest["requirements"]) <= in_contract)
        self.assertEqual(manifest["stage"], "DB-04")
        self.assertIn("**Amendments:** A1", text)
        self.assertEqual(len(manifest["contract_amendments"]), 1)

    def test_manifest_classification_equals_the_roadmap_db04_coverage(self) -> None:
        coverage = _roadmap_db04_coverage()
        self.assertEqual(set(coverage), {"primary", "boundary"})
        requirements = _load()["requirements"]
        for key, expected in coverage.items():
            actual = {int(rid[-3:]) for rid, entry in requirements.items() if entry["classification"] == key}
            self.assertEqual(actual, expected, key)
        self.assertEqual(len(requirements), len(coverage["primary"]) + len(coverage["boundary"]))

    def test_every_requirement_has_resolvable_code_and_tests(self) -> None:
        for rid, entry in _load()["requirements"].items():
            self.assertIn(entry["status"], STATUSES, rid)
            self.assertTrue(entry["components"] and entry["tests"], rid)
            for component in entry["components"]:
                _resolve(component)
            for reference in entry["tests"]:
                case = _resolve(reference)
                self.assertTrue(callable(case), (rid, reference))
                self.assertTrue(reference.rsplit(".", 1)[1].startswith("test_"), reference)
            self.assertEqual(len(entry["tests"]), len(set(entry["tests"])), rid)

    def test_partial_entries_carry_a_note_and_status_matches_classification(self) -> None:
        for rid, entry in _load()["requirements"].items():
            if entry["status"] == "partially_verified":
                self.assertTrue(entry["note"].strip(), rid)
            if entry["classification"] == "primary":
                self.assertIn(entry["status"], {"verified", "partially_verified"}, rid)
            else:
                self.assertEqual(entry["status"], "boundary_verified", rid)

    def test_the_unverifiable_branches_are_declared_not_hidden(self) -> None:
        requirements = _load()["requirements"]
        partial = {rid for rid, e in requirements.items() if e["status"] == "partially_verified"}
        self.assertEqual(partial, {"DSS-CTR-013-REQ-025", "DSS-CTR-013-REQ-046"})
        for rid in partial:
            self.assertIn("D3", requirements[rid]["note"])

    def test_every_status_used_is_defined(self) -> None:
        manifest = _load()
        self.assertEqual(set(manifest["status_definitions"]), STATUSES)

    def test_the_historical_db01_manifest_is_not_rewritten(self) -> None:
        historical = json.loads(HISTORICAL.read_text(encoding="utf-8"))
        self.assertEqual(historical["stage"], "DB-01")
        for number in (20, 39, 40, 44):
            self.assertEqual(historical["requirements"][f"DSS-CTR-013-REQ-{number:03d}"]["status"], "type_level_only")
        self.assertNotEqual(MANIFEST, HISTORICAL)

    def test_legacy_manifest_does_not_include_ctr_013(self) -> None:
        legacy = json.loads((ROOT / "tests" / "contract_traceability.json").read_text(encoding="utf-8"))
        self.assertNotIn("DSS-CTR-013", {c["contract_id"] for c in legacy["contracts"]})


if __name__ == "__main__":
    unittest.main()
