import hashlib
import importlib
import inspect
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "tests" / "contract_traceability.json"
EXPECTED_CONTRACTS = {f"DSS-CTR-{number:03d}" for number in range(1, 13)}
CLASSIFICATIONS = {"mechanical", "boundary", "compatibility", "policy"}
STATUSES = {
    "verified",
    "partially_verified",
    "not_verified",
    "policy_only",
    "not_implemented_by_adjudication",
    "environment_blocked",
}
RATIONALE_STATUSES = STATUSES - {"verified"}
TEST_REF_RE = re.compile(r"^tests\.[A-Za-z0-9_]+\.[A-Za-z_][A-Za-z0-9_]*\.test_[A-Za-z0-9_]+$")


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _excerpt_hash(lines: list[str]) -> str:
    digest = hashlib.sha256(_normalise("\n".join(lines)).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"




def _is_boundary(line: str) -> bool:
    stripped = line.strip()
    return not stripped or set(stripped) <= {"-"} or stripped.startswith("#")


def _is_bullet(line: str) -> bool:
    return line.lstrip().startswith(("- ", "* ")) or re.match(r"\s*\d+\.\s+", line) is not None


def _has_normative_anchor(line: str) -> bool:
    return re.search(r"\b(SHALL NOT|MUST NOT|SHALL|MUST)\b", line, flags=re.IGNORECASE) is not None


def _requirement_range(lines: list[str], anchor_index: int) -> tuple[int, int]:
    """Return a conservative complete obligation range for a normative anchor.

    This is intentionally a small mechanical Markdown-aware check, not a general
    semantic parser. It covers wrapped paragraphs and dependent bullet lists so
    that a manifest cannot pass by hashing only the physical SHALL/MUST line.
    """
    start = anchor_index
    while start > 0 and not _is_boundary(lines[start - 1]) and not _is_bullet(lines[start - 1]):
        start -= 1

    end = anchor_index
    while end + 1 < len(lines) and not _is_boundary(lines[end + 1]) and not _is_bullet(lines[end + 1]):
        end += 1

    if lines[end].strip().endswith(":"):
        pos = end + 1
        while pos < len(lines) and not lines[pos].strip():
            pos += 1
        while pos < len(lines):
            if not lines[pos].strip():
                break
            if _is_boundary(lines[pos]) and not _is_bullet(lines[pos]):
                break
            end = pos
            pos += 1

    if lines[anchor_index].lstrip().startswith(">"):
        start = anchor_index
        while start > 0 and lines[start - 1].lstrip().startswith(">"):
            start -= 1
        end = anchor_index
        while end + 1 < len(lines) and lines[end + 1].lstrip().startswith(">"):
            end += 1

    return start + 1, end + 1


def _normative_anchor_ranges(lines: list[str]) -> list[tuple[int, int]]:
    return [_requirement_range(lines, index) for index, line in enumerate(lines) if _has_normative_anchor(line)]


def _load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _resolve_test_reference(reference: str) -> None:
    if not TEST_REF_RE.match(reference):
        raise AssertionError(f"invalid test reference format: {reference}")
    module_name, class_name, method_name = reference.rsplit(".", 2)
    module = importlib.import_module(module_name)
    test_class = getattr(module, class_name)
    if not inspect.isclass(test_class) or not issubclass(test_class, unittest.TestCase):
        raise AssertionError(f"reference does not name a unittest.TestCase: {reference}")
    method = getattr(test_class, method_name)
    if not callable(method):
        raise AssertionError(f"reference does not name a callable test method: {reference}")


class ContractTraceabilityManifestTests(unittest.TestCase):
    def test_manifest_integrity_and_source_hashes(self) -> None:
        manifest = _load_manifest()
        self.assertEqual(manifest.get("schema_version"), 1)
        self.assertIsInstance(manifest.get("contracts"), list)

        contract_ids = [contract.get("contract_id") for contract in manifest["contracts"]]
        self.assertEqual(set(contract_ids), EXPECTED_CONTRACTS)
        self.assertEqual(len(contract_ids), len(set(contract_ids)))

        requirement_ids: set[str] = set()
        for contract in manifest["contracts"]:
            contract_id = contract["contract_id"]
            self.assertRegex(contract_id, r"^DSS-CTR-\d{3}$")
            source_path = contract.get("source_path")
            self.assertIsInstance(source_path, str)
            source_file = (ROOT / source_path).resolve()
            self.assertTrue(source_file.exists(), source_path)
            self.assertIsInstance(contract.get("status"), str)
            self.assertTrue(contract["status"].strip(), contract_id)
            self.assertIsInstance(contract.get("requirements"), list)
            self.assertGreater(len(contract["requirements"]), 0, contract_id)

            source_lines = source_file.read_text(encoding="utf-8").splitlines()
            expected_ranges = _normative_anchor_ranges(source_lines)
            actual_ranges = [
                (requirement["source"]["line_start"], requirement["source"]["line_end"])
                for requirement in contract["requirements"]
            ]
            self.assertEqual(actual_ranges, expected_ranges, contract_id)

            expected_index = 1
            for requirement in contract["requirements"]:
                requirement_id = requirement.get("requirement_id")
                self.assertEqual(requirement_id, f"{contract_id}.REQ-{expected_index:03d}")
                expected_index += 1
                self.assertNotIn(requirement_id, requirement_ids)
                requirement_ids.add(requirement_id)

                self.assertIsInstance(requirement.get("summary"), str)
                self.assertTrue(requirement["summary"].strip(), requirement_id)
                self.assertIn(requirement.get("classification"), CLASSIFICATIONS, requirement_id)
                self.assertIn(requirement.get("verification_status"), STATUSES, requirement_id)

                source = requirement.get("source")
                self.assertIsInstance(source, dict, requirement_id)
                line_start = source.get("line_start")
                line_end = source.get("line_end")
                self.assertIsInstance(line_start, int, requirement_id)
                self.assertIsInstance(line_end, int, requirement_id)
                self.assertGreaterEqual(line_start, 1, requirement_id)
                self.assertGreaterEqual(line_end, line_start, requirement_id)
                self.assertLessEqual(line_end, len(source_lines), requirement_id)
                excerpt = source_lines[line_start - 1 : line_end]
                self.assertEqual(source.get("excerpt_hash"), _excerpt_hash(excerpt), requirement_id)

                tests = requirement.get("tests")
                self.assertIsInstance(tests, list, requirement_id)
                self.assertEqual(len(tests), len(set(tests)), requirement_id)
                for test_reference in tests:
                    _resolve_test_reference(test_reference)

                verification_status = requirement["verification_status"]
                if verification_status == "verified":
                    self.assertGreater(len(tests), 0, requirement_id)
                if verification_status in RATIONALE_STATUSES:
                    self.assertIsInstance(requirement.get("rationale"), str, requirement_id)
                    self.assertTrue(requirement["rationale"].strip(), requirement_id)

    def test_manifest_reports_coverage_gaps_without_hiding_them(self) -> None:
        manifest = _load_manifest()
        statuses = {
            requirement["verification_status"]
            for contract in manifest["contracts"]
            for requirement in contract["requirements"]
        }
        self.assertIn("verified", statuses)
        self.assertTrue(statuses <= STATUSES)
        self.assertTrue(
            statuses & RATIONALE_STATUSES,
            "manifest should distinguish reported conformance gaps from fully verified requirements",
        )


if __name__ == "__main__":
    unittest.main()
