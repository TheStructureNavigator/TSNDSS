"""The read-only camera-state diagnostic: allow-list, wire frame and sanitized report, offline against scripted fakes."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from seestar_support import HOST, FakeSeestarTransport, ScriptedDevice, StubAuthenticator, make_config
except ImportError:  # pragma: no cover
    from tests.seestar_support import HOST, FakeSeestarTransport, ScriptedDevice, StubAuthenticator, make_config

from tsn_dss.engine.seestar_provider import TcpSeestarTransport
from tsn_dss.engine.seestar_provider.errors import SeestarMethodNotAllowed, SeestarTimeout, SeestarUnreachable
from tsn_dss.engine.seestar_provider.protocol import RpcReply, encode_read_request

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "seestar_readonly_validate.py"
spec = importlib.util.spec_from_file_location("seestar_readonly_validate", SCRIPT)
RV = importlib.util.module_from_spec(spec)
sys.modules["seestar_readonly_validate"] = RV
spec.loader.exec_module(RV)


def reply(result, code=0):
    return RpcReply("get_camera_state", code, result, None)


class WireTests(unittest.TestCase):
    def test_the_request_has_no_parameters_and_neighbouring_methods_stay_refused(self) -> None:
        self.assertEqual(json.loads(encode_read_request(4, "get_camera_state")), {"id": 4, "verify": True, "method": "get_camera_state"})
        with self.assertRaises(SeestarMethodNotAllowed):
            encode_read_request(4, "get_camera_state", {"camera": "main"})
        for method in ("get_view_state", "get_camera_info", "iscope_start_view", "iscope_stop_view", "set_setting"):
            with self.assertRaises(SeestarMethodNotAllowed, msg=method):
                encode_read_request(4, method)

    def test_the_transport_reads_once_over_a_short_lived_authenticated_connection(self) -> None:
        device = ScriptedDevice(auth="required")
        transport = TcpSeestarTransport(make_config(), authenticator=StubAuthenticator(), connect_factory=device.connect)
        got = transport.read_camera_state(HOST)
        self.assertEqual(got.result["state"], "idle")
        self.assertEqual(device.methods, ["get_verify_str", "verify_client", "pi_is_verified", "get_camera_state"])
        self.assertEqual(device.unexpected, [])
        self.assertTrue(all(s.closed for s in device.sockets))


class ReportTests(unittest.TestCase):
    def test_a_state_word_is_reported_without_attributing_it_to_a_camera(self) -> None:
        report = RV.camera_state_report(reply({"state": "idle", "name": "x", "path": "/some/where"}))
        self.assertTrue(report["rpc_ok"])
        self.assertEqual(report["state"], {"present": True, "null": False, "value": "idle"})
        self.assertEqual(report["result_keys"], ["name", "path", "state"])
        self.assertEqual(report["other_key_types"], {"name": "str", "path": "str"})  # types only: a path or name is never copied
        self.assertNotIn("/some/where", json.dumps(report))
        self.assertIn("not determined", report["camera_scope"])

    def test_missing_null_and_unexpected_types_are_not_turned_into_a_state(self) -> None:
        self.assertEqual(RV.camera_state_report(reply({"name": "x"}))["state"], {"present": False})
        self.assertEqual(RV.camera_state_report(reply({"state": None}))["state"], {"present": True, "null": True})
        odd = RV.camera_state_report(reply({"state": {"a": 1}, "other": 5}))
        self.assertEqual(odd["state"], {"present": True, "null": False, "value_type": "dict"})
        long = RV.camera_state_report(reply({"state": "x" * 100}))
        self.assertEqual(long["state"]["value_type"], "str")
        self.assertNotIn("x" * 50, json.dumps(long))
        for report in (RV.camera_state_report(reply({"name": "x"})), RV.camera_state_report(reply({"state": None}))):
            self.assertNotIn("value", report["state"])

    def test_per_camera_structure_is_described_by_key_names_only(self) -> None:
        report = RV.camera_state_report(reply({"main": {"state": "idle", "secret": 1}, "wide": {"state": "working"}}))
        self.assertEqual(report["state"], {"present": False})  # a nested state is not promoted to the top level
        self.assertEqual(report["nested_keys"], {"main": ["secret", "state"], "wide": ["state"]})
        self.assertNotIn("working", json.dumps(report))

    def test_other_result_shapes(self) -> None:
        self.assertEqual(RV.camera_state_report(reply(None))["result_type"], "NoneType")
        as_list = RV.camera_state_report(reply(["idle", 3, None]))
        self.assertEqual((as_list["list_length"], as_list["item_types"]), (3, ["str", "int", "NoneType"]))
        self.assertNotIn("idle", json.dumps(as_list))
        bare = RV.camera_state_report(reply("idle"))
        self.assertEqual(bare["string_value"]["value"], "idle")
        self.assertNotIn("state", bare)  # a bare string is shown as such, never relabelled as a camera state
        self.assertNotIn("secret", json.dumps(RV.camera_state_report(reply("a secret: with spaces"))))

    def test_a_device_error_code(self) -> None:
        report = RV.camera_state_report(reply(None, code=103))
        self.assertEqual((report["rpc_ok"], report["code"]), (False, 103))


class CamFake(FakeSeestarTransport):
    def __init__(self, result=None, code=0, error=None) -> None:
        super().__init__()
        self.cam = (result if result is not None else {"state": "idle"}, code, error)

    def read_camera_state(self, host):
        self.calls.append(("read_camera_state", host))
        result, code, error = self.cam
        if error is not None:
            raise error
        return reply(result, code)


class ToolTests(unittest.TestCase):
    def _run(self, transport, *, flag=True, overwrite=False, preexisting=False):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "placeholder.pem"
            key.write_text("placeholder, never parsed", encoding="utf-8")
            out = Path(tmp) / "report.json"
            cam = Path(tmp) / "camera_state_report.json"
            if preexisting:
                cam.write_text("keep", encoding="utf-8")
            with mock.patch.dict(os.environ, {"TSNDSS_TEST_KEY": str(key)}), \
                    mock.patch.object(RV, "TcpSeestarTransport", lambda *a, **k: transport), \
                    mock.patch.object(RV, "RsaKeyFileAuthenticator", lambda path: object()), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = RV.run(HOST, "TSNDSS_TEST_KEY", out, False, overwrite, False, flag)
            return (code, out.read_text(encoding="utf-8") if out.exists() else None,
                    cam.read_text(encoding="utf-8") if cam.exists() else None, str(key))

    def test_opt_in_writes_a_separate_report_and_leaves_the_audited_report_alone(self) -> None:
        transport = CamFake()
        code, main_report, cam, key = self._run(transport)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(cam)["state"]["value"], "idle")
        self.assertEqual([c for c in transport.calls if c[0] == "read_camera_state"], [("read_camera_state", HOST)])
        for text in (cam, main_report):
            for secret in (HOST, key):
                self.assertNotIn(secret, text)
        self.assertNotIn("camera_state", main_report)

    def test_without_the_flag_nothing_is_read_or_written(self) -> None:
        transport = CamFake()
        code, _, cam, _ = self._run(transport, flag=False)
        self.assertEqual(code, 0)
        self.assertIsNone(cam)
        self.assertFalse(any(c[0] == "read_camera_state" for c in transport.calls))

    def test_rpc_failure_is_a_category_and_does_not_fail_the_validation(self) -> None:
        for error, category in ((SeestarTimeout("read_timeout"), "read_timeout"), (SeestarUnreachable("connect_failed"), "connect_failed")):
            code, _, cam, _ = self._run(CamFake(error=error))
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(cam), {"rpc_ok": False, "error_category": category})

    def test_an_existing_report_is_not_replaced_without_overwrite(self) -> None:
        transport = CamFake()
        code, main_report, cam, _ = self._run(transport, preexisting=True)
        self.assertEqual((code, main_report, cam), (2, None, "keep"))
        self.assertEqual(transport.calls, [])

    def test_the_diagnostic_changes_no_gate_and_the_tool_has_no_control_method(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        for forbidden in ("scope_goto", "scope_sync", "scope_park", "scope_move", "iscope_start_view", "iscope_stop_view",
                          "send_command", "seestar_control", "set_track"):
            self.assertNotIn(forbidden, source)
        from tsn_dss.engine.seestar_provider.normalize import APP_ITEM_NAMES, DEVICE_ITEM_NAMES

        self.assertFalse([n for n in (*APP_ITEM_NAMES, *DEVICE_ITEM_NAMES) if "camera_state" in n])  # canonical telemetry untouched


if __name__ == "__main__":
    unittest.main()
