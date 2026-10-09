"""DB-01 Provider identity, lifecycle, registry and discovery (DSS-CTR-013 sections 4-5)."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from tsn_dss.engine.device_runtime import (
    PROVIDER_TRANSITIONS,
    ConfigurationStatus,
    ContractViolation,
    DeviceReference,
    DiscoveryOutcome,
    InvalidTransition,
    ManualClock,
    ProviderAlreadyRegistered,
    ProviderDescriptor,
    ProviderEvent,
    ProviderLifecycleState,
    ProviderNotUsable,
    ProviderRegistry,
    ProviderRuntime,
    SimulatedDeviceSpec,
    UnknownProvider,
    provider_next_state,
)

try:
    from device_runtime_support import make_provider, make_registry, make_runtime
except ModuleNotFoundError:  # pragma: no cover - package-style invocation
    from tests.device_runtime_support import make_provider, make_registry, make_runtime

S = ProviderLifecycleState
E = ProviderEvent


class ProviderIdentityTests(unittest.TestCase):
    def test_descriptor_declares_identity_kind_label_simulation_and_configuration(self) -> None:
        """REQ-001, REQ-002: stable id plus kind, label, simulation and configuration status."""
        descriptor = make_provider().descriptor
        self.assertEqual(descriptor.provider_id, "sim-provider")
        self.assertEqual(descriptor.provider_kind, "simulator")
        self.assertTrue(descriptor.implementation_label)
        self.assertTrue(descriptor.simulated)
        self.assertIs(descriptor.configuration_status, ConfigurationStatus.CONFIGURED)
        with self.assertRaises(FrozenInstanceError):
            descriptor.provider_id = "other"  # type: ignore[misc]

    def test_descriptor_rejects_missing_declarations(self) -> None:
        """REQ-002."""
        for kwargs in ({"provider_id": " "}, {"provider_kind": ""}, {"implementation_label": ""}):
            base = dict(
                provider_id="p",
                provider_kind="k",
                implementation_label="l",
                simulated=True,
                configuration_status=ConfigurationStatus.CONFIGURED,
            )
            base.update(kwargs)
            with self.assertRaises(ValueError):
                ProviderDescriptor(**base)

    def test_provider_identity_is_independent_of_devices_connections_and_endpoints(self) -> None:
        """REQ-035: identity does not change with devices, connections or endpoint hints."""
        runtime, _ = make_runtime(make_provider(devices=(SimulatedDeviceSpec("a", endpoint_hint="x:1"),)))
        before = runtime.provider_id
        device = runtime.discover().devices[0]
        runtime.connect(runtime.open_connection(device))
        runtime.connect(runtime.open_connection(device))
        self.assertEqual(runtime.provider_id, before)
        self.assertNotEqual(runtime.provider_id, device.device_ref)
        for connection in runtime.connections:
            self.assertNotEqual(runtime.provider_id, connection.connection_id)

    def test_provider_with_zero_or_many_devices(self) -> None:
        """Section 4: a Provider MAY support zero, one or many Devices."""
        for count in (0, 1, 3):
            specs = tuple(SimulatedDeviceSpec(f"d{i}") for i in range(count))
            runtime, _ = make_runtime(make_provider(devices=specs))
            self.assertEqual(len(runtime.discover().devices), count)


class ProviderRegistryTests(unittest.TestCase):
    def test_register_get_and_list_are_deterministic(self) -> None:
        """REQ-001."""
        registry = make_registry()
        registry.register(make_provider(provider_id="zeta"))
        registry.register(make_provider(provider_id="alpha"))
        self.assertEqual([d.provider_id for d in registry.list_descriptors()], ["alpha", "zeta"])
        self.assertEqual(registry.get("alpha").provider_id, "alpha")

    def test_duplicate_and_unknown_provider_ids_are_rejected(self) -> None:
        """REQ-001."""
        registry = make_registry()
        registry.register(make_provider())
        with self.assertRaises(ProviderAlreadyRegistered):
            registry.register(make_provider())
        with self.assertRaises(UnknownProvider):
            registry.get("missing")

    def test_registration_has_no_domain_collaborators(self) -> None:
        """REQ-003: the registry holds only a clock, an id source and runtimes."""
        registry = ProviderRegistry()
        self.assertEqual(
            sorted(a for a in registry.__dict__ if not a.startswith("__")),
            ["_clock", "_ids", "_runtimes"],
        )


class ProviderLifecycleTableTests(unittest.TestCase):
    def test_every_contract_transition_is_present(self) -> None:
        """REQ-004, REQ-036: the section 4 table, row by row."""
        expected = {
            (S.UNCONFIGURED, E.CONFIGURATION_SUPPLIED): S.CONFIGURED,
            (S.CONFIGURED, E.DISCOVERY_STARTED): S.DISCOVERING,
            (S.AVAILABLE, E.DISCOVERY_STARTED): S.DISCOVERING,
            (S.DEGRADED, E.DISCOVERY_STARTED): S.DISCOVERING,
            (S.DISCOVERING, E.DISCOVERY_FOUND_DEVICES): S.AVAILABLE,
            (S.DISCOVERING, E.DISCOVERY_EMPTY_VALID): S.AVAILABLE,
            (S.DISCOVERING, E.DISCOVERY_EMPTY_REQUIRED_DEVICE_MISSING): S.DEGRADED,
            (S.DISCOVERING, E.DISCOVERY_FAILED_NONFATAL): S.DEGRADED,
            (S.DISCOVERING, E.DISCOVERY_FAILED_FATAL): S.FAILED,
            (S.CONFIGURED, E.HEALTH_PARTIAL): S.DEGRADED,
            (S.AVAILABLE, E.HEALTH_PARTIAL): S.DEGRADED,
            (S.DEGRADED, E.HEALTH_RECOVERED): S.AVAILABLE,
            (S.FAILED, E.RESET_TO_CONFIGURED): S.CONFIGURED,
            (S.FAILED, E.RESET_TO_UNCONFIGURED): S.UNCONFIGURED,
        }
        for state in S:
            expected[(state, E.FATAL_CONDITION)] = S.FAILED
        self.assertEqual(PROVIDER_TRANSITIONS, expected)

    def test_invalid_transitions_raise(self) -> None:
        """REQ-004."""
        invalid = [
            (S.UNCONFIGURED, E.DISCOVERY_STARTED),
            (S.DISCOVERING, E.DISCOVERY_STARTED),
            (S.AVAILABLE, E.CONFIGURATION_SUPPLIED),
            (S.FAILED, E.DISCOVERY_STARTED),
            (S.FAILED, E.HEALTH_RECOVERED),
            (S.AVAILABLE, E.HEALTH_RECOVERED),
            (S.DISCOVERING, E.HEALTH_PARTIAL),
            (S.CONFIGURED, E.RESET_TO_CONFIGURED),
        ]
        for state, event in invalid:
            with self.assertRaises(InvalidTransition, msg=(state, event)):
                provider_next_state(state, event)

    def test_provider_lifecycle_has_no_device_level_states(self) -> None:
        """REQ-005: no connecting, connected, ready, busy or unknown_result."""
        names = {state.value for state in ProviderLifecycleState}
        self.assertTrue(names.isdisjoint({"connecting", "connected", "ready", "busy", "unknown_result"}))
        self.assertEqual(
            names, {"unconfigured", "configured", "discovering", "available", "degraded", "failed"}
        )
        reachable = {to.value for to in PROVIDER_TRANSITIONS.values()}
        self.assertTrue(reachable <= names)


class DiscoveryTests(unittest.TestCase):
    def test_successful_discovery_returns_provider_native_references(self) -> None:
        """REQ-006, REQ-008, REQ-036."""
        runtime, _ = make_runtime(make_provider(devices=(SimulatedDeviceSpec("sim-a", "A", "M", "1.0"),)))
        result = runtime.discover()
        self.assertIs(result.outcome, DiscoveryOutcome.DEVICES_FOUND)
        self.assertIs(result.provider_state, S.AVAILABLE)
        (device,) = result.devices
        self.assertEqual((device.provider_id, device.device_ref), ("sim-provider", "sim-a"))
        self.assertEqual((device.label, device.model, device.firmware_version), ("A", "M", "1.0"))
        self.assertTrue(device.simulated)
        self.assertLess(result.requested_at, result.completed_at)
        self.assertIs(runtime.last_discovery, result)

    def test_valid_empty_discovery_is_not_a_failure(self) -> None:
        """REQ-037, REQ-036."""
        runtime, _ = make_runtime(make_provider(devices=()))
        result = runtime.discover()
        self.assertIs(result.outcome, DiscoveryOutcome.EMPTY_VALID)
        self.assertEqual(result.devices, ())
        self.assertIs(runtime.state, S.AVAILABLE)

    def test_empty_discovery_degrades_when_a_specific_device_is_required(self) -> None:
        """REQ-037: zero devices degrades only when configuration requires a Device."""
        runtime, _ = make_runtime(make_provider(devices=(), device_required=True))
        result = runtime.discover()
        self.assertIs(result.outcome, DiscoveryOutcome.EMPTY_REQUIRED_DEVICE_MISSING)
        self.assertIs(runtime.state, S.DEGRADED)

    def test_nonfatal_failure_degrades_and_recovers_on_refresh(self) -> None:
        """REQ-036: nonfatal failure, then recovery refresh from degraded."""
        runtime, provider = make_runtime()
        provider.script_discovery_failure("timeout", "no answer")
        failed = runtime.discover()
        self.assertIs(failed.outcome, DiscoveryOutcome.NONFATAL_FAILURE)
        self.assertEqual((failed.error_category, failed.error_message), ("timeout", "no answer"))
        self.assertEqual(failed.devices, ())
        self.assertIs(runtime.state, S.DEGRADED)
        recovered = runtime.discover()
        self.assertIs(recovered.outcome, DiscoveryOutcome.DEVICES_FOUND)
        self.assertIs(runtime.state, S.AVAILABLE)

    def test_fatal_failure_fails_the_provider_until_reset(self) -> None:
        """REQ-036, section 4: failed is terminal until reset."""
        runtime, provider = make_runtime()
        provider.script_discovery_failure("auth", "bad key", fatal=True)
        result = runtime.discover()
        self.assertIs(result.outcome, DiscoveryOutcome.FATAL_FAILURE)
        self.assertIs(runtime.state, S.FAILED)
        with self.assertRaises(InvalidTransition):
            runtime.discover()
        runtime.reset(configured=True)
        self.assertIs(runtime.state, S.CONFIGURED)
        self.assertIs(runtime.discover().outcome, DiscoveryOutcome.DEVICES_FOUND)

    def test_refresh_from_available_and_from_degraded(self) -> None:
        """REQ-036: refresh from available and from degraded."""
        runtime, provider = make_runtime()
        runtime.discover()
        self.assertIs(runtime.state, S.AVAILABLE)
        runtime.discover()
        self.assertIs(runtime.state, S.AVAILABLE)
        provider.script_discovery_failure("blip")
        runtime.discover()
        self.assertIs(runtime.state, S.DEGRADED)
        provider.script_empty_discovery()
        runtime.discover()
        self.assertIs(runtime.state, S.AVAILABLE)

    def test_recovery_after_empty_required_device_missing(self) -> None:
        """REQ-036, REQ-037."""
        runtime, provider = make_runtime(make_provider(device_required=True))
        provider.script_empty_discovery()
        runtime.discover()
        self.assertIs(runtime.state, S.DEGRADED)
        self.assertIs(runtime.discover().outcome, DiscoveryOutcome.DEVICES_FOUND)
        self.assertIs(runtime.state, S.AVAILABLE)

    def test_discovery_submits_no_commands_and_makes_only_discover_calls(self) -> None:
        """REQ-007: discovery calls only the Provider discover operation."""
        runtime, provider = make_runtime()
        runtime.discover()
        self.assertEqual(provider.calls, [("discover",)])
        self.assertFalse(hasattr(provider, "submit_command"))
        self.assertFalse(hasattr(runtime, "submit_command"))

    def test_device_reference_is_not_a_domain_identity(self) -> None:
        """REQ-008, REQ-065: a Device Reference carries no domain or Target identity fields."""
        fields = set(DeviceReference.__dataclass_fields__)
        forbidden = {
            "project_id", "session_id", "observation_id", "target_id", "capture_id",
            "frame_id", "dataset_id", "processing_run_id", "site_id", "target_name",
            "ra_hours", "dec_deg",
        }
        self.assertTrue(fields.isdisjoint(forbidden))
        self.assertEqual(set(DeviceReference.__dataclass_fields__) & {"id"}, set())

    def test_provider_evidence_for_another_provider_is_rejected(self) -> None:
        """Conformance: discovered references must name the registering Provider."""

        class Mislabelled:
            descriptor = make_provider().descriptor

            def discover(self):
                return (
                    DeviceReference("someone-else", "d", ManualClock()(), True),
                )

        runtime = ProviderRuntime(Mislabelled(), clock=ManualClock())  # type: ignore[arg-type]
        with self.assertRaises(ContractViolation):
            runtime.discover()
        self.assertIs(runtime.state, S.FAILED)

    def test_lifecycle_history_records_evidence_in_order(self) -> None:
        """Section 4 evidence column: each transition is recorded with a timestamp."""
        runtime, provider = make_runtime()
        provider.script_discovery_failure("timeout")
        runtime.discover()
        runtime.discover()
        events = [(r.from_state, r.event, r.to_state) for r in runtime.history]
        self.assertEqual(
            events,
            [
                ("configured", "discovery_started", "discovering"),
                ("discovering", "discovery_failed_nonfatal", "degraded"),
                ("degraded", "discovery_started", "discovering"),
                ("discovering", "discovery_found_devices", "available"),
            ],
        )
        self.assertEqual(runtime.history[1].evidence, "timeout")
        times = [r.at for r in runtime.history]
        self.assertEqual(times, sorted(times))

    def test_health_transitions_outside_discovery(self) -> None:
        """Section 4: partial health and recovery without discovery refresh."""
        runtime, _ = make_runtime()
        runtime.discover()
        runtime.report_health_partial("stale data")
        self.assertIs(runtime.state, S.DEGRADED)
        runtime.report_health_recovered("fresh evidence")
        self.assertIs(runtime.state, S.AVAILABLE)
        runtime.report_fatal_condition("crash", "gone")
        self.assertIs(runtime.state, S.FAILED)

    def test_unconfigured_provider_needs_configuration_before_discovery(self) -> None:
        """Section 4: unconfigured -> configured -> discovering."""
        runtime, _ = make_runtime(make_provider(configured=False))
        self.assertIs(runtime.state, S.UNCONFIGURED)
        with self.assertRaises(InvalidTransition):
            runtime.discover()
        runtime.supply_configuration()
        self.assertIs(runtime.state, S.CONFIGURED)
        runtime.discover()
        self.assertIs(runtime.state, S.AVAILABLE)

    def test_connection_objects_can_be_created_for_any_provider_state(self) -> None:
        """Section 6: ``new`` is only an object; creating it is not a transport attempt."""
        runtime, _ = make_runtime(make_provider(configured=False))
        device = DeviceReference("sim-provider", "d", ManualClock()(), True)
        connection = runtime.open_connection(device)
        self.assertEqual(connection.state.value, "new")

    def test_first_connection_attempt_is_refused_while_unconfigured_or_failed(self) -> None:
        """Section 4: unconfigured lacks configuration to attempt a connection; failed cannot proceed."""
        runtime, provider = make_runtime(make_provider(configured=False))
        device = DeviceReference("sim-provider", "sim-device-1", ManualClock()(), True)
        connection = runtime.open_connection(device)
        with self.assertRaises(ProviderNotUsable):
            runtime.connect(connection)
        self.assertEqual(connection.state.value, "new")
        self.assertEqual(connection.history, ())
        self.assertEqual(provider.calls, [])
        runtime.supply_configuration()
        runtime.report_fatal_condition("crash")
        with self.assertRaises(ProviderNotUsable):
            runtime.connect(connection)
        self.assertEqual(provider.calls, [])
        runtime.reset(configured=True)
        self.assertEqual(runtime.connect(connection).state.value, "connected")

    def test_connection_attempts_are_allowed_in_configured_available_and_degraded(self) -> None:
        """Section 4: configured can attempt connection; degraded is partially usable."""
        for prepare in (lambda r: None, lambda r: r.discover(),
                        lambda r: (r.discover(), r.report_health_partial())):
            runtime, _ = make_runtime()
            device = DeviceReference("sim-provider", "sim-device-1", ManualClock()(), True)
            prepare(runtime)
            connection = runtime.connect(runtime.open_connection(device))
            self.assertEqual(connection.state.value, "connected", runtime.state)

    def test_provider_failure_does_not_block_cleanup_of_established_connections(self) -> None:
        """Only the first attempt is gated; repeated connect and disconnect are unaffected."""
        runtime, provider = make_runtime()
        device = runtime.discover().devices[0]
        connection = runtime.connect(runtime.open_connection(device))
        runtime.report_fatal_condition("crash")
        runtime.connect(connection)
        self.assertEqual(len([c for c in provider.calls if c[0] == "connect"]), 1)
        runtime.disconnect(connection)
        self.assertEqual(connection.state.value, "disconnected")


if __name__ == "__main__":
    unittest.main()
