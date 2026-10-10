"""Read-only Seestar location/time diagnostics, offline against scripted fakes."""

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
    from seestar_support import HOST, ScriptedDevice, StubAuthenticator, make_config
except ImportError:  # pragma: no cover
    from tests.seestar_support import HOST, ScriptedDevice, StubAuthenticator, make_config

from tsn_dss.engine.seestar_provider import TcpSeestarTransport
from tsn_dss.engine.seestar_provider.errors import SeestarMethodNotAllowed, SeestarTimeout
from tsn_dss.engine.seestar_provider.protocol import RpcReply, encode_read_request

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "seestar_location_time_diagnostic.py"
spec = importlib.util.spec_from_file_location("seestar_location_time_diagnostic", SCRIPT)
DIAG = importlib.util.module_from_spec(spec)
sys.modules["seestar_location_time_diagnostic"] = DIAG
spec.loader.exec_module(DIAG)


def reply(method, result, code=0):
    return RpcReply(method, code, result, None)


class WireTests(unittest.TestCase):
    def test_read_requests_have_no_parameters_and_are_allow_listed(self) -> None:
        self.assertEqual(json.loads(encode_read_request(10, "get_user_location")), {"id": 10, "verify": True, "method": "get_user_location"})
        self.assertEqual(json.loads(encode_read_request(11, "pi_get_time")), {"id": 11, "verify": True, "method": "pi_get_time"})
        for method in ("set_user_location", "pi_set_time"):
            with self.assertRaises(SeestarMethodNotAllowed, msg=method):
                encode_read_request(10, method)

    def test_transport_reads_over_short_lived_authenticated_connections(self) -> None:
        device = ScriptedDevice(auth="required")
        transport = TcpSeestarTransport(make_config(), authenticator=StubAuthenticator(), connect_factory=device.connect)
        loc = transport.read_user_location(HOST)
        tm = transport.read_pi_time(HOST)
        self.assertEqual(loc.result, [14.7908, 47.9539])
        self.assertEqual(tm.result["time_zone"], "Europe/Warsaw")
        self.assertEqual(
            device.methods,
            [
                "get_verify_str", "verify_client", "pi_is_verified", "get_user_location",
                "get_verify_str", "verify_client", "pi_is_verified", "pi_get_time",
            ],
        )
        self.assertTrue(all(sock.closed for sock in device.sockets))
        self.assertEqual(device.unexpected, [])


class ReportTests(unittest.TestCase):
    def test_location_list_reports_order_and_ranges_without_conversion(self) -> None:
        report = DIAG.user_location_report(reply("get_user_location", [14.7908, 47.9539]))
        self.assertTrue(report["rpc_ok"])
        self.assertEqual(report["order_documented_unverified"], ["longitude_deg", "latitude_deg"])
        self.assertEqual([item["value"] for item in report["items"]], [14.7908, 47.9539])
        self.assertTrue(report["within_earth_ranges"])

    def test_location_dict_shape_is_reported_without_guessing(self) -> None:
        report = DIAG.user_location_report(reply("get_user_location", {"lat": 47.9, "lon": 14.7, "source": "x"}))
        self.assertEqual(report["fields"]["lat"]["value"], 47.9)
        self.assertEqual(report["fields"]["lon"]["value"], 14.7)
        self.assertEqual(report["other_key_types"], {"source": "str"})

    def test_time_shape_reports_known_fields(self) -> None:
        report = DIAG.pi_time_report(reply("pi_get_time", {"year": 2026, "mon": 10, "day": 10, "hour": 22, "min": 30, "sec": 5, "time_zone": "Europe/Warsaw"}))
        self.assertTrue(report["rpc_ok"])
        self.assertEqual(report["fields"]["year"]["value"], 2026.0)
        self.assertEqual(report["fields"]["time_zone"]["value"], "Europe/Warsaw")

    def test_malformed_values_are_typed_not_interpreted(self) -> None:
        loc = DIAG.user_location_report(reply("get_user_location", ["14.7", True]))
        self.assertEqual(loc["items"], [{"present": True, "null": False, "value_type": "str"}, {"present": True, "null": False, "value_type": "bool"}])
        tm = DIAG.pi_time_report(reply("pi_get_time", {"year": float("nan"), "time_zone": {"name": "x"}}))
        self.assertEqual(tm["fields"]["year"]["value_type"], "non_finite_number")
        self.assertEqual(tm["fields"]["time_zone"]["value_type"], "dict")
        odd = DIAG.pi_time_report(reply("pi_get_time", {"time_zone": "secret with spaces"}))
        self.assertEqual(odd["fields"]["time_zone"]["value_type"], "str")
        self.assertNotIn("secret", json.dumps(odd))


class ToolTests(unittest.TestCase):
    def test_tool_writes_one_report_and_redacts_host_and_key(self) -> None:
        device = ScriptedDevice(auth="required")
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "placeholder.pem"
            key.write_text("placeholder, never parsed", encoding="utf-8")
            out = Path(tmp) / "diag.json"
            with mock.patch.dict(os.environ, {"TSNDSS_TEST_KEY": str(key)}), \
                    mock.patch.object(DIAG, "TcpSeestarTransport", lambda *a, **k: TcpSeestarTransport(make_config(), authenticator=StubAuthenticator(), connect_factory=device.connect)), \
                    mock.patch.object(DIAG, "RsaKeyFileAuthenticator", lambda path: object()), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = DIAG.run(HOST, "TSNDSS_TEST_KEY", out)
            self.assertEqual(code, 0)
            text = out.read_text(encoding="utf-8")
            self.assertNotIn(HOST, text)
            self.assertNotIn(str(key), text)
            data = json.loads(text)
            self.assertEqual(set(data["diagnostics"]), {"user_location", "pi_time"})

    def test_rpc_failure_is_reported_by_category(self) -> None:
        class Failing:
            def read_user_location(self, host):
                raise SeestarTimeout("read_timeout")

            def read_pi_time(self, host):
                return reply("pi_get_time", {"year": 2026})

        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "placeholder.pem"
            key.write_text("placeholder", encoding="utf-8")
            out = Path(tmp) / "diag.json"
            with mock.patch.dict(os.environ, {"TSNDSS_TEST_KEY": str(key)}), \
                    mock.patch.object(DIAG, "TcpSeestarTransport", lambda *a, **k: Failing()), \
                    mock.patch.object(DIAG, "RsaKeyFileAuthenticator", lambda path: object()), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = DIAG.run(HOST, "TSNDSS_TEST_KEY", out)
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["diagnostics"]["user_location"], {"rpc_ok": False, "error_category": "read_timeout"})

    def test_existing_output_refuses_before_contact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "placeholder.pem"
            key.write_text("placeholder", encoding="utf-8")
            out = Path(tmp) / "diag.json"
            out.write_text("keep", encoding="utf-8")
            with mock.patch.dict(os.environ, {"TSNDSS_TEST_KEY": str(key)}), contextlib.redirect_stdout(io.StringIO()):
                code = DIAG.run(HOST, "TSNDSS_TEST_KEY", out)
            self.assertEqual(code, 2)
            self.assertEqual(out.read_text(encoding="utf-8"), "keep")

    def test_tool_contains_no_write_or_motion_rpc_names(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        for forbidden in ("set_user_location", "pi_set_time", "scope_goto", "scope_sync", "scope_park", "scope_move", "iscope_start_view", "iscope_stop_view", "send_command", "seestar_control"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
