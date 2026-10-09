"""DB-03 Wave 3: DB-02 runtime telemetry -> per-camera readiness evidence."""

from __future__ import annotations

import copy
import unittest
from dataclasses import replace

from tsn_dss.engine.device_runtime import PreviewAvailability as A
from tsn_dss.engine.device_runtime.preview_readiness import ReadinessEvidence, ReadinessEvidenceProvider
from tsn_dss.engine.seestar_preview import SeestarReadinessEvidenceProvider, camera_availability
from tsn_dss.engine.seestar_provider import SeestarUnreachable
from tsn_dss.engine.seestar_provider.normalize import preview_availability

try:
    from seestar_preview_support import Flow, VIEW_KEY, PORT
except ModuleNotFoundError:  # pragma: no cover
    from tests.seestar_preview_support import Flow, VIEW_KEY, PORT


def evidence_provider(flow: Flow) -> SeestarReadinessEvidenceProvider:
    return SeestarReadinessEvidenceProvider(flow.runtime, flow.connection)


def sample_availability(flow: Flow, camera: str) -> A:
    return camera_availability(flow.runtime.read_telemetry(flow.connection), camera)


class MappingTests(unittest.TestCase):
    def test_ready_fixture_is_available_for_both_cameras(self) -> None:
        flow = Flow()
        provider = evidence_provider(flow)
        self.assertIsInstance(provider, ReadinessEvidenceProvider)
        for camera in ("main", "wide"):
            self.assertIs(provider.read_evidence(camera).availability, A.AVAILABLE)

    def test_cameras_are_mapped_independently(self) -> None:
        for ready, idle in (("main", "wide"), ("wide", "main")):
            flow = Flow()
            flow.set_camera_ready(idle, False)
            provider = evidence_provider(flow)
            self.assertIs(provider.read_evidence(ready).availability, A.AVAILABLE, ready)
            self.assertIs(provider.read_evidence(idle).availability, A.UNAVAILABLE, idle)

    def test_missing_block_for_one_camera_is_unknown_for_that_camera_only(self) -> None:
        flow = Flow()
        flow.drop_camera_block("wide")
        provider = evidence_provider(flow)
        self.assertIs(provider.read_evidence("wide").availability, A.UNKNOWN)
        self.assertIs(provider.read_evidence("main").availability, A.AVAILABLE)

    def test_equivalence_with_db02_availability_rule(self) -> None:
        """AVAILABLE here iff AVAILABLE in DB-02; partial replies may only be stricter."""
        variants = {
            "ready": lambda r: None,
            "idle": lambda r: [r[k].update(state="idle", mode="none", stage="Idle") or r[k].__setitem__("RTSP", {"state": "stopped", "port": 0}) for k in ("View", "SecondView")],
            "rtsp_not_working": lambda r: [r[k]["RTSP"].update(state="starting") for k in ("View", "SecondView")],
            "state_not_working": lambda r: [r[k].update(state="idle") for k in ("View", "SecondView")],
            "mode_other": lambda r: [r[k].update(mode="star") for k in ("View", "SecondView")],
            "stage_other": lambda r: [r[k].update(stage="Stack") for k in ("View", "SecondView")],
            "swapped_ports": lambda r: [r["View"]["RTSP"].update(port=4555), r["SecondView"]["RTSP"].update(port=4554)],
            "view_missing": lambda r: [r.pop("View"), r.pop("SecondView")],
            "rtsp_block_missing": lambda r: [r[k].pop("RTSP") for k in ("View", "SecondView")],
            "mode_missing": lambda r: [r[k].pop("mode") for k in ("View", "SecondView")],
            "state_null": lambda r: [r[k].update(state=None) for k in ("View", "SecondView")],
            "port_text": lambda r: [r[k]["RTSP"].update(port="4554") for k in ("View", "SecondView")],
        }
        for name, change in variants.items():
            flow = Flow()
            change(flow.transport.app_reply["result"])
            app_result = copy.deepcopy(flow.transport.app_reply["result"])
            for camera in ("main", "wide"):
                mine = sample_availability(flow, camera)
                theirs = preview_availability(app_result, camera)
                with self.subTest(variant=name, camera=camera):
                    self.assertEqual(mine is A.AVAILABLE, theirs is A.AVAILABLE)
                    if theirs is not A.AVAILABLE:
                        self.assertIsNot(mine, A.AVAILABLE)
                    if name in {"ready", "idle", "rtsp_not_working", "state_not_working", "mode_other", "stage_other", "swapped_ports"}:
                        self.assertIs(mine, theirs)

    def test_stale_items_never_count_as_available(self) -> None:
        flow = Flow()
        provider = evidence_provider(flow)
        self.assertIs(provider.read_evidence("main").availability, A.AVAILABLE)
        flow.transport.fail_next("app_state", SeestarUnreachable("unreachable"))
        sample = flow.runtime.read_telemetry(flow.connection)
        self.assertEqual(sample.get("app.main.state").state.value, "stale")  # retained, not fresh
        flow.transport.fail_next("app_state", SeestarUnreachable("unreachable"))
        self.assertIs(provider.read_evidence("main").availability, A.UNKNOWN)

    def test_unknown_camera_name_gives_no_evidence(self) -> None:
        provider = evidence_provider(Flow())
        for label in ("tele", "MAIN", "", "cam_a"):
            self.assertIsNone(provider.read_evidence(label))
            self.assertEqual(provider.last_error_category, "camera_not_configured")

    def test_unconfigured_camera_gives_no_evidence(self) -> None:
        flow = Flow()
        provider = SeestarReadinessEvidenceProvider(flow.runtime, flow.connection, cameras=("wide",))
        self.assertIsNone(provider.read_evidence("main"))
        self.assertIsNotNone(provider.read_evidence("wide"))


class BindingTests(unittest.TestCase):
    def test_evidence_is_bound_to_connection_device_and_provider(self) -> None:
        flow = Flow()
        ev = evidence_provider(flow).read_evidence("main")
        self.assertIsInstance(ev, ReadinessEvidence)
        self.assertEqual(ev.connection_id, flow.connection.connection_id)
        self.assertEqual(ev.device_ref, flow.connection.device.device_ref)
        self.assertEqual(ev.provider_id, flow.connection.provider_id)
        self.assertEqual(ev.source_label, "main")

    def test_observation_time_is_host_time_never_the_device_timestamp(self) -> None:
        flow = Flow()
        ev = evidence_provider(flow).read_evidence("wide")
        self.assertEqual(ev.host_observed_at, flow.clock())  # frozen clock: the runtime's stamp
        self.assertEqual(ev.host_observed_at.year, 2026)
        self.assertFalse(hasattr(ev, "provider_reported_at"))
        self.assertIn("Timestamp", flow.transport.app_reply)  # the device did send one; it is not used

    def test_evidence_is_marked_with_the_samples_simulated_flag(self) -> None:
        flow = Flow()
        self.assertFalse(evidence_provider(flow).read_evidence("main").simulated)

    def test_every_call_reads_the_device_anew(self) -> None:
        flow = Flow()
        provider = evidence_provider(flow)
        before = len([c for c in flow.transport.calls if c[0] == "read_app_state"])
        for _ in range(3):
            provider.read_evidence("main")
        after = len([c for c in flow.transport.calls if c[0] == "read_app_state"])
        self.assertEqual(after - before, 3)

    def test_sample_for_another_connection_is_rejected(self) -> None:
        flow = Flow()

        class Foreign:
            def read_telemetry(self, connection):
                return replace(flow.runtime.read_telemetry(connection), connection_id="another-connection")

        provider = SeestarReadinessEvidenceProvider(Foreign(), flow.connection)
        self.assertIsNone(provider.read_evidence("main"))
        self.assertEqual(provider.last_error_category, "identity_mismatch")

    def test_sample_for_another_provider_is_rejected(self) -> None:
        flow = Flow()

        class Foreign:
            def read_telemetry(self, connection):
                return replace(flow.runtime.read_telemetry(connection), provider_id="another-provider")

        self.assertIsNone(SeestarReadinessEvidenceProvider(Foreign(), flow.connection).read_evidence("main"))

    def test_a_different_device_at_the_endpoint_gives_no_evidence(self) -> None:
        flow = Flow()
        swapped = copy.deepcopy(flow.transport.state_reply)
        swapped["result"]["device"]["sn"] = "another-serial-number"
        flow.transport.state_reply = swapped
        provider = evidence_provider(flow)
        self.assertIsNone(provider.read_evidence("main"))
        self.assertEqual(provider.last_error_category, "telemetry_unavailable")


class FailureTests(unittest.TestCase):
    def test_total_read_failure_gives_no_evidence(self) -> None:
        flow = Flow()
        flow.transport.fail_next("device_state", SeestarUnreachable("unreachable"))
        flow.transport.fail_next("app_state", SeestarUnreachable("unreachable"))
        provider = evidence_provider(flow)
        self.assertIsNone(provider.read_evidence("main"))
        self.assertEqual(provider.last_error_category, "telemetry_unavailable")
        self.assertIsNotNone(provider.read_evidence("main"))  # next call reads anew and succeeds
        self.assertIsNone(provider.last_error_category)

    def test_disconnected_connection_gives_no_evidence(self) -> None:
        flow = Flow()
        provider = evidence_provider(flow)
        flow.runtime.disconnect(flow.connection)
        self.assertIsNone(provider.read_evidence("main"))
        self.assertEqual(provider.last_error_category, "telemetry_unavailable")

    def test_reads_are_allow_listed_read_requests_only(self) -> None:
        flow = Flow()
        provider = evidence_provider(flow)
        for camera in ("main", "wide"):
            provider.read_evidence(camera)
        self.assertTrue({c[0] for c in flow.transport.calls} <= {"read_device_state", "read_app_state", "test_connection"})

    def test_connection_ready_state_is_not_an_input(self) -> None:
        flow = Flow()
        self.assertEqual(flow.connection.state.value, "ready")
        flow.drop_camera_block("main")
        flow.drop_camera_block("wide")
        provider = evidence_provider(flow)
        for camera in ("main", "wide"):
            self.assertIs(provider.read_evidence(camera).availability, A.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
