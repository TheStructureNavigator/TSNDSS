"""Guards the REQ-018 documentation correction in the closed DB-02 records (documentation only)."""

from __future__ import annotations

import importlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "tests" / "seestar_db02_traceability.json").read_text(encoding="utf-8"))
RECORD = (ROOT / "docs" / "DB-02_ACCEPTANCE_RECORD.md").read_text(encoding="utf-8")
CONTRACT = (ROOT / "Contracts" / "DSS-CTR-013-Device-Provider-Runtime-Contract.md").read_text(encoding="utf-8")
ENTRY = MANIFEST["requirements"]["DSS-CTR-013-REQ-018"]


def _resolve(dotted: str):
    module_name, cls_name, method = dotted.rsplit(".", 2)
    return getattr(getattr(importlib.import_module(module_name), cls_name), method)


class Req018CorrectionTests(unittest.TestCase):
    def test_contract_wording_is_the_one_the_correction_relies_on(self) -> None:
        line = next(l for l in CONTRACT.splitlines() if "DSS-CTR-013-REQ-018:" in l)
        self.assertIn("Preview data **SHALL NOT** automatically become Capture, Frame, Dataset, ProcessingRun, Observation or Session state", line)

    def test_hardware_availability_read_is_not_classified_as_req_018_evidence(self) -> None:
        self.assertEqual(ENTRY["evidence"]["hardware"], "not_applicable")
        self.assertNotIn("hardware_steps", ENTRY["evidence"])
        self.assertIn("S7", ENTRY["evidence"]["hardware_note"])  # the historical fact is kept, attributed to REQ-016
        self.assertIn("REQ-016", ENTRY["evidence"]["hardware_note"])

    def test_manifest_cites_the_architecture_tests(self) -> None:
        required = {
            "tests.test_seestar_boundaries.OwnershipBoundaryTests.test_full_exercise_leaves_the_canonical_database_untouched",
            "tests.test_seestar_boundaries.OwnershipBoundaryTests.test_runtime_evidence_has_no_domain_identity_fields",
            "tests.test_seestar_boundaries.ImportBoundaryTests.test_no_domain_storage_gui_mcp_or_legacy_adapter_imports",
        }
        self.assertTrue(required <= set(ENTRY["tests"]))
        for dotted in ENTRY["tests"]:
            self.assertTrue(callable(_resolve(dotted)), dotted)

    def test_record_no_longer_calls_req_018_availability_evidence(self) -> None:
        self.assertNotIn("| REQ-018 | exercised |", RECORD)
        self.assertIn("Boundaries: REQ-018, 019,", RECORD)
        self.assertIn("not evidence for REQ-018", RECORD)
        self.assertIn("Correction note", RECORD)

    def test_status_and_scope_are_unchanged(self) -> None:
        self.assertEqual(MANIFEST["acceptance"]["stage_status"], "HARDWARE_VERIFIED (read-only scope)")
        self.assertIn("**Status: HARDWARE_VERIFIED (read-only scope).**", RECORD)
        self.assertEqual(ENTRY["status"], "boundary_offline")
        self.assertEqual(ENTRY["classification"], "boundary")


if __name__ == "__main__":
    unittest.main()
