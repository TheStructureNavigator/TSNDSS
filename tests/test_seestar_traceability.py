"""Validates the DB-02 REQ-ID manifest against the roadmap, the contract snapshot, code and tests."""

from __future__ import annotations

import importlib
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "tests" / "seestar_db02_traceability.json"
ROADMAP = ROOT / "ROADMAP_DEVICE_BACKEND.md"
STATUSES = {"offline_verified", "partial_offline", "boundary_offline", "deferred"}
REQ_RE = re.compile(r"REQ-(\d{3})(?:\s+through\s+REQ-(\d{3}))?")


def _load() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _ids(text: str) -> set[int]:
    found: set[int] = set()
    for match in REQ_RE.finditer(text):
        low = int(match.group(1))
        found.update(range(low, int(match.group(2) or low) + 1))
    return found


def _roadmap_db02_coverage() -> dict[str, set[int]]:
    text = ROADMAP.read_text(encoding="utf-8")
    stage = text.split("## DB-02", 1)[1].split("\n## DB-03", 1)[0]
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


class SeestarTraceabilityTests(unittest.TestCase):
    def test_manifest_matches_the_contract_snapshot(self) -> None:
        manifest = _load()
        text = (ROOT / manifest["source_path"]).read_text(encoding="utf-8")
        self.assertIn("**Status:** Draft", text)
        self.assertIn("**Version:** 0.2", text)
        self.assertEqual(set(manifest["requirements"]), set(re.findall(r"DSS-CTR-013-REQ-\d{3}", text)))

    def test_classification_equals_the_roadmap_db02_coverage(self) -> None:
        coverage = _roadmap_db02_coverage()
        self.assertEqual(set(coverage), {"primary", "boundary"})
        requirements = _load()["requirements"]
        for key, expected in coverage.items():
            actual = {int(r[-3:]) for r, e in requirements.items() if e["classification"] == key}
            self.assertEqual(actual, expected, key)
        in_scope = coverage["primary"] | coverage["boundary"]
        deferred = {int(r[-3:]) for r, e in requirements.items() if e["classification"] == "deferred"}
        self.assertEqual(in_scope | deferred, set(range(1, 68)))
        self.assertTrue(in_scope.isdisjoint(deferred))

    def test_resolved_requirements_from_the_roadmap_correction(self) -> None:
        coverage = _roadmap_db02_coverage()
        for n in (16, 17, 55, 57, 59):
            self.assertIn(n, coverage["primary"], n)
        self.assertIn(65, coverage["boundary"])
        self.assertNotIn(51, coverage["primary"] | coverage["boundary"])
        requirements = _load()["requirements"]
        self.assertEqual(requirements["DSS-CTR-013-REQ-051"]["classification"], "deferred")
        self.assertEqual(requirements["DSS-CTR-013-REQ-016"]["status"], "partial_offline")
        self.assertEqual(requirements["DSS-CTR-013-REQ-057"]["status"], "partial_offline")

    def test_every_in_scope_requirement_has_resolvable_code_and_tests(self) -> None:
        for rid, entry in _load()["requirements"].items():
            self.assertIn(entry["status"], STATUSES, rid)
            if entry["classification"] == "deferred":
                self.assertEqual((entry["components"], entry["tests"]), ([], []), rid)
                self.assertTrue(entry["planned_stage"], rid)
                continue
            self.assertTrue(entry["components"] and entry["tests"], rid)
            for component in entry["components"]:
                _resolve(component)
            for reference in entry["tests"]:
                self.assertTrue(callable(_resolve(reference)), reference)

    def test_nothing_is_claimed_verified_on_hardware(self) -> None:
        manifest = _load()
        self.assertIn("NOT yet performed", manifest["note"])
        self.assertEqual(set(manifest["status_definitions"]), STATUSES)
        for rid, entry in manifest["requirements"].items():
            self.assertNotEqual(entry["status"], "verified", rid)
            if entry["status"] == "partial_offline":
                self.assertTrue(entry["note"].strip(), rid)

    def test_other_manifests_do_not_include_this_stage(self) -> None:
        legacy = json.loads((ROOT / "tests" / "contract_traceability.json").read_text(encoding="utf-8"))
        self.assertNotIn("DSS-CTR-013", {c["contract_id"] for c in legacy["contracts"]})
        db01 = json.loads((ROOT / "tests" / "device_runtime_traceability.json").read_text(encoding="utf-8"))
        self.assertEqual(db01["stage"], "DB-01")


if __name__ == "__main__":
    unittest.main()
