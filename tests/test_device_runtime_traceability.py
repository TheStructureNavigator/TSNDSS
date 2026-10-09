"""Validates DSS-CTR-013 REQ-ID traceability for DB-01 against the roadmap, contract, code and tests."""

from __future__ import annotations

import importlib
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tests" / "device_runtime_traceability.json"
ROADMAP = ROOT / "ROADMAP_DEVICE_BACKEND.md"
STATUSES = {"verified", "partially_verified", "boundary_verified", "type_level_only", "deferred"}
REQ_RE = re.compile(r"REQ-(\d{3})(?:\s+through\s+REQ-(\d{3}))?")


def _load() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _ids(text: str) -> set[int]:
    found: set[int] = set()
    for match in REQ_RE.finditer(text):
        low = int(match.group(1))
        found.update(range(low, int(match.group(2) or low) + 1))
    return found


def _roadmap_db01_coverage() -> dict[str, set[int]]:
    text = ROADMAP.read_text(encoding="utf-8")
    stage = text.split("## DB-01", 1)[1].split("\n## DB-02", 1)[0]
    block = stage.split("### Contract requirement coverage", 1)[1].split("\n### ", 1)[0]
    coverage: dict[str, set[int]] = {}
    for line in block.splitlines():
        for prefix, key in (
            ("Primary:", "primary"),
            ("Boundary:", "boundary"),
            ("Type-level preparation only", "type_level_only"),
        ):
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


class DeviceRuntimeTraceabilityTests(unittest.TestCase):
    def test_manifest_matches_the_contract_snapshot(self) -> None:
        manifest = _load()
        source = ROOT / manifest["source_path"]
        self.assertTrue(source.is_file())
        text = source.read_text(encoding="utf-8")
        self.assertIn("**Status:** Draft", text)
        self.assertIn("**Version:** 0.2", text)
        self.assertEqual((manifest["contract_status"], manifest["contract_version"]), ("Draft", "0.2"))
        in_contract = set(re.findall(r"DSS-CTR-013-REQ-\d{3}", text))
        self.assertEqual(set(manifest["requirements"]), in_contract)
        self.assertEqual(len(in_contract), 67)

    def test_manifest_classification_equals_the_roadmap_db01_coverage(self) -> None:
        coverage = _roadmap_db01_coverage()
        self.assertEqual(set(coverage), {"primary", "boundary", "type_level_only"})
        requirements = _load()["requirements"]
        for key, expected in coverage.items():
            actual = {
                int(rid[-3:]) for rid, entry in requirements.items() if entry["classification"] == key
            }
            self.assertEqual(actual, expected, key)
        in_scope = set().union(*coverage.values())
        deferred = {int(r[-3:]) for r, e in requirements.items() if e["classification"] == "deferred"}
        self.assertEqual(in_scope | deferred, set(range(1, 68)))
        self.assertTrue(in_scope.isdisjoint(deferred))

    def test_every_in_scope_requirement_has_resolvable_code_and_tests(self) -> None:
        for rid, entry in _load()["requirements"].items():
            self.assertIn(entry["status"], STATUSES, rid)
            if entry["classification"] == "deferred":
                self.assertEqual((entry["components"], entry["tests"]), ([], []), rid)
                self.assertTrue(entry["planned_stage"], rid)
                continue
            self.assertTrue(entry["components"], rid)
            self.assertTrue(entry["tests"], rid)
            for component in entry["components"]:
                _resolve(component)
            for reference in entry["tests"]:
                case = _resolve(reference)
                self.assertTrue(callable(case), (rid, reference))
                self.assertTrue(reference.rsplit(".", 1)[1].startswith("test_"), reference)

    def test_partial_and_type_level_entries_carry_a_deferral_note(self) -> None:
        for rid, entry in _load()["requirements"].items():
            if entry["status"] in ("partially_verified", "type_level_only"):
                self.assertTrue(entry["note"].strip(), rid)
        for number in (20, 39, 40, 44):
            entry = _load()["requirements"][f"DSS-CTR-013-REQ-{number:03d}"]
            self.assertEqual(entry["status"], "type_level_only")

    def test_every_status_used_is_defined_and_boundary_statuses_are_not_overclaimed(self) -> None:
        manifest = _load()
        definitions = manifest["status_definitions"]
        self.assertEqual(set(definitions), STATUSES)
        used = {entry["status"] for entry in manifest["requirements"].values()}
        self.assertTrue(used <= set(definitions))
        for rid, entry in manifest["requirements"].items():
            if entry["classification"] == "primary":
                self.assertIn(entry["status"], {"verified", "partially_verified"}, rid)
            if entry["classification"] == "boundary":
                self.assertEqual(entry["status"], "boundary_verified", rid)

    def test_legacy_manifest_does_not_include_ctr_013(self) -> None:
        legacy = json.loads((ROOT / "tests" / "contract_traceability.json").read_text(encoding="utf-8"))
        self.assertNotIn("DSS-CTR-013", {c["contract_id"] for c in legacy["contracts"]})


if __name__ == "__main__":
    unittest.main()
