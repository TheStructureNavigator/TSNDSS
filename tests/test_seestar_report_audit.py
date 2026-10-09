"""Tests for tools/seestar_report_audit.py.

Input reports are produced by the real validator against the fake transport. That is test data for the
checker only and is never hardware evidence.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tsn_dss.engine.seestar_provider import SeestarUnreachable

try:
    from seestar_support import HOST, FakeSeestarTransport
except ModuleNotFoundError:  # pragma: no cover
    from tests.seestar_support import HOST, FakeSeestarTransport

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "tools" / "seestar_report_audit.py"
VALIDATOR = ROOT / "tools" / "seestar_readonly_validate.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


audit_module = _load(AUDIT, "seestar_report_audit")
validator_module = _load(VALIDATOR, "seestar_readonly_validate_for_audit")


def make_report(directory: Path, name: str, transport: FakeSeestarTransport | None = None) -> Path:
    transport = transport or FakeSeestarTransport()
    key = directory / "placeholder.credential"
    key.write_text("placeholder", encoding="utf-8")
    out = directory / name
    with mock.patch.dict(os.environ, {"AUDIT_TEST_KEY": str(key)}, clear=False), \
            mock.patch.object(validator_module, "TcpSeestarTransport", lambda *a, **k: transport), \
            mock.patch.object(validator_module, "RsaKeyFileAuthenticator", lambda path: object()), \
            contextlib.redirect_stdout(io.StringIO()):
        validator_module.run(HOST, "AUDIT_TEST_KEY", out, udp=False, overwrite=True)
    return out


def run_audit(*paths: Path) -> tuple[int, str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = audit_module.main([str(p) for p in paths])
    return code, buffer.getvalue()


def tamper(path: Path, mutate) -> Path:
    data = json.loads(path.read_text(encoding="utf-8"))
    mutate(data)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


class HealthyAndLossReportTests(unittest.TestCase):
    def test_healthy_report_is_recognized_with_no_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = make_report(Path(tmp), "r1.json")
            code, text = run_audit(report)
        self.assertEqual(code, 0, text)
        self.assertIn("kind=full_pass", text)
        self.assertIn("steps_ok=9/9", text)
        self.assertIn("CHECK RESULT: STRUCTURE_AND_SANITIZATION_OK", text)
        self.assertIn("model: Seestar S30 Pro", text)
        self.assertIn("firmware: 9.31", text)

    def test_network_loss_report_is_classified_and_recovery_is_labelled_restart_based(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            first = make_report(directory, "a.json")
            transport = FakeSeestarTransport()
            transport.fail_next("device_state", SeestarUnreachable("connect_failed"), 2)
            lost = make_report(directory, "b.json", transport)
            third = make_report(directory, "c.json")
            for offset, path in enumerate((first, lost, third)):  # control the timeline explicitly
                os.utime(path, (1_700_000_000 + offset * 100, 1_700_000_000 + offset * 100))
            code, text = run_audit(first, lost, third)
        self.assertEqual(code, 0, text)
        self.assertIn("kind=early_stop", text)
        self.assertIn("discovery_error_class: network", text)
        self.assertIn("LOSS PATTERN: network-category discovery failure present=True; full passing session(s) after it=True", text)
        self.assertIn("recovery after a restart", text)
        self.assertIn("not automatic reconnect within one session", text)

    def test_loss_after_the_passes_is_not_reported_as_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            good = make_report(directory, "a.json")
            transport = FakeSeestarTransport()
            transport.fail_next("device_state", SeestarUnreachable("connect_failed"), 2)
            lost = make_report(directory, "b.json", transport)
            os.utime(good, (1_700_000_000, 1_700_000_000))
            os.utime(lost, (1_700_000_500, 1_700_000_500))
            _code, text = run_audit(good, lost)
        self.assertIn("present=True; full passing session(s) after it=False", text)

    def test_identical_repeat_sessions_are_informational_not_violations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            one, two = make_report(directory, "a.json"), make_report(directory, "b.json")
            code, text = run_audit(one, two)
        self.assertEqual(code, 0, text)
        self.assertIn("identical_content_to_report: 1", text)
        self.assertIn("byte-identical reports", text)

    def test_device_identity_groups_are_compared_without_printing_the_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            one = make_report(directory, "a.json")
            two = make_report(directory, "b.json")
            fingerprint = json.loads(one.read_text(encoding="utf-8"))["steps"][1]["device_fingerprint"]
            tamper(two, lambda d: d["steps"][1].__setitem__("device_fingerprint", "ffff" if fingerprint != "ffff" else "0000"))
            _code, text = run_audit(one, two)
        self.assertIn("device_identity_group: A", text)
        self.assertIn("device_identity_group: B", text)
        self.assertNotIn(fingerprint, text.replace("ffff", "").replace("0000", "") if fingerprint in ("ffff", "0000") else text)


class SanitizationDetectionTests(unittest.TestCase):
    def _audit_tampered(self, mutate):
        with tempfile.TemporaryDirectory() as tmp:
            report = make_report(Path(tmp), "r.json")
            tamper(report, mutate)
            return run_audit(report)

    def test_each_leak_class_is_detected_and_the_leaked_text_is_never_echoed(self) -> None:
        # (violation label, injected text, unique fragments that must never appear in the output)
        cases = (
            ("sanitization:ipv4_address", "10." + "20." + "30." + "40", ("10." + "20." + "30." + "40", "30." + "40")),
            ("sanitization:hex_run_8plus", "0b" + "adc0" + "de11", ("adc0de11",)),
            ("sanitization:key_or_pem_marker", "operator" + ".pe" + "m", ("operator",)),
            ("sanitization:filesystem_path", "C:" + "\\\\" + "Users" + "\\\\" + "someone", ("someone",)),
            ("sanitization:wifi_or_credential_term", "pass" + "word=hunter2", ("hunter2",)),
            ("sanitization:serial_term", "serial " + "ABCDWXYZ", ("ABCDWXYZ",)),
            ("sanitization:base64_like_24plus", "QUJDREVGR0hJSktMTU5PUFFSU1RVVldY", ("QUJDREVG", "UFFSU1RV")),
        )
        for label, payload, forbidden in cases:
            code, text = self._audit_tampered(lambda d, p=payload: d["steps"][1].__setitem__("model", p))
            self.assertEqual(code, 1, label)
            self.assertIn(label, text)
            self.assertIn("ISSUES_FOUND", text)
            self.assertIn("model: <unexpected-format>", text)
            for fragment in forbidden:
                self.assertNotIn(fragment, text, label)

    def test_leaks_in_fields_the_checker_never_prints_are_still_counted(self) -> None:
        code, text = self._audit_tampered(lambda d: d["steps"][2].__setitem__("state", "192." + "168." + "1." + "9"))
        self.assertEqual(code, 1)
        self.assertIn("sanitization:ipv4_address:1", text)
        self.assertNotIn("192." + "168." + "1." + "9", text)

    def test_unexpected_fields_and_values_are_flagged_by_name_only(self) -> None:
        code, text = self._audit_tampered(lambda d: d["steps"][2].__setitem__("raw_payload", {"x": "secret-looking-value"}))
        self.assertEqual(code, 1)
        self.assertIn("unexpected_step_keys:raw_payload", text)
        self.assertNotIn("secret-looking-value", text)
        code, text = self._audit_tampered(lambda d: d.__setitem__("debug", True))
        self.assertIn("unexpected_top_level_keys:debug", text)

    def test_unsafe_key_names_are_redacted(self) -> None:
        hostile_key = "a" * 5 + "!" + "b" * 50
        code, text = self._audit_tampered(lambda d: d["steps"][2].__setitem__(hostile_key, 1))
        self.assertIn("unexpected_step_keys:<redacted>", text)
        self.assertNotIn(hostile_key, text)

    def test_unexpected_model_format_is_not_echoed(self) -> None:
        code, text = self._audit_tampered(lambda d: d["steps"][1].__setitem__("model", "Seestar\nweird;value;with;semicolons;" + "x" * 30))
        self.assertIn("model: <unexpected-format>", text)

    def test_bad_fingerprint_length_and_charset_are_violations(self) -> None:
        for bad in ("abcdef12", "zz", "AB12"):
            code, text = self._audit_tampered(lambda d, b=bad: d["steps"][1].__setitem__("device_fingerprint", b))
            self.assertEqual(code, 1, bad)
            self.assertIn("step2.device_fingerprint:unexpected_value", text)
            self.assertNotIn(bad, text.replace("sanitization", ""))


class MissingEvidenceTests(unittest.TestCase):
    def _audit_tampered(self, mutate):
        with tempfile.TemporaryDirectory() as tmp:
            report = make_report(Path(tmp), "r.json")
            tamper(report, mutate)
            return run_audit(report)

    def test_missing_required_fields_are_listed(self) -> None:
        def drop(d):
            del d["steps"][1]["firmware"]
            del d["steps"][5]["states"]
            del d["steps"][6]["availability"]

        code, text = self._audit_tampered(drop)
        self.assertEqual(code, 1)
        for item in ("step2.firmware", "step6.states", "step7.availability"):
            self.assertIn(item, text)

    def test_capability_set_and_telemetry_arithmetic_are_enforced(self) -> None:
        code, text = self._audit_tampered(lambda d: d["steps"][4].__setitem__("supported", ["identity.read"]))
        self.assertIn("step5.supported:unexpected_value", text)
        code, text = self._audit_tampered(lambda d: d["steps"][5].__setitem__("items", 999))
        self.assertIn("telemetry_state_counts_do_not_sum_to_items", text)

    def test_failed_step_in_a_complete_sequence_is_reported(self) -> None:
        def fail(d):
            d["steps"][7]["ok"] = False
            d["all_passed"] = False

        code, text = self._audit_tampered(fail)
        self.assertIn("kind=full_with_failures", text)
        self.assertIn("steps_ok=8/9", text)

    def test_unrecognized_step_sequence_is_flagged(self) -> None:
        code, text = self._audit_tampered(lambda d: d["steps"].reverse())
        self.assertEqual(code, 1)
        self.assertIn("step_sequence_not_recognized", text)


class InputHandlingTests(unittest.TestCase):
    def test_inputs_are_never_modified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            reports = [make_report(directory, "a.json"), make_report(directory, "b.json")]
            before = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in reports}
            listing = sorted(os.listdir(directory))
            run_audit(*reports)
            after = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns) for p in reports}
            self.assertEqual(before, after)
            self.assertEqual(sorted(os.listdir(directory)), listing)

    def test_bom_prefixed_reports_from_a_windows_shell_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = make_report(Path(tmp), "r.json")
            report.write_bytes(b"\xef\xbb\xbf" + report.read_bytes())
            code, text = run_audit(report)
        self.assertEqual(code, 0, text)

    def test_unreadable_and_invalid_files_are_reported_without_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            bad = directory / "bad.json"
            bad.write_text("{not json with HOST-ish 10." + "1.2.3", encoding="utf-8")
            code, text = run_audit(bad, directory / "missing.json")
        self.assertEqual(code, 1)
        self.assertIn("not_valid_utf8_json", text)
        self.assertIn("file_not_readable", text)
        self.assertNotIn("HOST-ish", text)

    def test_wildcards_are_expanded_by_the_tool_for_cmd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            make_report(directory, "seestar_validation_report.json")
            make_report(directory, "seestar_validation_report_no.json")
            code, text = run_audit(directory / "seestar_validation_report*.json")
        self.assertEqual(code, 0, text)
        self.assertIn("[2]", text)

    def test_output_is_ascii_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = make_report(Path(tmp), "r.json")
            tamper(report, lambda d: d["steps"][1].__setitem__("model", "Seestar é中"))
            _code, text = run_audit(report)
        text.encode("ascii")

    def test_command_line_runs_as_a_script_and_never_prints_a_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = make_report(Path(tmp), "r.json")
            fingerprint = json.loads(report.read_text(encoding="utf-8"))["steps"][1]["device_fingerprint"]
            result = subprocess.run([sys.executable, str(AUDIT), str(report)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(fingerprint, result.stdout.replace("fingerprint", ""))
        self.assertEqual(result.stderr, "")


class CheckerBoundaryTests(unittest.TestCase):
    def test_checker_is_stdlib_only_has_no_network_or_process_capability_and_cannot_write(self) -> None:
        tree = ast.parse(AUDIT.read_text(encoding="utf-8"))
        imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertLessEqual(imported, {"argparse", "datetime", "glob", "hashlib", "json", "os", "re", "sys", "__future__"})
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
                mode = node.args[1].value if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) else "r"
                self.assertEqual(mode, "rb")
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, {"write_text", "write_bytes", "unlink", "remove", "rename", "replace", "rmdir",
                                             "mkdir", "system", "popen", "chmod", "truncate", "connect", "sendall", "sendto"})
        text = AUDIT.read_text(encoding="utf-8")
        for word in ("seestar_provider", "tsn_dss", "socket", "subprocess", "RpcReply"):
            self.assertNotIn(word, text, word)

    def test_audit_makes_no_network_calls(self) -> None:
        def boom(*a, **k):
            raise AssertionError("network access")

        with tempfile.TemporaryDirectory() as tmp:
            report = make_report(Path(tmp), "r.json")
            import socket

            with mock.patch.object(socket, "socket", boom), mock.patch.object(socket, "create_connection", boom), \
                    mock.patch.object(socket, "getaddrinfo", boom):
                code, _text = run_audit(report)
        self.assertEqual(code, 0)

    def test_real_reports_are_never_trackable(self) -> None:
        patterns = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("seestar_validation_report*.json", patterns)
        self.assertIn("report_audit_summary*.txt", patterns)


if __name__ == "__main__":
    unittest.main()
