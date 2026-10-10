"""The read-only RA/Dec diagnostic: allow-list, wire frame and the sanitized report, all offline against scripted fakes."""

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
    return RpcReply("scope_get_equ_coord", code, result, None)


class WireTests(unittest.TestCase):
    def test_the_request_has_no_parameters_and_nothing_else_became_allowed(self) -> None:
        raw = json.loads(encode_read_request(3, "scope_get_equ_coord"))
        self.assertEqual(raw, {"id": 3, "verify": True, "method": "scope_get_equ_coord"})
        with self.assertRaises(SeestarMethodNotAllowed):
            encode_read_request(3, "scope_get_equ_coord", {"ra": 1})
        for method in ("scope_goto", "scope_sync", "scope_get_horiz_coord", "scope_set_track_state"):
            with self.assertRaises(SeestarMethodNotAllowed, msg=method):
                encode_read_request(3, method)

    def test_the_transport_reads_once_over_a_short_lived_authenticated_connection(self) -> None:
        device = ScriptedDevice(auth="required")
        transport = TcpSeestarTransport(make_config(), authenticator=StubAuthenticator(), connect_factory=device.connect)
        got = transport.read_equ_coord(HOST)
        self.assertEqual(got.code, 0)
        self.assertEqual(got.result, {"ra": 5.5, "dec": -5.25})
        self.assertEqual([m for m in device.methods], ["get_verify_str", "verify_client", "pi_is_verified", "scope_get_equ_coord"])
        self.assertEqual(device.unexpected, [])
        self.assertTrue(all(s.closed for s in device.sockets))


class ReportTests(unittest.TestCase):
    def test_success_reports_the_numbers_as_sent_without_conversion(self) -> None:
        report = RV.equ_coord_report(reply({"ra": 5.5, "dec": -5.25}))
        self.assertTrue(report["rpc_ok"])
        self.assertEqual(report["result_keys"], ["dec", "ra"])
        self.assertEqual(report["fields"], {"ra": {"present": True, "null": False, "value": 5.5},
                                            "dec": {"present": True, "null": False, "value": -5.25}})
        self.assertEqual(report["units_documented_unverified"], {"ra": "hours", "dec": "degrees"})
        self.assertEqual(report["epoch"], "not stated by any source")
        self.assertTrue(report["within_documented_ranges"])

    def test_out_of_range_numbers_are_flagged_not_changed(self) -> None:
        report = RV.equ_coord_report(reply({"ra": 83.9, "dec": -5.25}))
        self.assertEqual(report["fields"]["ra"]["value"], 83.9)  # e.g. degrees instead of hours: reported, never converted
        self.assertFalse(report["within_documented_ranges"])

    def test_missing_null_and_odd_fields_stay_distinct(self) -> None:
        report = RV.equ_coord_report(reply({"ra": None, "extra": "x"}))
        self.assertEqual(report["fields"]["ra"], {"present": True, "null": True})
        self.assertEqual(report["fields"]["dec"], {"present": False})
        self.assertEqual(report["other_key_types"], {"extra": "str"})
        self.assertNotIn("within_documented_ranges", report)
        odd = RV.equ_coord_report(reply({"ra": "5.5", "dec": True}))
        self.assertEqual(odd["fields"]["ra"]["value_type"], "str")
        self.assertEqual(odd["fields"]["dec"]["value_type"], "bool")
        self.assertNotIn("5.5", json.dumps(odd))  # a non-number is typed, never copied

    def test_malformed_results(self) -> None:
        self.assertEqual(RV.equ_coord_report(reply(None))["result_type"], "NoneType")
        as_list = RV.equ_coord_report(reply([5.5, -5.25, 12.0]))
        self.assertEqual(as_list["list_length"], 3)  # the firmware's other shape: reported as a list, not interpreted
        self.assertEqual([i["value"] for i in as_list["items"]], [5.5, -5.25, 12.0])
        self.assertNotIn("fields", as_list)
        text = RV.equ_coord_report(reply("a secret string"))
        self.assertEqual(text["result_type"], "str")
        self.assertNotIn("secret", json.dumps(text))
        nan = RV.equ_coord_report(reply({"ra": float("nan"), "dec": 1}))
        self.assertEqual(nan["fields"]["ra"]["value_type"], "non_finite_number")

    def test_a_device_error_code(self) -> None:
        report = RV.equ_coord_report(reply(None, code=103))
        self.assertFalse(report["rpc_ok"])
        self.assertEqual(report["code"], 103)


class EquFake(FakeSeestarTransport):
    """The scripted read transport plus the one new typed read."""

    def __init__(self, result=None, code=0, error=None) -> None:
        super().__init__()
        self.equ = (result if result is not None else {"ra": 5.5, "dec": -5.25}, code, error)

    def read_equ_coord(self, host):
        self.calls.append(("read_equ_coord", host))
        result, code, error = self.equ
        if error is not None:
            raise error
        return reply(result, code)


class ToolTests(unittest.TestCase):
    def _run(self, transport, *, flag=True, overwrite=False, preexisting=False):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "placeholder.pem"
            key.write_text("placeholder, never parsed", encoding="utf-8")
            out = Path(tmp) / "report.json"
            equ = Path(tmp) / "equ_coord_report.json"
            if preexisting:
                equ.write_text("keep", encoding="utf-8")
            with mock.patch.dict(os.environ, {"TSNDSS_TEST_KEY": str(key)}), \
                    mock.patch.object(RV, "TcpSeestarTransport", lambda *a, **k: transport), \
                    mock.patch.object(RV, "RsaKeyFileAuthenticator", lambda path: object()), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = RV.run(HOST, "TSNDSS_TEST_KEY", out, False, overwrite, flag)
            return (code, out.read_text(encoding="utf-8") if out.exists() else None,
                    equ.read_text(encoding="utf-8") if equ.exists() else None, str(key))

    def test_success_writes_a_separate_sanitized_report(self) -> None:
        transport = EquFake()
        code, main_report, equ, key = self._run(transport)
        self.assertEqual(code, 0)
        data = json.loads(equ)
        self.assertEqual(data["fields"]["ra"]["value"], 5.5)
        self.assertEqual([c for c in transport.calls if c[0] == "read_equ_coord"], [("read_equ_coord", HOST)])
        for text in (equ, main_report):
            for secret in (HOST, key):
                self.assertNotIn(secret, text)
        self.assertNotIn("5.5", main_report)  # the audited report keeps its shape: no coordinates in it
        self.assertNotIn("equ", main_report)

    def test_without_the_flag_nothing_is_read_or_written(self) -> None:
        transport = EquFake()
        code, _, equ, _ = self._run(transport, flag=False)
        self.assertEqual(code, 0)
        self.assertIsNone(equ)
        self.assertFalse(any(c[0] == "read_equ_coord" for c in transport.calls))

    def test_rpc_failure_is_reported_by_category_and_does_not_fail_the_validation(self) -> None:
        for error, category in ((SeestarTimeout("read_timeout"), "read_timeout"), (SeestarUnreachable("connect_failed"), "connect_failed")):
            code, _, equ, _ = self._run(EquFake(error=error))
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(equ), {"rpc_ok": False, "error_category": category})

    def test_device_error_and_malformed_replies_are_written_as_structure_only(self) -> None:
        _, _, equ, _ = self._run(EquFake(result={"x": 1}, code=103))
        data = json.loads(equ)
        self.assertEqual((data["rpc_ok"], data["code"], data["fields"]["ra"]), (False, 103, {"present": False}))
        _, _, equ, _ = self._run(EquFake(result="garbage"))
        self.assertEqual(json.loads(equ)["result_type"], "str")

    def test_an_existing_coordinate_report_is_not_replaced_without_overwrite(self) -> None:
        transport = EquFake()
        code, main_report, equ, _ = self._run(transport, preexisting=True)
        self.assertEqual((code, main_report, equ), (2, None, "keep"))
        self.assertEqual(transport.calls, [])  # refused before any contact

    def test_the_tool_contains_no_motion_or_control_method(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        for forbidden in ("scope_goto", "scope_sync", "scope_park", "scope_move", "iscope_start_view", "iscope_stop_view",
                          "send_command", "seestar_control", "set_track"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
