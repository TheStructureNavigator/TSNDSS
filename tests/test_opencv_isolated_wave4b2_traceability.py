"""Validates the DB-03 Wave 4B-2 manifest against the contract text and the test code."""

from __future__ import annotations

import importlib
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "tests" / "opencv_isolated_wave4b2_traceability.json").read_text(encoding="utf-8"))
PREVIOUS = json.loads((ROOT / "tests" / "opencv_isolated_wave4b1_traceability.json").read_text(encoding="utf-8"))
CONTRACT = (ROOT / "Contracts" / "DSS-CTR-013-Device-Provider-Runtime-Contract.md").read_text(encoding="utf-8")
KEYWORDS = {
    "REQ-022": "Passive reads **SHALL NOT** create or submit Commands",
    "REQ-052": "SHALL NOT** own Project, Session, Observation",
    "REQ-067": "Acquisition Execution work",
}


def resolve(dotted: str):
    module_name, cls_name, method = dotted.rsplit(".", 2)
    return getattr(getattr(importlib.import_module(module_name), cls_name), method)


class WaveFourB2TraceabilityTests(unittest.TestCase):
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

    def test_all_evidence_is_offline_and_nothing_is_claimed_for_hardware_or_windows(self) -> None:
        self.assertTrue(all(entry["evidence"] == "offline" for entry in self.entries()))
        self.assertEqual(MANIFEST["hardware_claims"], [])
        for key in ("windows_verified", "cleanup_guarantee", "crc_is_not_security", "sandbox", "memory_containment", "hardware", "decoder"):
            self.assertIn(key, MANIFEST["not_claimed"])
        self.assertIn("UNVERIFIED", MANIFEST["windows_status"]["this_wave"])
        self.assertNotIn("hardware_verified", json.dumps(MANIFEST).lower())

    def test_semantic_ids_continue_the_4b1_sequence_without_overlap(self) -> None:
        ids = [e["id"] for e in MANIFEST["local_semantics"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(re.match(r"^PRV-ISO-\d{3}$", i) for i in ids))
        previous = {e["id"] for e in PREVIOUS["local_semantics"]}
        self.assertFalse(previous & set(ids))
        self.assertEqual(min(ids), "PRV-ISO-%03d" % (len(previous) + 1))

    def test_the_owner_conditions_are_each_traced(self) -> None:
        text = {e["id"]: " ".join(e["tests"]) for e in MANIFEST["local_semantics"]}
        joined = " ".join(text.values())
        for needle in ("test_a_confirmed_exit_recovers_the_slot_explicitly", "test_timeout_kill_and_a_lost_pipe_do_not_free_the_slot",
                       "test_concurrent_starts_never_exceed_the_limit", "test_the_mapping_is_closed_only_after_an_active_copy_has_finished",
                       "test_stop_from_another_thread_never_uses_a_closed_buffer", "test_a_stream_is_not_open_just_because_the_worker_is_ready",
                       "test_the_state_stays_readable_while_an_operation_hangs", "test_a_worker_process_that_attaches_and_exits_does_not_destroy_the_segment",
                       "test_unpack_rejects_bad_headers", "test_the_crc_is_checked_on_the_parents_copy_not_on_the_segment"):
            self.assertIn(needle, joined, needle)

    def test_requirements_within_db03_roadmap_scope(self) -> None:
        text = (ROOT / "ROADMAP_DEVICE_BACKEND.md").read_text(encoding="utf-8")
        stage = text.split("## DB-03", 1)[1].split("\n## DB-04", 1)[0]
        for req in MANIFEST["requirements"]:
            self.assertIn(req, stage, req)


if __name__ == "__main__":
    unittest.main()
