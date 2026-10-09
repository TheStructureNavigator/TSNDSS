"""Validates the DB-03 Wave 4 manifest against the contract and the test code."""

from __future__ import annotations

import importlib
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "tests" / "opencv_preview_wave4_traceability.json").read_text(encoding="utf-8"))
CONTRACT = (ROOT / "Contracts" / "DSS-CTR-013-Device-Provider-Runtime-Contract.md").read_text(encoding="utf-8")
STATUSES = {"verified", "partially_verified", "boundary_verified"}
# What each claimed requirement says, in the words of the contract. A claim whose text drifts is caught here.
CONTRACT_KEYWORDS = {
    "REQ-018": "SHALL NOT** automatically become Capture, Frame, Dataset, ProcessingRun, Observation or Session state",
    "REQ-022": "Passive reads **SHALL NOT** create or submit Commands",
    "REQ-059": "SHALL NOT** submit state-changing Commands or alter physical Device state",
    "REQ-032": "canonical TSN DSS domain identities",
    "REQ-033": "SHALL NOT** become Capture or Frame",
    "REQ-052": "SHALL NOT** own Project, Session, Observation",
    "REQ-064": "SHALL NOT** by itself prove that a Session, Observation, Capture, Frame, Dataset or ProcessingRun occurred",
    "REQ-067": "Acquisition Execution work",
}


def _resolve(dotted: str):
    module_name, cls_name, method = dotted.rsplit(".", 2)
    return getattr(getattr(importlib.import_module(module_name), cls_name), method)


class WaveFourTraceabilityTests(unittest.TestCase):
    def entries(self):
        return MANIFEST["local_semantics"] + list(MANIFEST["requirements"].values())

    def test_every_referenced_test_exists(self) -> None:
        for entry in self.entries():
            self.assertTrue(entry["tests"])
            for dotted in entry["tests"]:
                self.assertTrue(callable(_resolve(dotted)), dotted)

    def test_claims_match_the_contract_text(self) -> None:
        for req, entry in MANIFEST["requirements"].items():
            self.assertIn(entry["status"], STATUSES)
            line = next((l for l in CONTRACT.splitlines() if f"DSS-CTR-013-{req}:" in l), None)
            self.assertIsNotNone(line, req)
            self.assertIn(CONTRACT_KEYWORDS[req], line, req)

    def test_unclaimed_requirements_stay_unclaimed(self) -> None:
        for req in ("REQ-058", "REQ-057", "REQ-060", "REQ-030", "REQ-031", "REQ-063", "REQ-016"):
            self.assertNotIn(req, MANIFEST["requirements"])
            self.assertIn(req, MANIFEST["not_claimed"])

    def test_all_evidence_is_offline_and_no_hardware_is_claimed(self) -> None:
        self.assertTrue(all(entry["evidence"] == "offline" for entry in self.entries()))
        self.assertEqual(MANIFEST["hardware_claims"], [])
        self.assertNotIn("hardware_verified", json.dumps(MANIFEST).lower())

    def test_req_018_and_064_are_backed_by_behavioral_tests(self) -> None:
        behavioral = "tests.test_opencv_preview_integration.EvidenceOnlyTests."
        for req in ("REQ-018", "REQ-064"):
            self.assertTrue(any(t.startswith(behavioral) for t in MANIFEST["requirements"][req]["tests"]), req)

    def test_no_hard_timeout_is_claimed_anywhere(self) -> None:
        self.assertIn("hard_timeout", MANIFEST["not_claimed"])
        text = json.dumps(MANIFEST).lower()
        self.assertNotIn("hard timeout is provided", text)
        self.assertNotIn("guarantee", text)
        self.assertIn("no hard timeout", text)

    def test_semantic_ids_unique_and_well_formed(self) -> None:
        ids = [e["id"] for e in MANIFEST["local_semantics"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(re.match(r"^PRV-S4-\d{3}$", i) for i in ids))

    def test_requirements_within_db03_roadmap_scope(self) -> None:
        text = (ROOT / "ROADMAP_DEVICE_BACKEND.md").read_text(encoding="utf-8")
        stage = text.split("## DB-03", 1)[1].split("\n## DB-04", 1)[0]
        for req in MANIFEST["requirements"]:
            self.assertIn(req, stage, req)


if __name__ == "__main__":
    unittest.main()
