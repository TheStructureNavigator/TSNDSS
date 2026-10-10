"""The read-only validator's app-state structure diagnostic: pure and offline. Absent keys, null values and present views stay distinct."""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "seestar_readonly_validate.py"
spec = importlib.util.spec_from_file_location("seestar_readonly_validate", SCRIPT)
RV = importlib.util.module_from_spec(spec)
sys.modules["seestar_readonly_validate"] = RV
spec.loader.exec_module(RV)

FIELDS = ("state", "stage", "mode", "RTSP.state")


class AppShapeTests(unittest.TestCase):
    def test_absent_views(self) -> None:
        shape = RV.app_state_shape({"selected_cam": "View"})
        self.assertEqual(shape["top_level_keys"], ["selected_cam"])
        self.assertEqual(shape["views"], {"View": {"present": False}, "SecondView": {"present": False}})

    def test_null_is_not_absent(self) -> None:
        shape = RV.app_state_shape({"View": None, "SecondView": {"state": None, "RTSP": {}, "mode": "none"}})
        self.assertEqual(shape["views"]["View"], {"present": True, "null": True, "value_type": "NoneType"})
        second = shape["views"]["SecondView"]
        self.assertEqual(second["keys"], ["RTSP", "mode", "state"])
        self.assertEqual(second["fields"]["state"], {"present": True, "null": True})  # key present, value null
        self.assertEqual(second["fields"]["stage"], {"present": False})  # key not in the reply
        self.assertEqual(second["fields"]["RTSP.state"], {"present": False})  # RTSP present but without a state
        self.assertEqual(second["fields"]["mode"], {"present": True, "null": False, "value": "none"})

    def test_present_views_report_words_only(self) -> None:
        reply = {"selected_cam": "View", "secret": 1,
                 "View": {"state": "cancel", "stage": "Sleep", "mode": "none", "RTSP": {"state": "idle", "url": "rtsp://192.0.2.1/x"}, "pixels": [1, 2]},
                 "SecondView": {"state": "working", "stage": "RTSP", "mode": "scenery", "RTSP": {"state": "working", "port": 4555}}}
        shape = RV.app_state_shape(reply)
        main = shape["views"]["View"]
        self.assertEqual({k: v.get("value") for k, v in main["fields"].items()},
                         {"state": "cancel", "stage": "Sleep", "mode": "none", "RTSP.state": "idle"})
        self.assertEqual(shape["top_level_keys"], ["SecondView", "View", "secret", "selected_cam"])
        text = json.dumps(shape)
        for leak in ("192.0.2.1", "rtsp://", "4555"):
            self.assertNotIn(leak, text)

    def test_odd_values_are_typed_not_copied(self) -> None:
        shape = RV.app_state_shape({"View": {"state": "x" * 200, "stage": {"a": 1}, "mode": 7}})
        fields = shape["views"]["View"]["fields"]
        self.assertEqual(fields["state"]["value_type"], "str")
        self.assertEqual(fields["stage"]["value_type"], "dict")
        self.assertNotIn("x" * 50, json.dumps(shape))

    def test_a_non_mapping_result(self) -> None:
        self.assertEqual(RV.app_state_shape(None), {"result_type": "NoneType"})

    def test_only_the_read_only_transport_call_was_added(self) -> None:
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("transport.read_app_state(host)", source)
        self.assertNotIn('"app-state', source)  # the audited report's step list is unchanged
        for forbidden in ("seestar_control", "send_command", "iscope_start_view", "scope_park", "scope_move_to_horizon"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
