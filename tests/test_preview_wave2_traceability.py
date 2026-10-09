"""Validates the DB-03 Wave 2 manifest against the roadmap, contract and test code."""

from __future__ import annotations

import importlib
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "tests" / "preview_wave2_traceability.json").read_text(encoding="utf-8"))
CONTRACT = (ROOT / "Contracts" / "DSS-CTR-013-Device-Provider-Runtime-Contract.md").read_text(encoding="utf-8")
STATUSES = {"verified", "partially_verified", "boundary_verified"}


def _resolve(dotted: str):
    module_name, cls_name, method = dotted.rsplit(".", 2)
    cls = getattr(importlib.import_module(module_name), cls_name)
    return getattr(cls, method)


class WaveTwoTraceabilityTests(unittest.TestCase):
    def all_test_ids(self):
        for entry in MANIFEST["local_semantics"]:
            yield from entry["tests"]
        for entry in MANIFEST["requirements"].values():
            yield from entry["tests"]

    def test_every_referenced_test_exists(self) -> None:
        for dotted in self.all_test_ids():
            self.assertTrue(callable(_resolve(dotted)), dotted)

    def test_requirement_ids_exist_in_contract_and_statuses_valid(self) -> None:
        for req, entry in MANIFEST["requirements"].items():
            self.assertRegex(req, r"^REQ-\d{3}$")
            self.assertIn(f"{req}", CONTRACT, req)
            self.assertIn(entry["status"], STATUSES)
            self.assertTrue(entry["tests"], req)

    def test_unclaimed_requirements_stay_unclaimed(self) -> None:
        for req in ("REQ-058", "REQ-057", "REQ-060"):
            self.assertNotIn(req, MANIFEST["requirements"])
            self.assertIn(req, MANIFEST["not_claimed"])

    def test_all_evidence_is_offline_and_none_is_hardware(self) -> None:
        entries = MANIFEST["local_semantics"] + list(MANIFEST["requirements"].values())
        self.assertTrue(all(entry["evidence"] == "offline" for entry in entries))
        self.assertEqual(MANIFEST["evidence_classes"]["hardware"].split(".")[0], "None")

    def test_req_018_and_064_are_backed_by_behavioral_tests(self) -> None:
        """Both the Wave 1 and Wave 2 manifests must cite behavior, not only wording."""
        behavioral = "tests.test_preview_boundaries.PreviewEvidenceOnlyTests."
        wave1 = json.loads((ROOT / "tests" / "preview_wave1_traceability.json").read_text(encoding="utf-8"))
        for manifest in (wave1, MANIFEST):
            for req in ("REQ-018", "REQ-064"):
                tests = manifest["requirements"][req]["tests"]
                self.assertTrue(any(t.startswith(behavioral) for t in tests), (manifest["wave"], req))
                for dotted in tests:
                    self.assertTrue(callable(_resolve(dotted)), dotted)

    def test_wave1_correction_note_documents_all_five_corrections(self) -> None:
        wave1 = json.loads((ROOT / "tests" / "preview_wave1_traceability.json").read_text(encoding="utf-8"))
        for req in ("REQ-018", "REQ-022", "REQ-030", "REQ-031", "REQ-064"):
            self.assertIn(req, wave1["correction_note"])

    def test_semantic_ids_unique_and_no_hardware_claims(self) -> None:
        ids = [e["id"] for e in MANIFEST["local_semantics"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(re.match(r"^PRV-ORC-\d{3}$", i) for i in ids))
        self.assertNotIn("hardware_verified", json.dumps(MANIFEST).lower())

    def test_requirements_within_db03_roadmap_scope(self) -> None:
        text = (ROOT / "ROADMAP_DEVICE_BACKEND.md").read_text(encoding="utf-8")
        stage = text.split("## DB-03", 1)[1].split("\n## DB-04", 1)[0]
        for req in MANIFEST["requirements"]:
            self.assertIn(req.split("-")[1], stage.replace("REQ-", ""), req)


if __name__ == "__main__":
    unittest.main()
