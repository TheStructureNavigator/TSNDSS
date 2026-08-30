from __future__ import annotations

import json
import tempfile
import threading
import unittest
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from urllib.request import Request, urlopen

from tsn_dss.domain.models import ImagingProfile, Site, TelescopeState
from tsn_dss.engine.telescope import (
    SeestarAdapter,
    TelescopeStateService,
    build_default_telescope_adapter_registry,
)
from tsn_dss.gui.http_api import create_http_server


@dataclass
class FakeHardwareAdapter:
    connected: bool = True

    def __post_init__(self) -> None:
        self.last_slew: tuple[float, float, str | None] | None = None

    def get_state(self) -> TelescopeState:
        return TelescopeState(
            adapter_id="fake-hardware",
            source_kind="fake_hardware",
            timestamp_utc="2026-08-30T00:00:00Z",
            connected=self.connected,
            status="tracking",
            is_simulated=False,
            ra_hours=12.5,
            dec_deg=22.75,
            target_name="Deneb",
        )

    def get_imaging_profile(self) -> ImagingProfile:
        return ImagingProfile(
            profile_id="fake_profile",
            label="Fake hardware profile",
            focal_length_mm=400.0,
            sensor_width_mm=23.5,
            sensor_height_mm=15.6,
            fov_width_deg=3.365,
            fov_height_deg=2.234,
        )

    def get_capabilities(self):
        from tsn_dss.engine.telescope import TelescopeAdapterCapabilities

        return TelescopeAdapterCapabilities(
            can_manual_pointing=False,
            can_slew_to_coordinates=True,
            can_stream_preview=True,
        )

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def slew_to_coordinates(
        self,
        *,
        ra_hours: float,
        dec_deg: float,
        target_name: str | None = None,
    ) -> None:
        self.last_slew = (ra_hours, dec_deg, target_name)


class TelescopeStateServiceTests(unittest.TestCase):
    def test_default_snapshot_uses_simulator_and_profile(self) -> None:
        service = TelescopeStateService()

        snapshot = service.get_snapshot()

        self.assertEqual(snapshot.telescope_state.adapter_id, "simulator")
        self.assertEqual(snapshot.telescope_state.source_kind, "simulator")
        self.assertTrue(snapshot.telescope_state.connected)
        self.assertTrue(snapshot.telescope_state.is_simulated)
        self.assertEqual(snapshot.telescope_state.status, "tracking")
        self.assertEqual(snapshot.telescope_state.target_name, "M42")
        self.assertEqual(snapshot.imaging_profile.profile_id, "simulator-default")
        self.assertEqual(snapshot.imaging_profile.label, "Seestar S30 Pro tele profile")
        self.assertAlmostEqual(snapshot.imaging_profile.focal_length_mm, 160.0)
        self.assertAlmostEqual(snapshot.imaging_profile.sensor_width_mm, 11.2)
        self.assertAlmostEqual(snapshot.imaging_profile.sensor_height_mm, 6.3)
        self.assertAlmostEqual(snapshot.imaging_profile.fov_width_deg or 0.0, 4.0107, places=3)
        self.assertAlmostEqual(snapshot.imaging_profile.fov_height_deg or 0.0, 2.2558, places=3)

    def test_service_accepts_hardware_agnostic_adapter_contract(self) -> None:
        adapter = FakeHardwareAdapter()
        service = TelescopeStateService(adapter=adapter)

        snapshot = service.get_snapshot()

        self.assertEqual(snapshot.telescope_state.adapter_id, "fake-hardware")
        self.assertEqual(snapshot.telescope_state.source_kind, "fake_hardware")
        self.assertFalse(snapshot.telescope_state.is_simulated)
        self.assertEqual(snapshot.imaging_profile.profile_id, "fake_profile")
        self.assertEqual(snapshot.imaging_profile.label, "Fake hardware profile")

        capabilities = service.get_adapter_capabilities()
        self.assertFalse(capabilities.can_manual_pointing)
        self.assertTrue(capabilities.can_slew_to_coordinates)
        self.assertTrue(capabilities.can_stream_preview)

    def test_update_simulator_pointing_changes_snapshot(self) -> None:
        service = TelescopeStateService()

        snapshot = service.update_simulator_pointing(
            ra_hours=0.712,
            dec_deg=41.269,
            target_name="M31",
            status="slewing",
        )

        self.assertEqual(snapshot.telescope_state.target_name, "M31")
        self.assertEqual(snapshot.telescope_state.status, "slewing")

        adapter = service._adapter
        adapter._slew_started_at = adapter._slew_started_at - timedelta(seconds=30)
        adapter._slew_finish_at = adapter._slew_finish_at - timedelta(seconds=30)
        completed = service.get_snapshot()
        self.assertEqual(completed.telescope_state.ra_hours, 0.712)
        self.assertEqual(completed.telescope_state.dec_deg, 41.269)
        self.assertEqual(completed.telescope_state.status, "slewing")

    def test_planned_pointing_can_be_set_and_cleared_independently(self) -> None:
        service = TelescopeStateService()

        planned = service.set_planned_pointing(
            ra_hours=20.75,
            dec_deg=30.5,
            target_name="Cygnus Loop Panel 01",
            source_kind="mosaic_panel",
            source_id="panel:01",
        )

        self.assertIsNotNone(planned.planned_pointing)
        self.assertEqual(planned.planned_pointing.target_name, "Cygnus Loop Panel 01")
        self.assertEqual(planned.planned_pointing.source_kind, "mosaic_panel")
        self.assertEqual(planned.planned_pointing.source_id, "panel:01")
        self.assertEqual(planned.planned_pointing.ra_hours, 20.75)
        self.assertEqual(planned.planned_pointing.dec_deg, 30.5)
        self.assertEqual(planned.telescope_state.target_name, "M42")

        cleared = service.clear_planned_pointing()
        self.assertIsNone(cleared.planned_pointing)

    def test_slew_to_planned_pointing_uses_planned_coordinates(self) -> None:
        service = TelescopeStateService()
        service.set_planned_pointing(
            ra_hours=20.75,
            dec_deg=30.5,
            target_name="Cygnus Loop Panel 01",
            source_kind="mosaic_panel",
            source_id="panel:01",
        )

        slewing = service.slew_to_planned_pointing()
        self.assertEqual(slewing.telescope_state.status, "slewing")
        self.assertEqual(slewing.telescope_state.target_name, "Cygnus Loop Panel 01")

        adapter = service._adapter
        adapter._slew_started_at = adapter._slew_started_at - timedelta(seconds=30)
        adapter._slew_finish_at = adapter._slew_finish_at - timedelta(seconds=30)

        completed = service.get_snapshot()
        self.assertEqual(completed.telescope_state.ra_hours, 20.75)
        self.assertEqual(completed.telescope_state.dec_deg, 30.5)
        self.assertEqual(completed.telescope_state.status, "tracking")

    def test_manual_pointing_update_is_rejected_for_non_manual_adapter(self) -> None:
        service = TelescopeStateService(adapter=FakeHardwareAdapter())

        with self.assertRaisesRegex(ValueError, "does not support manual pointing"):
            service.update_simulator_pointing(ra_hours=1.5, dec_deg=2.5)

    def test_default_registry_lists_simulator_and_seestar(self) -> None:
        registry = build_default_telescope_adapter_registry()

        descriptors = registry.list_descriptors()
        descriptor_ids = [descriptor.adapter_id for descriptor in descriptors]

        self.assertEqual(descriptor_ids, ["seestar", "simulator"])
        self.assertEqual(registry.get_descriptor("seestar").source_kind, "seestar")
        self.assertTrue(registry.get_descriptor("simulator").is_simulated)

    def test_service_can_switch_to_registered_seestar_adapter(self) -> None:
        service = TelescopeStateService()

        snapshot = service.set_active_adapter("seestar")

        self.assertEqual(service.get_active_adapter_id(), "seestar")
        self.assertEqual(snapshot.telescope_state.adapter_id, "seestar")
        self.assertEqual(snapshot.telescope_state.source_kind, "seestar")
        self.assertFalse(snapshot.telescope_state.is_simulated)
        self.assertEqual(snapshot.telescope_state.status, "disconnected")
        self.assertEqual(snapshot.imaging_profile.profile_id, "seestar_s30_pro_tele")

    def test_active_site_overrides_snapshot_location(self) -> None:
        service = TelescopeStateService()

        snapshot = service.set_active_site(
            Site(
                id="site:bieszczady",
                name="Bieszczady",
                latitude_deg=49.2,
                longitude_deg=22.5,
                elevation_m=640.0,
            )
        )

        self.assertIsNotNone(snapshot.active_site)
        self.assertEqual(snapshot.active_site.name, "Bieszczady")
        self.assertEqual(snapshot.telescope_state.site_lat_deg, 49.2)
        self.assertEqual(snapshot.telescope_state.site_lon_deg, 22.5)
        self.assertEqual(snapshot.telescope_state.site_elevation_m, 640.0)

    def test_seestar_adapter_skeleton_exposes_future_capabilities(self) -> None:
        adapter = SeestarAdapter()

        capabilities = adapter.get_capabilities()

        self.assertTrue(capabilities.can_stream_preview)
        self.assertTrue(capabilities.can_start_stack)
        self.assertTrue(capabilities.can_run_observation_plans)
        with self.assertRaisesRegex(NotImplementedError, "not implemented yet"):
            adapter.slew_to_coordinates(ra_hours=5.5, dec_deg=-5.3, target_name="M42")


class TelescopeStateApiTests(unittest.TestCase):
    def test_get_telescope_state_returns_snapshot_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            projects_root = Path(temp_dir) / "projects"
            projects_root.mkdir(parents=True, exist_ok=True)

            server = create_http_server(host="127.0.0.1", port=0, projects_root=projects_root)
            host, port = server.server_address
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            try:
                with urlopen(f"http://{host}:{port}/api/telescope/state") as response:
                    payload = json.loads(response.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=2)

        self.assertIn("telescope_state", payload)
        self.assertIn("imaging_profile", payload)
        self.assertEqual(payload["telescope_state"]["adapter_id"], "simulator")
        self.assertEqual(payload["telescope_state"]["source_kind"], "simulator")
        self.assertTrue(payload["telescope_state"]["is_simulated"])
        self.assertEqual(payload["imaging_profile"]["profile_id"], "simulator-default")
        self.assertIn("planned_pointing", payload)
        self.assertIn("active_site", payload)

    def test_planned_pointing_api_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            projects_root = Path(temp_dir) / "projects"
            projects_root.mkdir(parents=True, exist_ok=True)

            server = create_http_server(host="127.0.0.1", port=0, projects_root=projects_root)
            host, port = server.server_address
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            try:
                request_payload = json.dumps(
                    {
                        "target_name": "Cygnus Loop Panel 01",
                        "ra_hours": 20.75,
                        "dec_deg": 30.5,
                        "source_kind": "mosaic_panel",
                        "source_id": "panel:01",
                    }
                ).encode("utf-8")
                request = Request(
                    f"http://{host}:{port}/api/telescope/planned-pointing",
                    data=request_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request) as response:
                    create_payload = json.loads(response.read().decode("utf-8"))

                clear_request = Request(
                    f"http://{host}:{port}/api/telescope/planned-pointing",
                    method="DELETE",
                )
                with urlopen(clear_request) as response:
                    delete_payload = json.loads(response.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=2)

        self.assertEqual(create_payload["planned_pointing"]["target_name"], "Cygnus Loop Panel 01")
        self.assertEqual(create_payload["planned_pointing"]["source_kind"], "mosaic_panel")
        self.assertTrue(delete_payload["deleted"])
        self.assertIsNone(delete_payload["planned_pointing"])

    def test_slew_to_planned_pointing_api_starts_slew(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            projects_root = Path(temp_dir) / "projects"
            projects_root.mkdir(parents=True, exist_ok=True)

            server = create_http_server(host="127.0.0.1", port=0, projects_root=projects_root)
            host, port = server.server_address
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            try:
                request_payload = json.dumps(
                    {
                        "target_name": "Cygnus Loop Panel 01",
                        "ra_hours": 20.75,
                        "dec_deg": 30.5,
                        "source_kind": "mosaic_panel",
                        "source_id": "panel:01",
                    }
                ).encode("utf-8")
                request = Request(
                    f"http://{host}:{port}/api/telescope/planned-pointing",
                    data=request_payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request):
                    pass

                slew_request = Request(
                    f"http://{host}:{port}/api/telescope/slew-to-planned",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(slew_request) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            finally:
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=2)

        self.assertEqual(payload["telescope_state"]["status"], "slewing")
        self.assertEqual(payload["telescope_state"]["target_name"], "Cygnus Loop Panel 01")
        self.assertEqual(payload["planned_pointing"]["source_kind"], "mosaic_panel")


if __name__ == "__main__":
    unittest.main()
