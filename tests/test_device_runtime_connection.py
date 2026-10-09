"""DB-01 Connection identity and lifecycle (DSS-CTR-013 section 6)."""

from __future__ import annotations

import unittest

from tsn_dss.engine.device_runtime import (
    CONNECTION_TRANSITIONS,
    CommandRef,
    CommandState,
    Connection,
    ConnectionEvent,
    ConnectionNotActive,
    ConnectionState,
    ContractViolation,
    InvalidTransition,
    ProviderCall,
    ProviderEvent,
    TransitionRecord,
    connection_next,
)
from tsn_dss.engine.device_runtime.models import COMMAND_TERMINAL_STATES

try:
    from device_runtime_support import connected_runtime, make_provider, make_runtime
except ModuleNotFoundError:  # pragma: no cover
    from tests.device_runtime_support import connected_runtime, make_provider, make_runtime

C = ConnectionState
V = ConnectionEvent


def provider_calls(provider, name):
    return [call for call in provider.calls if call[0] == name]


class ConnectionIdentityTests(unittest.TestCase):
    def test_connection_has_runtime_id_one_provider_and_one_device(self) -> None:
        """REQ-009, REQ-010."""
        runtime, _ = make_runtime()
        device = runtime.discover().devices[0]
        connection = runtime.open_connection(device)
        self.assertEqual(connection.connection_id, "conn-0001")
        self.assertEqual(connection.provider_id, runtime.provider_id)
        self.assertIs(connection.device, device)
        self.assertIs(connection.state, C.NEW)
        self.assertTrue(connection.simulated)
        with self.assertRaises(AttributeError):
            connection.device = device  # type: ignore[misc]

    def test_connection_id_is_not_any_other_identity(self) -> None:
        """REQ-038: connection ids differ from provider, device and command identities."""
        runtime, _ = make_runtime()
        device = runtime.discover().devices[0]
        connection = runtime.open_connection(device)
        self.assertNotIn(connection.connection_id, {runtime.provider_id, device.device_ref})
        ref = CommandRef("cmd-1", runtime.provider_id, connection.connection_id)
        self.assertNotEqual(ref.command_id, connection.connection_id)

    def test_device_of_another_provider_is_rejected(self) -> None:
        """REQ-010."""
        runtime, _ = make_runtime()
        other, _ = make_runtime(make_provider(provider_id="other"))
        foreign = other.discover().devices[0]
        with self.assertRaises(ContractViolation):
            runtime.open_connection(foreign)

    def test_connection_must_belong_to_the_runtime(self) -> None:
        """REQ-010."""
        runtime, _ = make_runtime()
        other, _ = make_runtime()
        stranger = other.open_connection(other.discover().devices[0])
        with self.assertRaises(ContractViolation):
            runtime.connect(stranger)


class ConnectionTableTests(unittest.TestCase):
    def test_contract_rows_present(self) -> None:
        """Section 6 transition table, DB-01 rows."""
        t = connection_next
        self.assertEqual(t(C.NEW, V.CONNECT_REQUESTED).next_state, C.CONNECTING)
        self.assertIs(t(C.NEW, V.CONNECT_REQUESTED).provider_call, ProviderCall.CONNECT)
        self.assertEqual(t(C.CONNECTING, V.TRANSPORT_ESTABLISHED).next_state, C.CONNECTED)
        self.assertEqual(t(C.CONNECTED, V.FRESH_STATE_OBTAINED).next_state, C.READY)
        self.assertEqual(t(C.READY, V.EVIDENCE_STALE_OR_PARTIAL).next_state, C.DEGRADED)
        self.assertEqual(t(C.DEGRADED, V.FRESH_STATE_OBTAINED).next_state, C.READY)
        self.assertEqual(t(C.DEGRADED, V.USABLE_EVIDENCE_RETURNED).next_state, C.CONNECTED)
        for state in (C.CONNECTED, C.READY, C.DEGRADED):
            row = t(state, V.DISCONNECT_REQUESTED)
            self.assertEqual((row.next_state, row.provider_call), (C.DISCONNECTING, ProviderCall.DISCONNECT))
        self.assertEqual(t(C.DISCONNECTING, V.DISCONNECT_COMPLETED).next_state, C.DISCONNECTED)

    def test_repeated_requests_never_require_a_provider_call(self) -> None:
        """REQ-041."""
        repeats = [
            (C.CONNECTING, V.CONNECT_REQUESTED, C.CONNECTING),
            (C.CONNECTED, V.CONNECT_REQUESTED, C.CONNECTED),
            (C.READY, V.CONNECT_REQUESTED, C.READY),
            (C.DISCONNECTING, V.DISCONNECT_REQUESTED, C.DISCONNECTING),
            (C.NEW, V.DISCONNECT_REQUESTED, C.NEW),
            (C.DISCONNECTED, V.DISCONNECT_REQUESTED, C.DISCONNECTED),
        ]
        for state, event, expected in repeats:
            row = connection_next(state, event)
            self.assertEqual(row.next_state, expected)
            self.assertIsNone(row.provider_call, (state, event))

    def test_transport_loss_and_cannot_continue_from_every_nonterminal_state(self) -> None:
        """Section 6: any nonterminal state -> disconnected, degraded or failed."""
        nonterminal = (C.NEW, C.CONNECTING, C.CONNECTED, C.READY, C.DEGRADED, C.DISCONNECTING)
        for state in nonterminal:
            self.assertEqual(connection_next(state, V.TRANSPORT_LOST_DISCONNECTED).next_state, C.DISCONNECTED)
            self.assertEqual(connection_next(state, V.TRANSPORT_LOST_DEGRADED).next_state, C.DEGRADED)
            self.assertEqual(connection_next(state, V.TRANSPORT_LOST_FAILED).next_state, C.FAILED)
            self.assertEqual(connection_next(state, V.CANNOT_CONTINUE).next_state, C.FAILED)

    def test_terminal_states_are_never_reactivated(self) -> None:
        """REQ-042, REQ-043: disconnected and failed accept no activating event."""
        activating = [e for e in V if e is not V.DISCONNECT_REQUESTED]
        for state in (C.DISCONNECTED, C.FAILED):
            for event in activating:
                with self.assertRaises(InvalidTransition, msg=(state, event)):
                    connection_next(state, event)
        with self.assertRaises(InvalidTransition):
            connection_next(C.FAILED, V.DISCONNECT_REQUESTED)

    def test_connection_lifecycle_excludes_unknown_result(self) -> None:
        """REQ-012: unknown_result is a Command outcome only."""
        self.assertNotIn("unknown_result", {s.value for s in ConnectionState})
        self.assertIn("unknown_result", {s.value for s in CommandState})
        self.assertNotIn("unknown_result", {e.value for e in V})

    def test_connection_state_and_command_state_are_independent_types(self) -> None:
        """REQ-011: separate enumerations; equal-valued members never compare equal."""
        self.assertIsNot(ConnectionState, CommandState)
        self.assertEqual({s.value for s in ConnectionState} & {s.value for s in CommandState}, {"failed"})
        self.assertNotEqual(ConnectionState.FAILED, CommandState.FAILED)

    def test_busy_is_a_state_name_only_in_db01(self) -> None:
        """DB-04 boundary: no DB-01 transition enters or leaves ``busy``."""
        for (state, _event), row in CONNECTION_TRANSITIONS.items():
            self.assertIsNot(state, C.BUSY)
            self.assertIsNot(row.next_state, C.BUSY)


class ConnectionBehaviorTests(unittest.TestCase):
    def test_connect_establishes_transport_with_one_provider_call(self) -> None:
        """REQ-041."""
        runtime, provider, connection = connected_runtime()
        self.assertIs(connection.state, C.CONNECTED)
        self.assertEqual(len(provider_calls(provider, "connect")), 1)

    def test_repeated_connect_is_idempotent_without_duplicate_provider_calls(self) -> None:
        """REQ-041."""
        runtime, provider, connection = connected_runtime()
        runtime.connect(connection)
        runtime.connect(connection)
        self.assertIs(connection.state, C.CONNECTED)
        self.assertEqual(len(provider_calls(provider, "connect")), 1)
        self.assertEqual(runtime.refresh_evidence(connection), C.READY)
        runtime.connect(connection)
        self.assertIs(connection.state, C.READY)
        self.assertEqual(len(provider_calls(provider, "connect")), 1)

    def test_repeated_connect_while_connecting_makes_no_duplicate_call(self) -> None:
        """REQ-041: reentrant connect during the in-flight attempt."""
        runtime, provider = make_runtime()
        connection = runtime.open_connection(runtime.discover().devices[0])
        original = provider.connect
        seen: list[C] = []

        def reentrant(connection_id, device):
            seen.append(connection.state)
            runtime.connect(connection)  # repeated request while connecting
            original(connection_id, device)

        provider.connect = reentrant  # type: ignore[method-assign]
        runtime.connect(connection)
        self.assertEqual(seen, [C.CONNECTING])
        self.assertIs(connection.state, C.CONNECTED)
        self.assertEqual(len(provider_calls(provider, "connect")), 1)

    def test_disconnect_then_repeated_disconnect_is_idempotent(self) -> None:
        """REQ-041."""
        runtime, provider, connection = connected_runtime()
        runtime.disconnect(connection)
        self.assertIs(connection.state, C.DISCONNECTED)
        runtime.disconnect(connection)
        runtime.disconnect(connection)
        self.assertIs(connection.state, C.DISCONNECTED)
        self.assertEqual(len(provider_calls(provider, "disconnect")), 1)

    def test_repeated_disconnect_while_disconnecting_makes_no_duplicate_call(self) -> None:
        """REQ-041: reentrant disconnect during the in-flight disconnect."""
        runtime, provider, connection = connected_runtime()
        original = provider.disconnect

        def reentrant(connection_id):
            self.assertIs(connection.state, C.DISCONNECTING)
            runtime.disconnect(connection)
            original(connection_id)

        provider.disconnect = reentrant  # type: ignore[method-assign]
        runtime.disconnect(connection)
        self.assertIs(connection.state, C.DISCONNECTED)
        self.assertEqual(len(provider_calls(provider, "disconnect")), 1)

    def test_disconnect_of_new_connection_is_a_no_op_without_provider_call(self) -> None:
        """Section 6: new + disconnect requested -> idempotent no-op."""
        runtime, provider = make_runtime()
        connection = runtime.open_connection(runtime.discover().devices[0])
        runtime.disconnect(connection)
        self.assertIs(connection.state, C.NEW)
        self.assertEqual(provider_calls(provider, "disconnect"), [])
        self.assertEqual(connection.history, ())

    def test_reconnect_creates_a_new_connection_id_and_never_reactivates(self) -> None:
        """REQ-042."""
        runtime, provider, first = connected_runtime()
        runtime.disconnect(first)
        with self.assertRaises(InvalidTransition):
            runtime.connect(first)
        self.assertIs(first.state, C.DISCONNECTED)
        second = runtime.connect(runtime.open_connection(first.device))
        self.assertNotEqual(first.connection_id, second.connection_id)
        self.assertIs(second.state, C.CONNECTED)
        self.assertIs(first.state, C.DISCONNECTED)
        self.assertEqual(len(runtime.connections), 2)

    def test_failed_connection_is_replaced_not_reset(self) -> None:
        """REQ-043."""
        runtime, provider = make_runtime()
        device = runtime.discover().devices[0]
        provider.script_connect_failure("refused", "no route")
        failed = runtime.connect(runtime.open_connection(device))
        self.assertIs(failed.state, C.FAILED)
        self.assertEqual(failed.history[-1].evidence, "refused")
        with self.assertRaises(InvalidTransition):
            runtime.connect(failed)
        replacement = runtime.replace_failed_connection(failed)
        self.assertIsNot(replacement, failed)
        self.assertNotEqual(replacement.connection_id, failed.connection_id)
        self.assertIs(replacement.device, failed.device)
        self.assertIs(replacement.state, C.NEW)
        self.assertIs(failed.state, C.FAILED)
        runtime.connect(replacement)
        self.assertIs(replacement.state, C.CONNECTED)

    def test_only_failed_connections_can_be_replaced(self) -> None:
        """REQ-043."""
        runtime, _, connection = connected_runtime()
        with self.assertRaises(InvalidTransition):
            runtime.replace_failed_connection(connection)

    def test_unknown_device_fails_the_connection(self) -> None:
        """Section 6: connection cannot continue -> failed."""
        runtime, _ = make_runtime()
        device = runtime.discover().devices[0]
        ghost = type(device)(runtime.provider_id, "ghost", device.discovered_at, True)
        self.assertIs(runtime.connect(runtime.open_connection(ghost)).state, C.FAILED)

    def test_disconnect_failure_marks_the_connection_failed(self) -> None:
        """Section 6: connection cannot continue -> failed."""
        runtime, provider, connection = connected_runtime()
        provider.script_disconnect_failure("stuck")
        runtime.disconnect(connection)
        self.assertIs(connection.state, C.FAILED)

    def test_ready_requires_fresh_evidence_and_degrades_on_partial_failure(self) -> None:
        """Section 6: connected -> ready on fresh evidence; stale/partial -> degraded -> ready."""
        runtime, provider, connection = connected_runtime()
        self.assertIs(connection.state, C.CONNECTED)
        provider.script_evidence_failure("partial")
        self.assertIs(runtime.refresh_evidence(connection), C.DEGRADED)
        self.assertIs(runtime.refresh_evidence(connection), C.READY)
        provider.script_evidence_failure("stale")
        self.assertIs(runtime.refresh_evidence(connection), C.DEGRADED)
        provider.script_evidence_failure("again")
        self.assertIs(runtime.refresh_evidence(connection), C.DEGRADED)

    def test_transport_loss_resolves_independently_of_provider_calls(self) -> None:
        """Section 6: unexpected transport loss -> disconnected, degraded or failed."""
        for outcome in (C.DISCONNECTED, C.DEGRADED, C.FAILED):
            runtime, provider, connection = connected_runtime()
            before = list(provider.calls)
            runtime.report_transport_loss(connection, outcome)
            self.assertIs(connection.state, outcome)
            self.assertEqual(provider.calls, before)
        with self.assertRaises(ValueError):
            runtime.report_transport_loss(connection, C.READY)

    def test_reads_require_an_active_connection(self) -> None:
        """Reads are served only for connected, ready or degraded Connections."""
        runtime, _ = make_runtime()
        connection = runtime.open_connection(runtime.discover().devices[0])
        for read in (runtime.read_telemetry, runtime.describe_preview, runtime.capability_report):
            with self.assertRaises(ConnectionNotActive):
                read(connection)

    def test_connection_history_records_transitions_with_timestamps(self) -> None:
        """Section 6 evidence column."""
        runtime, _, connection = connected_runtime()
        runtime.disconnect(connection)
        self.assertEqual(
            [(r.from_state, r.to_state) for r in connection.history],
            [
                ("new", "connecting"),
                ("connecting", "connected"),
                ("connected", "disconnecting"),
                ("disconnecting", "disconnected"),
            ],
        )

    def test_provider_event_enum_is_unaffected_by_connection_events(self) -> None:
        """REQ-004, REQ-005: the two lifecycles do not share events."""
        self.assertTrue({e.value for e in ProviderEvent}.isdisjoint({e.value for e in ConnectionEvent}))


class ConnectionCommandIndependenceTests(unittest.TestCase):
    """REQ-011: Connection state and Command outcome are modeled independently.

    DB-01 can verify the structural half: the two models share no type, field,
    event or signature. The dynamic half (a Connection loss leaving an active
    Command in ``unknown_result``) needs Command execution and is verified in DB-04.
    """

    def test_connection_model_holds_no_command_reference(self) -> None:
        self.assertTrue(all("command" not in slot for slot in Connection.__slots__))
        self.assertTrue(all("command" not in event.value for event in V))
        self.assertTrue(all("command" not in event.value for _state, event in CONNECTION_TRANSITIONS))
        self.assertEqual(
            set(TransitionRecord.__dataclass_fields__),
            {"subject_id", "from_state", "event", "to_state", "at", "evidence"},
        )

    def test_runtime_operations_never_accept_or_return_command_types(self) -> None:
        import inspect

        runtime, _ = make_runtime()
        for name in dir(runtime):
            if name.startswith("_"):
                continue
            member = getattr(runtime, name)
            if not callable(member):
                continue
            signature = inspect.signature(member)
            text = " ".join(
                [str(p.annotation) for p in signature.parameters.values()] + [str(signature.return_annotation)]
            ).lower()
            self.assertNotIn("command", text, name)

    def test_command_identity_is_unaffected_by_every_connection_outcome(self) -> None:
        """A CommandRef bound to a Connection is immutable evidence and never alters that Connection."""
        for outcome in (C.DISCONNECTED, C.DEGRADED, C.FAILED):
            runtime, _, connection = connected_runtime()
            ref = CommandRef("cmd-1", runtime.provider_id, connection.connection_id)
            snapshot = (ref.command_id, ref.provider_id, ref.connection_id)
            runtime.report_transport_loss(connection, outcome)
            self.assertIs(connection.state, outcome)
            self.assertEqual((ref.command_id, ref.provider_id, ref.connection_id), snapshot)
            history = [value for record in connection.history for value in vars_of(record)]
            self.assertNotIn("cmd-1", history)
            with self.assertRaises(AttributeError):
                ref.command_id = "other"  # type: ignore[misc]

    def test_connection_state_values_exclude_command_outcomes(self) -> None:
        shared = {s.value for s in ConnectionState} & {s.value for s in CommandState}
        self.assertEqual(shared, {"failed"})  # same word, distinct non-comparable types
        self.assertNotEqual(ConnectionState.FAILED, CommandState.FAILED)


def vars_of(record) -> list[str]:
    return [str(getattr(record, name)) for name in record.__dataclass_fields__]


class CommandTypeTests(unittest.TestCase):
    """DB-01 defines Command identity and state types only (REQ-020, REQ-044 type level)."""

    def test_command_ref_binds_command_provider_and_connection(self) -> None:
        ref = CommandRef("cmd-1", "sim-provider", "conn-0001")
        self.assertEqual((ref.command_id, ref.provider_id, ref.connection_id), ("cmd-1", "sim-provider", "conn-0001"))
        with self.assertRaises(ValueError):
            CommandRef("", "p", "c")

    def test_command_states_match_the_contract_and_terminal_set(self) -> None:
        self.assertEqual(
            {s.value for s in CommandState},
            {
                "requested", "validated", "rejected", "safety_blocked", "submitted", "acknowledged",
                "in_progress", "succeeded", "failed", "timed_out", "cancelled", "unknown_result",
            },
        )
        self.assertEqual(
            {s.value for s in COMMAND_TERMINAL_STATES},
            {"rejected", "safety_blocked", "succeeded", "failed", "timed_out", "cancelled", "unknown_result"},
        )
        for state in (CommandState.REQUESTED, CommandState.VALIDATED, CommandState.SUBMITTED,
                      CommandState.ACKNOWLEDGED, CommandState.IN_PROGRESS):
            self.assertFalse(state.is_terminal)

    def test_no_command_execution_surface_exists(self) -> None:
        """DB-04 boundary: no executor, submission, safety gate or exclusivity API in DB-01."""
        runtime, provider = make_runtime()
        banned = ("submit", "execute", "command", "cancel", "safety", "retry", "slew", "park")
        for owner in (runtime, provider):
            public = [n for n in dir(owner) if not n.startswith("_")]
            self.assertEqual([n for n in public if any(b in n.lower() for b in banned)], [], owner)
        self.assertIsInstance(runtime.connections, tuple)
        self.assertNotIn("active_command", Connection.__slots__)


if __name__ == "__main__":
    unittest.main()
