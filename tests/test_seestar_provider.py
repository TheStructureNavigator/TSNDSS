"""DB-02 Seestar Provider through the vendor-neutral DB-01 ProviderRuntime, on fakes only."""

from __future__ import annotations

import copy
import unittest
from datetime import timedelta

from tsn_dss.engine.device_runtime import (
    CapabilityConfirmation,
    ConnectionState,
    DeviceProvider,
    DiscoveryOutcome,
    ManualClock,
    PreviewAvailability,
    ProviderLifecycleState,
    ProviderRegistry,
    SequentialIdGenerator,
    TelemetrySource,
    ValueState,
)
from tsn_dss.engine.seestar_provider import (
    SeestarAnnouncement,
    SeestarAuthError,
    SeestarProvider,
    SeestarTimeout,
    SeestarUnreachable,
)

try:
    from seestar_support import (
        HOST,
        OTHER_HOST,
        FakeSeestarTransport,
        connected,
        load_fixture,
        make_config,
        make_runtime,
    )
except ModuleNotFoundError:  # pragma: no cover
    from tests.seestar_support import (
        HOST,
        OTHER_HOST,
        FakeSeestarTransport,
        connected,
        load_fixture,
        make_config,
        make_runtime,
    )

S = ProviderLifecycleState
C = ConnectionState
SECRETS = ("SYNTHETIC-NOT-A-SECRET", "SYNTHETIC-AP", "SYNTHETIC-NET", "SYNTHETIC-CPU-ID", "192.0.2.1", "255.255.255.0", "WPA2-PSK")
ALLOWED_CALLS = {"read_device_state", "read_app_state", "test_connection", "discover_via_udp"}


def item(sample, name):
    found = sample.get(name)
    assert found is not None, name
    return found


class DescriptorAndRegistrationTests(unittest.TestCase):
    def test_descriptor_declares_identity_kind_label_and_real_provider(self) -> None:
        """REQ-001, REQ-002, REQ-035: stable id independent of host or device."""
        descriptor = make_runtime()[1].descriptor
        self.assertEqual(descriptor.provider_id, "seestar")
        self.assertEqual(descriptor.provider_kind, "seestar")
        self.assertFalse(descriptor.simulated)
        self.assertTrue(descriptor.implementation_label)
        self.assertTrue(descriptor.device_required)

    def test_configuration_status_reflects_operator_input(self) -> None:
        """REQ-002, REQ-004: incomplete configuration is `unconfigured`, never silently defaulted."""
        for overrides, expected in (({}, S.CONFIGURED), ({"key_path": None}, S.UNCONFIGURED), ({"host": None}, S.UNCONFIGURED)):
            runtime, _, _ = make_runtime(make_config(**overrides))
            self.assertIs(runtime.state, expected, overrides)
        runtime, _, _ = make_runtime(make_config(host=None, allow_udp_discovery=True))
        self.assertIs(runtime.state, S.CONFIGURED)
        self.assertFalse(runtime.descriptor.device_required)

    def test_satisfies_the_neutral_provider_protocol_and_registers_without_side_effects(self) -> None:
        """REQ-001, REQ-003."""
        provider = SeestarProvider(make_config(), FakeSeestarTransport())
        self.assertIsInstance(provider, DeviceProvider)
        registry = ProviderRegistry(clock=ManualClock(), id_generator=SequentialIdGenerator())
        registry.register(provider)
        self.assertEqual([d.provider_id for d in registry.list_descriptors()], ["seestar"])
        self.assertEqual(provider._transport.calls, [])  # registration touches no device


class DiscoveryTests(unittest.TestCase):
    def test_configured_host_discovery_returns_a_runtime_device_reference(self) -> None:
        """REQ-006, REQ-008, REQ-036: acceptance 'read-only discovery returns a runtime Device Reference'."""
        runtime, _, transport = make_runtime()
        result = runtime.discover()
        self.assertIs(result.outcome, DiscoveryOutcome.DEVICES_FOUND)
        self.assertFalse(result.simulated)
        (device,) = result.devices
        self.assertEqual(device.device_ref, "s30pro_0badc0de")
        self.assertEqual(device.provider_id, "seestar")
        self.assertEqual((device.model, device.firmware_version), ("Seestar S30 Pro", "9.31"))
        self.assertIsNone(device.endpoint_hint)
        self.assertFalse(device.simulated)
        self.assertEqual(transport.calls, [("read_device_state", HOST, "device")])

    def test_device_reference_carries_no_address_or_secret(self) -> None:
        """REQ-008, REQ-032: identity is the serial-derived ref, never an address."""
        device = make_runtime()[0].discover().devices[0]
        text = repr(device)
        for secret in (HOST, *SECRETS):
            self.assertNotIn(secret, text)

    def test_unreachable_host_is_a_nonfatal_failure_and_recovers(self) -> None:
        """REQ-036: nonfatal failure then recovery refresh."""
        runtime, _, transport = make_runtime()
        transport.fail_next("device_state", SeestarUnreachable("connect_failed"))
        failed = runtime.discover()
        self.assertIs(failed.outcome, DiscoveryOutcome.NONFATAL_FAILURE)
        self.assertEqual(failed.error_category, "connect_failed")
        self.assertIs(runtime.state, S.DEGRADED)
        recovered = runtime.discover()
        self.assertIs(recovered.outcome, DiscoveryOutcome.DEVICES_FOUND)
        self.assertIs(runtime.state, S.AVAILABLE)

    def test_timeout_is_nonfatal(self) -> None:
        runtime, _, transport = make_runtime()
        transport.fail_next("device_state", SeestarTimeout("read_timeout"))
        self.assertIs(runtime.discover().outcome, DiscoveryOutcome.NONFATAL_FAILURE)

    def test_rejected_authentication_is_fatal_until_reset(self) -> None:
        """REQ-036: fatal failure; failed is terminal until reset (section 4)."""
        runtime, _, transport = make_runtime()
        transport.fail_next("device_state", SeestarAuthError("auth_rejected"))
        self.assertIs(runtime.discover().outcome, DiscoveryOutcome.FATAL_FAILURE)
        self.assertIs(runtime.state, S.FAILED)
        runtime.reset(configured=True)
        self.assertIs(runtime.discover().outcome, DiscoveryOutcome.DEVICES_FOUND)

    def test_missing_serial_means_no_identity_and_is_not_invented(self) -> None:
        """REQ-008: no stable identity, no device reference."""
        runtime, _, transport = make_runtime()
        del transport.state_reply["result"]["device"]["sn"]
        result = runtime.discover()
        self.assertIs(result.outcome, DiscoveryOutcome.NONFATAL_FAILURE)
        self.assertEqual(result.error_category, "identity_unavailable")
        self.assertEqual(result.devices, ())

    def test_non_zero_rpc_code_is_a_nonfatal_failure(self) -> None:
        runtime, _, transport = make_runtime()
        transport.state_reply["code"] = 5
        result = runtime.discover()
        self.assertEqual((result.outcome, result.error_category), (DiscoveryOutcome.NONFATAL_FAILURE, "rpc_error"))

    def test_udp_discovery_is_opt_in_and_never_used_when_a_host_is_configured(self) -> None:
        """Approved design: prefer explicit hosts; no silent fallback to broadcast."""
        runtime, _, transport = make_runtime(make_config(allow_udp_discovery=True))
        transport.fail_next("device_state", SeestarUnreachable("connect_failed"))
        runtime.discover()
        self.assertNotIn(("discover_via_udp",), transport.calls)
        runtime2, _, transport2 = make_runtime(make_config(host=None, allow_udp_discovery=True))
        transport2.announcements = [SeestarAnnouncement("0badc0de", "Seestar S30 Pro", HOST)]
        result = runtime2.discover()
        self.assertEqual(result.devices[0].device_ref, "s30pro_0badc0de")
        self.assertEqual(transport2.calls, [("discover_via_udp",)])

    def test_udp_silence_is_valid_empty_discovery(self) -> None:
        """REQ-037: zero devices is not by itself a failure."""
        runtime, _, _ = make_runtime(make_config(host=None, allow_udp_discovery=True))
        result = runtime.discover()
        self.assertIs(result.outcome, DiscoveryOutcome.EMPTY_VALID)
        self.assertIs(runtime.state, S.AVAILABLE)

    def test_configured_host_that_does_not_answer_degrades_because_a_device_is_required(self) -> None:
        """REQ-037."""
        runtime, _, transport = make_runtime()
        transport.fail_next("device_state", SeestarUnreachable("connect_failed"))
        runtime.discover()
        self.assertIs(runtime.state, S.DEGRADED)

    def test_discovery_creates_no_persistent_identity_and_no_commands(self) -> None:
        """REQ-007, REQ-055: acceptance 'discovery submits no Commands'."""
        runtime, provider, transport = make_runtime()
        runtime.discover()
        self.assertTrue({c[0] for c in transport.calls} <= ALLOWED_CALLS)
        # Only in-memory runtime state exists: no store, database or registry attribute.
        self.assertEqual(
            set(vars(provider)),
            {"_config", "_transport", "_clock", "_lock", "_endpoints", "_sessions", "_last_good", "_descriptor"},
        )
        self.assertEqual(provider._endpoints, {"s30pro_0badc0de": HOST})


class ConnectionLifecycleTests(unittest.TestCase):
    def test_connect_verifies_identity_and_reports_provider_evidence_only(self) -> None:
        """REQ-009..012: acceptance 'connection state reports provider evidence'."""
        runtime, _, transport, connection = connected()
        self.assertIs(connection.state, C.CONNECTED)
        self.assertEqual(connection.device.device_ref, "s30pro_0badc0de")
        self.assertEqual([c[0] for c in transport.calls], ["read_device_state"] * 2)

    def test_reconnect_creates_a_new_connection_id_with_the_same_device_ref(self) -> None:
        """REQ-041, REQ-042: identity changes per Connection, not per device."""
        runtime, _, transport, first = connected()
        runtime.disconnect(first)
        second = runtime.connect(runtime.open_connection(first.device))
        self.assertNotEqual(first.connection_id, second.connection_id)
        self.assertEqual(first.device.device_ref, second.device.device_ref)
        self.assertIs(first.state, C.DISCONNECTED)

    def test_disconnect_makes_no_device_call(self) -> None:
        runtime, _, transport, connection = connected()
        before = list(transport.calls)
        runtime.disconnect(connection)
        runtime.disconnect(connection)
        self.assertEqual(transport.calls, before)

    def test_a_different_device_at_the_same_address_fails_the_connection_and_it_is_replaced(self) -> None:
        """REQ-043: identity mismatch (for example after an address change) fails, then is replaced."""
        runtime, _, transport = make_runtime()
        device = runtime.discover().devices[0]
        transport.state_reply["result"]["device"]["sn"] = "feedface"
        failed = runtime.connect(runtime.open_connection(device))
        self.assertIs(failed.state, C.FAILED)
        self.assertEqual(failed.history[-1].evidence, "identity_mismatch")
        transport.state_reply["result"]["device"]["sn"] = "0badc0de"
        replacement = runtime.connect(runtime.replace_failed_connection(failed))
        self.assertIs(replacement.state, C.CONNECTED)

    def test_connection_to_an_unreachable_device_fails(self) -> None:
        runtime, _, transport = make_runtime()
        device = runtime.discover().devices[0]
        transport.fail_next("device_state", SeestarUnreachable("connect_failed"))
        self.assertIs(runtime.connect(runtime.open_connection(device)).state, C.FAILED)

    def test_address_change_in_udp_mode_keeps_device_ref_and_updates_the_endpoint(self) -> None:
        """Changing IPs: the device_ref is serial-derived; only the endpoint is refreshed."""
        runtime, _, transport = make_runtime(make_config(host=None, allow_udp_discovery=True))
        transport.announcements = [SeestarAnnouncement("0badc0de", "Seestar S30 Pro", HOST)]
        first = runtime.discover().devices[0]
        transport.announcements = [SeestarAnnouncement("0badc0de", "Seestar S30 Pro", OTHER_HOST)]
        second = runtime.discover().devices[0]
        self.assertEqual(first.device_ref, second.device_ref)
        runtime.connect(runtime.open_connection(second))
        self.assertEqual(transport.calls[-1][1], OTHER_HOST)

    def test_operator_reconfigured_host_is_used_after_rediscovery(self) -> None:
        runtime, provider, transport = make_runtime()
        runtime.discover()
        provider.reconfigure_host(OTHER_HOST)
        device = runtime.discover().devices[0]
        runtime.connect(runtime.open_connection(device))
        self.assertTrue(all(c[1] == OTHER_HOST for c in transport.calls[1:]))

    def test_loss_and_recovery_through_refresh(self) -> None:
        """Connection loss and recovery: degraded on read failure, ready again on fresh evidence."""
        runtime, _, transport, connection = connected()
        self.assertIs(runtime.refresh_evidence(connection), C.READY)
        transport.fail_next("device_state", SeestarUnreachable("connection_lost"))
        self.assertIs(runtime.refresh_evidence(connection), C.DEGRADED)
        self.assertIs(runtime.refresh_evidence(connection), C.READY)

    def test_no_reads_on_a_connection_that_never_connected(self) -> None:
        runtime, provider, transport = make_runtime()
        device = runtime.discover().devices[0]
        connection = runtime.open_connection(device)
        before = list(transport.calls)
        with self.assertRaises(Exception):
            runtime.read_telemetry(connection)
        self.assertEqual(transport.calls, before)


class CapabilityTests(unittest.TestCase):
    def test_provider_level_report_has_no_availability_and_makes_no_device_call(self) -> None:
        """REQ-013, REQ-056."""
        runtime, _, transport = make_runtime()
        report = runtime.capability_report()
        self.assertIsNone(report.connection_id)
        self.assertTrue(all(e.available_now is None for e in report.entries))
        self.assertEqual(transport.calls, [])

    def test_connection_report_is_backed_by_a_fresh_read_and_timestamped(self) -> None:
        """REQ-014, REQ-057 (partial): availability is fresh device evidence."""
        runtime, _, transport, connection = connected()
        before = len(transport.calls)
        first = runtime.capability_report(connection)
        second = runtime.capability_report(connection)
        self.assertEqual([c[0] for c in transport.calls[before:]], ["read_device_state", "read_device_state"])
        self.assertTrue(all(c[2] == "device" for c in transport.calls[before:]))
        self.assertGreater(second.observed_at, first.observed_at)
        self.assertEqual(first.provider_id, "seestar")
        self.assertEqual(first.connection_id, connection.connection_id)
        self.assertFalse(first.simulated)

    def test_support_is_separate_from_availability_and_physical_operations_are_unsupported(self) -> None:
        """REQ-013, REQ-015."""
        runtime, _, _, connection = connected()
        report = runtime.capability_report(connection)
        telemetry = report.entry("telemetry.read")
        self.assertEqual((telemetry.supported_by_provider, telemetry.available_now), (True, True))
        self.assertIs(telemetry.confirmation, CapabilityConfirmation.IMPLEMENTED_UNTESTED)
        for name in ("mount.motion", "camera.mode_control", "preview.stream", "acquisition", "heater.control"):
            entry = report.entry(name)
            self.assertEqual((entry.supported_by_provider, entry.available_now), (False, False), name)
            self.assertIs(entry.confirmation, CapabilityConfirmation.UNSUPPORTED)
        self.assertIs(report.proves_command_success, False)

    def test_failed_availability_read_is_connection_evidence_not_a_fabricated_report(self) -> None:
        runtime, _, transport, connection = connected()
        transport.fail_next("device_state", SeestarTimeout("read_timeout"))
        with self.assertRaises(Exception):
            runtime.capability_report(connection)


class TelemetryTests(unittest.TestCase):
    def test_sample_has_host_time_and_provider_and_host_sources(self) -> None:
        """REQ-016 (telemetry), REQ-017, REQ-058."""
        runtime, _, _, connection = connected()
        sample = runtime.read_telemetry(connection)
        self.assertIsNotNone(sample.host_observed_at.tzinfo)
        self.assertIsNone(sample.provider_reported_at)  # device Timestamp is not wall-clock evidence
        self.assertFalse(sample.simulated)
        self.assertIs(item(sample, "mount.move_type").source, TelemetrySource.PROVIDER_REPORTED)
        self.assertIs(item(sample, "connection_state").source, TelemetrySource.HOST_OBSERVED)

    def test_known_values_are_mapped_from_the_documented_fields(self) -> None:
        runtime, _, _, connection = connected()
        sample = runtime.read_telemetry(connection)
        expected = {
            "mount.move_type": "none", "mount.arm_closed": True, "mount.tracking": False,
            "pi.temperature_c": 41.5, "pi.battery_percent": 80, "focuser.step": 1580,
            "wide_focuser.step": 1500, "heater.enabled": False, "device.firmware_version": "9.31",
            "app.wide.rtsp_state": "working", "app.wide.rtsp_port": 4555, "app.selected_camera": "View",
        }
        for name, value in expected.items():
            self.assertEqual((item(sample, name).state, item(sample, name).value), (ValueState.KNOWN, value), name)

    def test_device_defaults_are_unknown_not_measurements(self) -> None:
        """Sentinels -999000.0 and -9990.0 are 'no manual value', never exposure or gain."""
        runtime, _, _, connection = connected()
        sample = runtime.read_telemetry(connection)
        for name in ("setting.exposure_ms", "setting.gain"):
            self.assertEqual((item(sample, name).state, item(sample, name).value), (ValueState.UNKNOWN, None), name)

    def test_absent_fields_are_unavailable_and_null_or_wrong_types_are_unknown(self) -> None:
        runtime, _, transport, connection = connected()
        result = transport.state_reply["result"]
        del result["pi_status"]["temp"]
        result["pi_status"]["battery_capacity"] = None
        result["focuser"]["step"] = "not-a-number"
        result["mount"]["close"] = 1  # int is not the documented bool
        sample = runtime.read_telemetry(connection)
        self.assertEqual(item(sample, "pi.temperature_c").state, ValueState.UNAVAILABLE)
        self.assertEqual(item(sample, "pi.battery_percent").state, ValueState.UNKNOWN)
        self.assertEqual(item(sample, "focuser.step").state, ValueState.UNKNOWN)
        self.assertEqual(item(sample, "mount.arm_closed").state, ValueState.UNKNOWN)
        for name in ("pi.temperature_c", "pi.battery_percent", "focuser.step", "mount.arm_closed"):
            self.assertIsNone(item(sample, name).value)

    def test_wide_cam_setting_is_labelled_a_setting_not_live_state(self) -> None:
        """Research: setting.wide_cam=false while the wide camera was streaming."""
        runtime, _, _, connection = connected()
        sample = runtime.read_telemetry(connection)
        self.assertEqual(item(sample, "setting.wide_cam_flag").value, False)
        self.assertEqual(item(sample, "app.wide.rtsp_state").value, "working")

    def test_secrets_returned_by_a_firmware_ignoring_the_key_filter_are_discarded(self) -> None:
        """Defense in depth: credentials, network and location blocks never reach evidence."""
        runtime, provider, transport, connection = connected()
        transport.honor_key_filter = False  # the fake returns the whole payload, secrets included
        sample = runtime.read_telemetry(connection)
        blob = repr(sample) + repr(provider._last_good) + repr(runtime.history) + repr(connection.history)
        for secret in SECRETS:
            self.assertNotIn(secret, blob)
        self.assertNotIn("location_lon_lat", blob)
        self.assertNotIn("0badc0de", repr(sample))  # the serial is identity only, not telemetry

    def test_provider_native_target_data_is_not_mapped(self) -> None:
        """REQ-065: target names and coordinates never become telemetry."""
        runtime, _, _, connection = connected()
        names = {i.name for i in runtime.read_telemetry(connection).items}
        self.assertFalse(any("target" in n or "ra_dec" in n for n in names))

    def test_requests_use_only_allow_listed_state_keys(self) -> None:
        runtime, _, transport, connection = connected()
        runtime.read_telemetry(connection)
        for call in transport.calls:
            if call[0] == "read_device_state":
                self.assertTrue(set(call[2].split(",")) <= {"device", "mount", "pi_status", "focuser", "second_focuser", "setting"})

    def test_partial_failure_returns_stale_then_unavailable_never_fresh_looking_values(self) -> None:
        """Acceptance: preserve stale values; no fabrication."""
        runtime, provider, transport, connection = connected(make_config(stale_retention_s=30))
        first = runtime.read_telemetry(connection)
        transport.fail_next("app_state", SeestarUnreachable("connection_lost"))
        partial = runtime.read_telemetry(connection)
        stale = item(partial, "app.wide.rtsp_state")
        self.assertEqual((stale.state, stale.value), (ValueState.STALE, "working"))
        self.assertEqual(item(partial, "mount.move_type").state, ValueState.KNOWN)  # the healthy group stays fresh
        self.assertEqual(item(first, "app.wide.rtsp_state").state, ValueState.KNOWN)
        # retention expires
        runtime._clock.advance(timedelta(seconds=120))
        provider._clock = runtime._clock
        transport.fail_next("app_state", SeestarUnreachable("connection_lost"))
        expired = runtime.read_telemetry(connection)
        self.assertEqual(item(expired, "app.wide.rtsp_state").state, ValueState.UNAVAILABLE)
        self.assertIsNone(item(expired, "app.wide.rtsp_state").value)

    def test_nothing_known_before_failure_is_unavailable_not_stale(self) -> None:
        runtime, _, transport, connection = connected()
        transport.fail_next("app_state", SeestarUnreachable("connection_lost"))
        sample = runtime.read_telemetry(connection)
        self.assertEqual(item(sample, "app.wide.rtsp_state").state, ValueState.UNAVAILABLE)

    def test_both_reads_failing_raises_and_degrades_the_connection(self) -> None:
        runtime, _, transport, connection = connected()
        transport.fail_next("device_state", SeestarUnreachable("connection_lost"))
        transport.fail_next("app_state", SeestarUnreachable("connection_lost"))
        with self.assertRaises(Exception) as ctx:
            runtime.read_telemetry(connection)
        self.assertEqual(ctx.exception.category, "connection_lost")

    def test_disconnect_forgets_cached_values(self) -> None:
        runtime, provider, transport, connection = connected()
        runtime.read_telemetry(connection)
        runtime.disconnect(connection)
        self.assertEqual(provider._last_good, {})

    def test_telemetry_reads_submit_no_commands_and_change_no_state(self) -> None:
        """REQ-059."""
        runtime, _, transport, connection = connected()
        state = connection.state
        runtime.read_telemetry(connection)
        self.assertIs(connection.state, state)
        self.assertTrue({c[0] for c in transport.calls} <= ALLOWED_CALLS)


class PreviewEvidenceTests(unittest.TestCase):
    def test_availability_comes_from_reported_state_and_opens_no_stream(self) -> None:
        """REQ-016 (preview half is DB-03), REQ-018, REQ-033."""
        runtime, _, transport, connection = connected()
        before = len(transport.calls)
        preview = runtime.describe_preview(connection)
        self.assertEqual([c[0] for c in transport.calls[before:]], ["read_device_state", "read_app_state"])
        self.assertIs(preview.availability, PreviewAvailability.AVAILABLE)
        self.assertEqual(preview.source_kind, "rtsp_secondary_camera")
        self.assertFalse(preview.is_canonical_record)
        self.assertTrue(preview.is_runtime_evidence_only)
        self.assertFalse(preview.simulated)

    def test_idle_device_is_unavailable_and_missing_blocks_are_unknown(self) -> None:
        runtime, _, transport, connection = connected()
        transport.app_reply = load_fixture("app_state_idle.json")
        self.assertIs(runtime.describe_preview(connection).availability, PreviewAvailability.UNAVAILABLE)
        transport.app_reply = copy.deepcopy(transport.app_reply)
        del transport.app_reply["result"]["SecondView"]
        self.assertIs(runtime.describe_preview(connection).availability, PreviewAvailability.UNKNOWN)

    def test_wrong_port_or_stage_is_not_available(self) -> None:
        runtime, _, transport, connection = connected()
        transport.app_reply["result"]["SecondView"]["RTSP"]["port"] = 4554
        self.assertIs(runtime.describe_preview(connection).availability, PreviewAvailability.UNAVAILABLE)
        transport.app_reply = load_fixture("app_state_scenery_ready.json")
        transport.app_reply["result"]["SecondView"]["stage"] = "ContinuousExposure"
        self.assertIs(runtime.describe_preview(connection).availability, PreviewAvailability.UNAVAILABLE)

    def test_main_camera_can_be_selected(self) -> None:
        runtime, _, _, connection = connected(make_config(preview_camera="main"))
        preview = runtime.describe_preview(connection)
        self.assertEqual(preview.source_kind, "rtsp_primary_camera")
        self.assertIs(preview.availability, PreviewAvailability.AVAILABLE)


class SafetyAndRedactionTests(unittest.TestCase):
    def test_full_session_uses_only_allow_listed_transport_calls(self) -> None:
        """REQ-007, REQ-022, REQ-059: acceptance 'submit no Commands'."""
        runtime, provider, transport, connection = connected()
        runtime.refresh_evidence(connection)
        runtime.capability_report(connection)
        runtime.read_telemetry(connection)
        runtime.describe_preview(connection)
        runtime.disconnect(connection)
        self.assertTrue({c[0] for c in transport.calls} <= ALLOWED_CALLS)
        banned = ("submit", "execute", "command", "move", "park", "slew", "start", "stop", "send")
        public = [n for n in dir(provider) if not n.startswith("_")]
        self.assertEqual([n for n in public if any(b in n.lower() for b in banned)], [])

    def test_failure_messages_never_contain_hosts_serials_or_payloads(self) -> None:
        runtime, _, transport = make_runtime()
        transport.fail_next("device_state", SeestarUnreachable("connect_failed"))
        result = runtime.discover()
        text = repr(result) + repr(runtime.history)
        self.assertNotIn(HOST, text)
        runtime2, _, transport2 = make_runtime()
        device = runtime2.discover().devices[0]
        transport2.state_reply["result"]["device"]["sn"] = "feedface"
        failed = runtime2.connect(runtime2.open_connection(device))
        for blob in (repr(failed.history), repr(runtime2.history)):
            self.assertNotIn(HOST, blob)
            self.assertNotIn("feedface", blob)

    def test_error_paths_with_secret_bearing_replies_leak_nothing(self) -> None:
        """A non-zero RPC code on a reply full of credentials yields only a fixed category."""
        runtime, provider, transport = make_runtime()
        transport.state_reply["code"] = 7  # reply still carries the synthetic secret blocks
        result = runtime.discover()
        blob = repr(result) + repr(runtime.history) + repr(provider)
        for secret in (*SECRETS, HOST, "0badc0de"):
            self.assertNotIn(secret, blob)
        transport.state_reply["code"] = 0
        device = runtime.discover().devices[0]
        connection = runtime.connect(runtime.open_connection(device))
        transport.state_reply["code"] = 7
        transport.app_reply["code"] = 7
        with self.assertRaises(Exception) as ctx:
            runtime.read_telemetry(connection)
        text = f"{ctx.exception!r} {ctx.exception} {getattr(ctx.exception, 'category', '')}"
        for secret in (*SECRETS, HOST, "0badc0de"):
            self.assertNotIn(secret, text)

    def test_provider_repr_is_redacted(self) -> None:
        runtime, provider, _ = make_runtime()
        self.assertNotIn(HOST, repr(provider))
        self.assertNotIn("operator-supplied", repr(provider))


class RetentionAndIdentityBindingTests(unittest.TestCase):
    """Audit: retained values are bounded in time and bound to the device that produced them."""

    def _controlled(self, retention: float = 60.0):
        class Controlled:
            def __init__(self) -> None:
                self.now = ManualClock()()

            def __call__(self):
                return self.now

        clock = Controlled()
        transport = FakeSeestarTransport()
        provider = SeestarProvider(make_config(stale_retention_s=retention), transport, clock=clock)
        from tsn_dss.engine.device_runtime import ProviderRuntime

        runtime = ProviderRuntime(provider, clock=ManualClock(), id_generator=SequentialIdGenerator())
        device = runtime.discover().devices[0]
        connection = runtime.connect(runtime.open_connection(device))
        return runtime, provider, transport, connection, clock

    def test_retention_boundary_is_inclusive_at_exactly_the_configured_age(self) -> None:
        runtime, _, transport, connection, clock = self._controlled(60.0)
        clock.now = clock.now + timedelta(seconds=1000)
        runtime.read_telemetry(connection)  # known at T0
        t0 = clock.now
        transport.fail_next("app_state", SeestarUnreachable("connection_lost"))
        clock.now = t0 + timedelta(seconds=60)
        at_limit = item(runtime.read_telemetry(connection), "app.wide.rtsp_state")
        self.assertEqual((at_limit.state, at_limit.value), (ValueState.STALE, "working"))
        transport.fail_next("app_state", SeestarUnreachable("connection_lost"))
        clock.now = t0 + timedelta(seconds=60, microseconds=1)
        past_limit = item(runtime.read_telemetry(connection), "app.wide.rtsp_state")
        self.assertEqual((past_limit.state, past_limit.value), (ValueState.UNAVAILABLE, None))

    def test_zero_retention_never_serves_retained_values(self) -> None:
        runtime, _, transport, connection, clock = self._controlled(0.0)
        runtime.read_telemetry(connection)
        transport.fail_next("app_state", SeestarUnreachable("connection_lost"))
        clock.now = clock.now + timedelta(milliseconds=1)
        self.assertEqual(item(runtime.read_telemetry(connection), "app.wide.rtsp_state").state, ValueState.UNAVAILABLE)

    def test_stale_values_are_never_known_and_failed_reads_never_refresh_the_cache(self) -> None:
        runtime, provider, transport, connection, clock = self._controlled(60.0)
        runtime.read_telemetry(connection)
        stored = dict(provider._last_good["s30pro_0badc0de"])
        transport.fail_next("app_state", SeestarUnreachable("connection_lost"))
        clock.now = clock.now + timedelta(seconds=10)
        partial = runtime.read_telemetry(connection)
        stale = [i for i in partial.items if i.state is ValueState.STALE]
        self.assertTrue(stale)
        self.assertTrue(all(i.name.startswith("app.") for i in stale))
        self.assertEqual(provider._last_good["s30pro_0badc0de"].keys(), stored.keys())
        for name, (_item, when) in provider._last_good["s30pro_0badc0de"].items():
            if name.startswith("app."):
                self.assertEqual(when, stored[name][1])  # timestamps of unrefreshed values did not advance

    def test_a_different_device_at_the_endpoint_invalidates_retained_values(self) -> None:
        runtime, provider, transport, connection, clock = self._controlled(60.0)
        runtime.read_telemetry(connection)
        transport.state_reply["result"]["device"]["sn"] = "feedface"
        with self.assertRaises(Exception) as ctx:
            runtime.read_telemetry(connection)
        self.assertEqual(ctx.exception.category, "identity_mismatch")
        self.assertNotIn("s30pro_0badc0de", provider._last_good)
        transport.state_reply["result"]["device"]["sn"] = "0badc0de"
        transport.fail_next("app_state", SeestarUnreachable("connection_lost"))
        after = runtime.read_telemetry(connection)
        self.assertEqual(item(after, "app.wide.rtsp_state").state, ValueState.UNAVAILABLE)  # nothing resurrected

    def test_capability_and_preview_reads_refuse_a_different_device(self) -> None:
        runtime, _, transport, connection, _ = self._controlled()
        transport.state_reply["result"]["device"]["sn"] = "feedface"
        for read in (runtime.capability_report, runtime.describe_preview):
            with self.assertRaises(Exception) as ctx:
                read(connection)
            self.assertEqual(ctx.exception.category, "identity_mismatch")

    def test_reconfiguring_the_host_drops_values_from_the_old_endpoint(self) -> None:
        runtime, provider, _, connection, _ = self._controlled()
        runtime.read_telemetry(connection)
        self.assertTrue(provider._last_good)
        provider.reconfigure_host(OTHER_HOST)
        self.assertEqual(provider._last_good, {})
        with self.assertRaises(Exception):
            runtime.read_telemetry(connection)  # old session has no endpoint until rediscovery

    def test_unverifiable_identity_in_a_telemetry_reply_is_a_failed_group_not_trusted_data(self) -> None:
        runtime, _, transport, connection, _ = self._controlled()
        runtime.read_telemetry(connection)
        del transport.state_reply["result"]["device"]["sn"]
        sample = runtime.read_telemetry(connection)
        self.assertEqual(item(sample, "mount.move_type").state, ValueState.STALE)  # retained, flagged, never KNOWN

    def test_partial_failure_leaves_the_connection_ready_so_item_states_are_the_only_freshness_signal(self) -> None:
        """Documented limitation: DB-01 refresh treats a non-raising read as success."""
        runtime, _, transport, connection, _ = self._controlled()
        transport.fail_next("app_state", SeestarUnreachable("connection_lost"))
        self.assertIs(runtime.refresh_evidence(connection), C.READY)
        sample = runtime.read_telemetry(connection)
        self.assertEqual(item(sample, "app.wide.rtsp_state").state, ValueState.KNOWN)


if __name__ == "__main__":
    unittest.main()
