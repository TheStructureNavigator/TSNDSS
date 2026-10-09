"""The operator validation script is exercised offline with the fake transport (no network, no key)."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tsn_dss.engine.seestar_provider import SeestarUnreachable

try:
    from seestar_support import HOST, FakeSeestarTransport
except ModuleNotFoundError:  # pragma: no cover
    from tests.seestar_support import HOST, FakeSeestarTransport

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "seestar_readonly_validate.py"


def load_script():
    spec = importlib.util.spec_from_file_location("seestar_readonly_validate", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ValidationScriptTests(unittest.TestCase):
    def _run(self, transport: FakeSeestarTransport, with_key: bool = True):
        module = load_script()
        with tempfile.TemporaryDirectory() as tmp:
            key_file = Path(tmp) / "placeholder.pem"
            key_file.write_text("placeholder, never parsed", encoding="utf-8")
            out = Path(tmp) / "report.json"
            env = {"TSNDSS_TEST_KEY": str(key_file)} if with_key else {}
            with mock.patch.dict(os.environ, env, clear=False), \
                    mock.patch.object(module, "TcpSeestarTransport", lambda *a, **k: transport), \
                    mock.patch.object(module, "RsaKeyFileAuthenticator", lambda path: object()):
                with contextlib.redirect_stdout(io.StringIO()):
                    code = module.run(HOST, "TSNDSS_TEST_KEY", out, udp=False)
            report = out.read_text(encoding="utf-8") if out.exists() else ""
            return code, report, str(key_file)

    def test_full_validation_passes_on_a_healthy_fake_and_report_leaks_nothing(self) -> None:
        transport = FakeSeestarTransport()
        code, report, key_path = self._run(transport)
        self.assertEqual(code, 0, report)
        data = json.loads(report)
        self.assertTrue(data["all_passed"])
        self.assertGreaterEqual(len(data["steps"]), 9)
        fingerprints = [step["device_fingerprint"] for step in data["steps"] if "device_fingerprint" in step]
        self.assertEqual([len(f) for f in fingerprints], [4])  # too short to recover the serial by search
        for secret in (HOST, key_path, "0badc0de", "SYNTHETIC-NOT-A-SECRET", "SYNTHETIC-AP", "192.0.2.1"):
            self.assertNotIn(secret, report)
        self.assertTrue({c[0] for c in transport.calls} <= {"read_device_state", "read_app_state", "test_connection"})

    def test_missing_credentials_abort_before_any_device_call(self) -> None:
        transport = FakeSeestarTransport()
        code, report, _ = self._run(transport, with_key=False)
        self.assertEqual(code, 2)
        self.assertEqual(transport.calls, [])

    def test_unreachable_device_fails_cleanly_without_crashing(self) -> None:
        transport = FakeSeestarTransport()
        transport.fail_next("device_state", SeestarUnreachable("connect_failed"))
        code, report, _ = self._run(transport)
        self.assertEqual(code, 1)
        self.assertNotIn(HOST, report)

    def test_changed_mount_state_between_samples_is_reported_as_a_failure(self) -> None:
        transport = FakeSeestarTransport()
        original = transport.read_device_state
        counter = {"n": 0}

        def drifting(host, keys):
            reply = original(host, keys)
            if tuple(keys) != ("device",):
                counter["n"] += 1
                if counter["n"] >= 3:  # the sample taken after the session
                    reply = type(reply)(reply.method, reply.code, {**reply.result, "mount": {**reply.result["mount"], "close": False}}, reply.device_timestamp)
            return reply

        transport.read_device_state = drifting  # type: ignore[method-assign]
        code, report, _ = self._run(transport)
        self.assertEqual(code, 1)
        steps = {s["step"]: s["ok"] for s in json.loads(report)["steps"]}
        self.assertFalse(steps["mount and view state unchanged across the session"])


class OutputOverwriteTests(unittest.TestCase):
    def _run_to(self, out: Path, **kwargs):
        module = load_script()
        transport = FakeSeestarTransport()
        key_file = out.parent / "placeholder.pem"
        key_file.write_text("placeholder, never parsed", encoding="utf-8")
        with mock.patch.dict(os.environ, {"TSNDSS_TEST_KEY": str(key_file)}, clear=False), \
                mock.patch.object(module, "TcpSeestarTransport", lambda *a, **k: transport), \
                mock.patch.object(module, "RsaKeyFileAuthenticator", lambda path: object()), \
                contextlib.redirect_stdout(io.StringIO()):
            code = module.run(HOST, "TSNDSS_TEST_KEY", out, udp=False, **kwargs)
        return code, transport

    def test_existing_report_is_not_replaced_and_no_device_call_is_made(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.json"
            out.write_text("previous evidence", encoding="utf-8")
            code, transport = self._run_to(out)
            self.assertEqual(code, 2)
            self.assertEqual(out.read_text(encoding="utf-8"), "previous evidence")
            self.assertEqual(transport.calls, [])

    def test_explicit_overwrite_replaces_the_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.json"
            out.write_text("previous evidence", encoding="utf-8")
            code, _ = self._run_to(out, overwrite=True)
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(out.read_text(encoding="utf-8"))["all_passed"])

    def test_a_new_report_name_needs_no_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, _ = self._run_to(Path(tmp) / "fresh.json")
            self.assertEqual(code, 0)

    def test_a_directory_is_never_a_valid_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, transport = self._run_to(Path(tmp), overwrite=True)
            self.assertEqual(code, 2)
            self.assertEqual(transport.calls, [])

    def test_command_line_exposes_the_overwrite_option_and_refuses_before_credentials(self) -> None:
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.json"
            out.write_text("previous evidence", encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if k != "TSNDSS_SEESTAR_KEY_PATH"}
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--host", "192.0.2.1", "--out", str(out)],
                capture_output=True, text=True, env=env, timeout=30,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("--overwrite", result.stdout)
            self.assertEqual(out.read_text(encoding="utf-8"), "previous evidence")
            self.assertIn("--overwrite", subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30).stdout)


if __name__ == "__main__":
    unittest.main()
