"""Validates the DB-03 Wave 4B-1 manifest against the contract text and the test code."""

from __future__ import annotations

import importlib
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "tests" / "opencv_isolated_wave4b1_traceability.json").read_text(encoding="utf-8"))
CONTRACT = (ROOT / "Contracts" / "DSS-CTR-013-Device-Provider-Runtime-Contract.md").read_text(encoding="utf-8")
KEYWORDS = {
    "REQ-022": "Passive reads **SHALL NOT** create or submit Commands",
    "REQ-052": "SHALL NOT** own Project, Session, Observation",
    "REQ-067": "Acquisition Execution work",
}


def resolve(dotted: str):
    module_name, cls_name, method = dotted.rsplit(".", 2)
    return getattr(getattr(importlib.import_module(module_name), cls_name), method)


class WaveFourB1TraceabilityTests(unittest.TestCase):
    def entries(self):
        return MANIFEST["local_semantics"] + list(MANIFEST["requirements"].values())

    def test_every_referenced_test_exists(self) -> None:
        for entry in self.entries():
            self.assertTrue(entry["tests"])
            for dotted in entry["tests"]:
                self.assertTrue(callable(resolve(dotted)), dotted)

    def test_claims_match_the_contract_text(self) -> None:
        for req, entry in MANIFEST["requirements"].items():
            self.assertEqual(entry["status"], "boundary_verified")
            line = next((l for l in CONTRACT.splitlines() if f"DSS-CTR-013-{req}:" in l), None)
            self.assertIsNotNone(line, req)
            self.assertIn(KEYWORDS[req], line, req)

    def test_image_and_preview_requirements_are_not_claimed(self) -> None:
        for req in ("REQ-016", "REQ-018", "REQ-033", "REQ-064", "REQ-059", "REQ-058", "REQ-057", "REQ-060"):
            self.assertNotIn(req, MANIFEST["requirements"])
        self.assertIn("image_requirements", MANIFEST["not_claimed"])

    def test_all_evidence_is_offline_and_no_hardware_or_windows_production_claim_is_made(self) -> None:
        self.assertTrue(all(entry["evidence"] == "offline" for entry in self.entries()))
        self.assertEqual(MANIFEST["hardware_claims"], [])
        self.assertIn("windows_production_launcher", MANIFEST["not_claimed"])
        self.assertIn("UNVERIFIED", MANIFEST["windows_status"]["this_wave"])
        self.assertNotIn("hardware_verified", json.dumps(MANIFEST).lower())

    def test_the_four_d13_freeze_checks_are_traced(self) -> None:
        ids = {e["id"]: e for e in MANIFEST["local_semantics"]}
        for key in ("PRV-ISO-003", "PRV-ISO-004", "PRV-ISO-005", "PRV-ISO-006"):
            self.assertIn(key, ids)
            self.assertTrue(any("StatusTableTests" in t or "StartupFailureTests" in t for t in ids[key]["tests"]), key)

    def test_semantic_ids_unique_and_well_formed(self) -> None:
        ids = [e["id"] for e in MANIFEST["local_semantics"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(re.match(r"^PRV-ISO-\d{3}$", i) for i in ids))

    def test_requirements_within_db03_roadmap_scope(self) -> None:
        text = (ROOT / "ROADMAP_DEVICE_BACKEND.md").read_text(encoding="utf-8")
        stage = text.split("## DB-03", 1)[1].split("\n## DB-04", 1)[0]
        for req in MANIFEST["requirements"]:
            self.assertIn(req, stage, req)


if __name__ == "__main__":
    unittest.main()
