"""DB-01 capability, telemetry, preview and simulator conformance (DSS-CTR-013 sections 7, 8, 12)."""

from __future__ import annotations

import unittest
from dataclasses import fields
from datetime import datetime, timedelta, timezone

from tsn_dss.engine.device_runtime import (
    CapabilityConfirmation,
    CapabilityEntry,
    CapabilityReport,
    ConnectionNotActive,
    ConnectionState,
    ContractViolation,
    DeviceReference,
    ManualClock,
    PreviewAvailability,
    PreviewDescriptor,
    ProviderRuntime,
    SequentialIdGenerator,
    SimulatorProvider,
    TelemetryItem,
    TelemetrySample,
    TelemetrySource,
    ValueState,
)

try:
    from device_runtime_support import connected_runtime, make_provider, make_runtime
except ModuleNotFoundError:  # pragma: no cover
    from tests.device_runtime_support import connected_runtime, make_provider, make_runtime

UTC = timezone.utc
NOW = datetime(2026, 1, 1, tzinfo=UTC)


class CapabilityReportTests(unittest.TestCase):
    def test_report_separates_support_from_current_availability(self) -> None:
        """REQ-013."""
        runtime, _, connection = connected_runtime()
        report = runtime.capability_report(connection)
        telemetry = report.entry("telemetry.read")
        synthetic = report.entry("sim.synthetic_action")
        unsupported = report.entry("heater.control")
        self.assertEqual((telemetry.supported_by_provider, telemetry.available_now), (True, True))
        self.assertEqual((synthetic.supported_by_provider, synthetic.available_now), (True, False))
        self.assertEqual((unsupported.supported_by_provider, unsupported.available_now), (False, False))
        self.assertTrue(synthetic.requires_safety_gate)
        self.assertIs(unsupported.confirmation, CapabilityConfirmation.UNSUPPORTED)

    def test_provider_level_report_makes_no_availability_claim(self) -> None:
        """REQ-013, REQ-056: availability is only meaningful for a Connection."""
        runtime, _ = make_runtime()
        report = runtime.capability_report()
        self.assertIsNone(report.connection_id)
        self.assertTrue(all(e.available_now is None for e in report.entries))
        with self.assertRaises(ValueError):
            CapabilityReport("p", None, NOW, (CapabilityEntry("x", True, True, False,
                             CapabilityConfirmation.SIMULATED, True),), True)

    def test_report_identifies_provider_and_connection_and_carries_observation_time(self) -> None:
        """REQ-014, REQ-056."""
        runtime, _, connection = connected_runtime()
        report = runtime.capability_report(connection)
        self.assertEqual(report.provider_id, runtime.provider_id)
        self.assertEqual(report.connection_id, connection.connection_id)
        self.assertIsNotNone(report.observed_at.tzinfo)
        second = runtime.capability_report(connection)
        self.assertGreater(second.observed_at, report.observed_at)
        with self.assertRaises(ValueError):
            CapabilityReport("p", None, datetime(2026, 1, 1), (), True)

    def test_report_is_never_proof_of_command_success(self) -> None:
        """REQ-015, REQ-057: even an available capability proves nothing about a Command."""
        runtime, _, connection = connected_runtime()
        report = runtime.capability_report(connection)
        self.assertIs(report.proves_command_success, False)
        self.assertTrue(report.entry("telemetry.read").available_now)
        self.assertIs(report.proves_command_success, False)
        names = {f.name for f in fields(CapabilityReport)} | {f.name for f in fields(CapabilityEntry)}
        self.assertTrue(names.isdisjoint({"succeeded", "guaranteed", "will_succeed", "verified_effect"}))

    def test_report_is_a_point_in_time_snapshot_not_live_state(self) -> None:
        """REQ-057 (model part): a report predates later state and asserts no freshness of its own."""
        runtime, _, connection = connected_runtime()
        report = runtime.capability_report(connection)
        runtime.report_transport_loss(connection, ConnectionState.DISCONNECTED)
        lost_at = connection.history[-1].at
        self.assertTrue(report.entry("telemetry.read").available_now)  # unchanged snapshot
        self.assertLess(report.observed_at, lost_at)  # consumers must compare time to judge freshness
        with self.assertRaises(ConnectionNotActive):
            runtime.capability_report(connection)  # fresh evidence must be re-read
        names = {f.name for f in fields(CapabilityReport)} | {f.name for f in fields(CapabilityEntry)}
        self.assertTrue(names.isdisjoint({"fresh", "is_fresh", "valid", "valid_until", "current"}))

    def test_inconsistent_entries_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            CapabilityEntry("x", False, True, False, CapabilityConfirmation.SIMULATED, True)
        with self.assertRaises(ValueError):
            CapabilityEntry("x", True, True, False, CapabilityConfirmation.UNSUPPORTED, True)


class TelemetryTests(unittest.TestCase):
    def test_sample_has_host_observation_time_and_optional_provider_time(self) -> None:
        """REQ-058, REQ-016."""
        runtime, _, connection = connected_runtime()
        sample = runtime.read_telemetry(connection)
        self.assertIsNotNone(sample.host_observed_at.tzinfo)
        self.assertIsNotNone(sample.provider_reported_at)
        self.assertEqual(sample.connection_id, connection.connection_id)
        with self.assertRaises(ValueError):
            TelemetrySample("p", "c", datetime(2026, 1, 1), (), True)

    def test_provider_reported_and_host_observed_evidence_are_distinguished(self) -> None:
        """REQ-017."""
        runtime, _, connection = connected_runtime()
        sample = runtime.read_telemetry(connection)
        self.assertIs(sample.get("tracking_state").source, TelemetrySource.PROVIDER_REPORTED)
        host = sample.get("connection_state")
        self.assertIs(host.source, TelemetrySource.HOST_OBSERVED)
        self.assertEqual(host.value, "connected")
        self.assertIsNone(sample.get("connection_state", TelemetrySource.PROVIDER_REPORTED))

    def test_unknown_stale_and_unavailable_state_is_preserved_not_fabricated(self) -> None:
        """REQ-058."""
        runtime, _, connection = connected_runtime()
        sample = runtime.read_telemetry(connection)
        self.assertIs(sample.get("ambient_temperature_c").state, ValueState.STALE)
        self.assertEqual(sample.get("ambient_temperature_c").value, 12.5)
        self.assertIs(sample.get("focus_position").state, ValueState.UNKNOWN)
        self.assertIsNone(sample.get("focus_position").value)
        self.assertIs(sample.get("battery_percent").state, ValueState.UNAVAILABLE)
        self.assertIsNone(sample.get("battery_percent").value)
        for state in (ValueState.UNKNOWN, ValueState.UNAVAILABLE):
            with self.assertRaises(ValueError):
                TelemetryItem("x", TelemetrySource.PROVIDER_REPORTED, state, 0)

    def test_provider_cannot_claim_host_observed_evidence(self) -> None:
        """REQ-017: host-observed evidence is added by the host only."""
        runtime, provider, connection = connected_runtime()
        provider.set_telemetry([TelemetryItem("x", TelemetrySource.HOST_OBSERVED, ValueState.KNOWN, 1)])
        with self.assertRaises(ContractViolation):
            runtime.read_telemetry(connection)

    def test_telemetry_reads_do_not_submit_commands_or_change_state(self) -> None:
        """REQ-016, REQ-059, REQ-022."""
        runtime, provider, connection = connected_runtime()
        before_state = connection.state
        before_history = (runtime.history, connection.history)
        before = list(provider.calls)
        runtime.read_telemetry(connection)
        runtime.capability_report(connection)
        runtime.describe_preview(connection)
        added = [c[0] for c in provider.calls[len(before):]]
        self.assertEqual(added, ["read_telemetry", "describe_capabilities", "describe_preview"])
        self.assertIs(connection.state, before_state)
        self.assertEqual((runtime.history, connection.history), before_history)

    def test_telemetry_is_not_a_domain_fact(self) -> None:
        """REQ-019, REQ-066: no SessionContextFact or SessionEvent shaped fields."""
        names = {f.name for f in fields(TelemetrySample)} | {f.name for f in fields(TelemetryItem)}
        self.assertTrue(names.isdisjoint({"session_id", "observation_id", "fact_id", "event_id", "event_type"}))


class PreviewTests(unittest.TestCase):
    def test_preview_descriptor_is_runtime_evidence_only(self) -> None:
        """REQ-016, REQ-018, REQ-033."""
        runtime, _, connection = connected_runtime()
        preview = runtime.describe_preview(connection)
        self.assertIs(preview.availability, PreviewAvailability.AVAILABLE)
        self.assertTrue(preview.is_runtime_evidence_only)
        self.assertFalse(preview.is_canonical_record)
        self.assertTrue(preview.simulated)
        self.assertEqual(preview.preview_id, "preview-0001")

    def test_preview_descriptor_has_no_capture_frame_or_file_fields(self) -> None:
        """REQ-018, REQ-033: nothing that could become a Capture or Frame."""
        names = {f.name for f in fields(PreviewDescriptor)}
        forbidden = {"capture_id", "frame_id", "project_id", "session_id", "observation_id",
                     "dataset_id", "processing_run_id", "rel_path", "path", "file_path",
                     "content_sha256", "pixels", "data"}
        self.assertTrue(names.isdisjoint(forbidden), names & forbidden)

    def test_repeated_preview_reads_yield_distinct_runtime_ids(self) -> None:
        runtime, _, connection = connected_runtime()
        a, b = runtime.describe_preview(connection), runtime.describe_preview(connection)
        self.assertNotEqual(a.preview_id, b.preview_id)


class SimulatorConformanceTests(unittest.TestCase):
    def test_every_artifact_is_marked_simulated(self) -> None:
        """REQ-030 (Provider, Device, Connection, Capability, Telemetry, Preview), REQ-063."""
        runtime, _, connection = connected_runtime()
        report = runtime.capability_report(connection)
        discovery = runtime.discover()
        self.assertTrue(runtime.descriptor.simulated)
        self.assertTrue(discovery.simulated)
        self.assertTrue(all(d.simulated for d in discovery.devices))
        self.assertTrue(connection.simulated)
        self.assertTrue(report.simulated)
        self.assertTrue(all(e.simulated for e in report.entries))
        self.assertTrue(runtime.capability_report().simulated)
        self.assertTrue(runtime.read_telemetry(connection).simulated)
        self.assertTrue(runtime.describe_preview(connection).simulated)

    def test_simulator_implements_the_same_concepts_as_a_physical_provider(self) -> None:
        """REQ-063: it satisfies the same Provider protocol, Device, Connection, Capability, Telemetry, Preview."""
        from tsn_dss.engine.device_runtime import DeviceProvider

        self.assertIsInstance(make_provider(), DeviceProvider)

    def test_behavior_is_deterministic_for_identical_input(self) -> None:
        """REQ-031: two independent runs produce identical evidence."""

        def run():
            provider = SimulatorProvider(clock=ManualClock())
            runtime = ProviderRuntime(provider, clock=ManualClock(), id_generator=SequentialIdGenerator())
            discovery = runtime.discover()
            connection = runtime.connect(runtime.open_connection(discovery.devices[0]))
            runtime.refresh_evidence(connection)
            return (
                discovery,
                runtime.capability_report(connection),
                runtime.read_telemetry(connection),
                runtime.describe_preview(connection),
                [(r.event, r.to_state, r.at) for r in runtime.history],
                [(r.event, r.to_state, r.at) for r in connection.history],
                provider.calls,
            )

        self.assertEqual(run(), run())

    def test_ids_are_injectable_and_sequential(self) -> None:
        """REQ-031."""
        gen = SequentialIdGenerator("t-")
        self.assertEqual([gen("conn"), gen("conn"), gen("preview")], ["t-conn-0001", "t-conn-0002", "t-preview-0001"])

    def test_manual_clock_is_deterministic_and_requires_timezone(self) -> None:
        clock = ManualClock(NOW, timedelta(seconds=5))
        self.assertEqual((clock(), clock()), (NOW, NOW + timedelta(seconds=5)))
        clock.advance(timedelta(minutes=1))
        self.assertEqual(clock(), NOW + timedelta(seconds=70))
        with self.assertRaises(ValueError):
            ManualClock(datetime(2026, 1, 1))

    def test_simulator_does_no_io(self) -> None:
        """Boundary: scripted, in-memory provider; calls are recorded not performed."""
        runtime, provider = make_runtime()
        runtime.discover()
        self.assertTrue(all(call[0] == "discover" for call in provider.calls))

    def test_misreporting_provider_is_rejected(self) -> None:
        """Conformance: a non-simulated claim from a simulated Provider is a violation."""

        class Lying(SimulatorProvider):
            def describe_capabilities(self, connection_id):
                return (CapabilityEntry("x", True, None if connection_id is None else True, False,
                                        CapabilityConfirmation.HARDWARE_CONFIRMED, False),)

        liar = Lying(clock=ManualClock())
        liar_runtime = ProviderRuntime(liar, clock=ManualClock(), id_generator=SequentialIdGenerator())
        conn = liar_runtime.connect(liar_runtime.open_connection(liar_runtime.discover().devices[0]))
        with self.assertRaises(ContractViolation):
            liar_runtime.capability_report(conn)

    def test_unsimulated_provider_artifacts_are_not_marked_simulated(self) -> None:
        """REQ-030 converse: the marking comes from the Provider, not a constant."""

        class Real(SimulatorProvider):
            def __init__(self):
                super().__init__(clock=ManualClock())
                self._descriptor = type(self._descriptor)(
                    provider_id="real-like", provider_kind="test", implementation_label="x",
                    simulated=False, configuration_status=self._descriptor.configuration_status)

            def discover(self):
                now = ManualClock()()
                return (DeviceReference("real-like", "d1", now, False),)

        runtime = ProviderRuntime(Real(), clock=ManualClock())
        self.assertFalse(runtime.discover().simulated)
        self.assertFalse(runtime.discover().devices[0].simulated)


if __name__ == "__main__":
    unittest.main()
