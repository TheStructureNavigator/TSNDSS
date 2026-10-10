"""DB-05 Slice 1: the Seestar command adapter and control transport, exercised only against scripted fakes."""

from __future__ import annotations

import ast
import copy
import json
import re
import socket
import sys
import unittest
from datetime import timedelta
from pathlib import Path

from tsn_dss.engine import seestar_control
from tsn_dss.engine.device_runtime import ManualClock, ProviderConnectionError, ProviderRuntime, SequentialIdGenerator
from tsn_dss.engine.device_runtime.command_executor import CommandExecutor, CommandIntent
from tsn_dss.engine.device_runtime.command_lifecycle import CommandEvent
from tsn_dss.engine.device_runtime.command_models import CommandKindRegistry
from tsn_dss.engine.device_runtime.errors import InvalidTransition, ProviderCommandRejected
from tsn_dss.engine.device_runtime.models import CommandState as S, ConnectionState as C
from tsn_dss.engine.device_runtime.provider import (
    CommandCapableProvider,
    DeviceProvider,
    ProviderCancelOutcome,
    ProviderCommandStatus as PS,
)
from tsn_dss.engine.device_runtime.safety_gates import UncertaintyState
from tsn_dss.engine.seestar_control import (
    ARM_DEPLOY,
    ARM_PARK,
    COMMANDS,
    SCENERY_PARAMS,
    SCENERY_START,
    SCENERY_STOP,
    ControlFreshness,
    ControlPostSendError,
    ControlPreSendError,
    SeestarCommandProvider,
    SeestarControlTransport,
    build_command_kinds,
    register_command_kinds,
)
from tsn_dss.engine.seestar_control.commands import wire_message
from tsn_dss.engine.seestar_provider import SeestarProvider
from tsn_dss.engine.seestar_provider.errors import SeestarConfigError, SeestarUnreachable
from tsn_dss.engine.seestar_provider.normalize import APP_ITEM_NAMES, DEVICE_ITEM_NAMES, STOPPED_STATES
from tsn_dss.engine.seestar_provider.protocol import RpcReply

try:
    from seestar_support import HOST, ScriptedDevice, ScriptedSocket, StubAuthenticator, FakeSeestarTransport, load_fixture, make_config
    from test_device_command_submission import Allow, establish_baselines
except ImportError:  # pragma: no cover
    from tests.seestar_support import HOST, ScriptedDevice, ScriptedSocket, StubAuthenticator, FakeSeestarTransport, load_fixture, make_config
    from tests.test_device_command_submission import Allow, establish_baselines

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "tsn_dss" / "engine" / "seestar_control"
SOURCES = sorted(PACKAGE.glob("*.py"))
METHODS = {"scope_move_to_horizon", "iscope_start_view", "iscope_stop_view", "scope_park"}
FIVE_MINUTES = timedelta(minutes=5)
FRESH = ControlFreshness(telemetry_max_age=FIVE_MINUTES, capability_max_age=FIVE_MINUTES)  # test values, not module defaults


# --- wire-level fake: the scripted device plus the four commands -------------------------------------------------------------


class _ControlSocket(ScriptedSocket):
    def sendall(self, payload: bytes) -> None:
        if self.device.fail_command_send is not None and any(m.encode() in payload for m in METHODS):
            raise self.device.fail_command_send
        super().sendall(payload)


class ControlDevice(ScriptedDevice):
    def __init__(self) -> None:
        super().__init__(auth="required")
        self.command_reply: dict | None = {"code": 0, "result": 0}
        self.fail_command_send: Exception | None = None
        self.events_after_command = 0

    def connect(self, address, timeout):
        self.connect_addresses.append(address)
        if self.connect_errors:
            raise self.connect_errors.popleft()
        sock = _ControlSocket(self)
        self.sockets.append(sock)
        self._verified = False
        return sock

    def handle(self, message):
        if message.get("method") in METHODS:
            if not self._verified:
                return []
            frames = [{"Event": "Noise"} for _ in range(self.events_after_command)]
            if self.command_reply is not None:
                frames.append(dict(self.command_reply, id=message["id"]))
            return frames
        return super().handle(message)

    def commands(self) -> list[dict]:
        return [m for s in self.sockets for m in s.received if m.get("method") in METHODS]


def control_transport(device: ControlDevice, **kwargs) -> SeestarControlTransport:
    return SeestarControlTransport(make_config(), StubAuthenticator(), connect_factory=device.connect, **kwargs)


# --- provider-level fakes -------------------------------------------------------------------------------------------------------


class StubControl:
    """Stands in for the control transport: scripted outcomes, records every call. It has no other methods."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.outcome: object = RpcReply("", 0, 0, None)

    def send_command(self, host, kind_id):
        self.calls.append((host, kind_id))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def set_mount(read: FakeSeestarTransport, *, close=True, move="none") -> None:
    mount = read.state_reply["result"]["mount"]
    mount["close"], mount["move_type"] = close, move


def set_cameras(read: FakeSeestarTransport, which: str) -> None:
    read.app_reply = load_fixture("app_state_scenery_ready.json" if which == "ready" else "app_state_idle.json")


def make(control=None, read=None, *, baseline=True):
    read = read or FakeSeestarTransport()
    set_mount(read)
    set_cameras(read, "idle")
    control = control or StubControl()
    clock = ManualClock()
    provider = SeestarCommandProvider(make_config(), read, control, clock=clock)
    runtime = ProviderRuntime(provider, clock=clock, id_generator=SequentialIdGenerator())
    devices = runtime.discover().devices
    connection = runtime.connect(runtime.open_connection(devices[0]))
    runtime.refresh_evidence(connection)
    registry = CommandKindRegistry()
    register_command_kinds(registry, FRESH)
    executor = CommandExecutor(
        runtime, registry, Allow(), clock=clock, id_generator=SequentialIdGenerator(),
        uncertainty=Established(), command_provider=provider,
    )
    if baseline:
        establish_baselines(executor, runtime, devices)
    return provider, runtime, executor, connection, read, control, clock


class Established:
    def state_for(self, provider_id, device_ref):
        return UncertaintyState.RESOLVED_BY_RECOVERY_EVIDENCE


def admit(executor, connection, kind):
    return executor.admit(CommandIntent(connection, kind, "operator"))


# =====================================================================================================================


class AllowListTests(unittest.TestCase):
    def test_exactly_four_commands_with_the_expected_wire_methods(self) -> None:
        self.assertEqual(set(COMMANDS), {ARM_DEPLOY, SCENERY_START, SCENERY_STOP, ARM_PARK})
        self.assertEqual({c.method for c in COMMANDS.values()}, METHODS)
        self.assertEqual({k: c.method for k, c in COMMANDS.items()}, {
            ARM_DEPLOY: "scope_move_to_horizon", SCENERY_START: "iscope_start_view",
            SCENERY_STOP: "iscope_stop_view", ARM_PARK: "scope_park"})

    def test_parameters_are_fixed_exactly(self) -> None:
        messages = {k: wire_message(c, 7) for k, c in COMMANDS.items()}
        for kind in (ARM_DEPLOY, SCENERY_STOP, ARM_PARK):
            self.assertEqual(messages[kind], {"id": 7, "verify": True, "method": COMMANDS[kind].method})
        self.assertEqual(messages[SCENERY_START], {
            "id": 7, "verify": True, "method": "iscope_start_view",
            "params": {"mode": "scenery", "target_ra_dec": [None, None], "target_name": "Unknown", "lp_filter": False, "cam_id": 1},
        })
        json.dumps(messages[SCENERY_START])  # serializable as is
        self.assertEqual(SCENERY_PARAMS["cam_id"], 1)

    def test_the_tables_cannot_be_changed_and_messages_are_copies(self) -> None:
        with self.assertRaises(TypeError):
            COMMANDS["x"] = None  # type: ignore[index]
        with self.assertRaises(TypeError):
            SCENERY_PARAMS["mode"] = "star"  # type: ignore[index]
        with self.assertRaises(Exception):
            COMMANDS[ARM_PARK].method = "scope_goto"  # type: ignore[misc]
        message = wire_message(COMMANDS[SCENERY_START], 1)
        message["params"]["mode"] = "star"
        message["params"]["target_ra_dec"].append(1)
        self.assertEqual(wire_message(COMMANDS[SCENERY_START], 1)["params"]["mode"], "scenery")
        self.assertEqual(wire_message(COMMANDS[SCENERY_START], 1)["params"]["target_ra_dec"], [None, None])

    def test_the_transport_sends_only_allow_listed_kinds_and_touches_nothing_otherwise(self) -> None:
        device = ControlDevice()
        transport = control_transport(device)
        for bad in ("get_device_state", "scope_goto", "scope_park", "iscope_start_view", "seestar.arm.goto", "", None, 5, "SEESTAR.ARM.PARK"):
            with self.assertRaises(ControlPreSendError, msg=repr(bad)):
                transport.send_command(HOST, bad)  # type: ignore[arg-type]
        self.assertEqual(device.connect_addresses, [])

    def test_there_is_no_generic_send_and_no_caller_parameters(self) -> None:
        import inspect

        public = {n for n in dir(SeestarControlTransport) if not n.startswith("_")}
        self.assertEqual(public, {"send_command", "read_device_state", "read_app_state", "test_connection", "read_equ_coord", "discover_via_udp"})
        self.assertEqual(list(inspect.signature(SeestarControlTransport.send_command).parameters), ["self", "host", "kind_id"])
        for cls in (SeestarCommandProvider, SeestarControlTransport):
            names = {n.lower() for n in dir(cls) if not n.startswith("_")}
            self.assertFalse([n for n in names if n in ("send", "call", "rpc", "execute", "raw", "send_raw", "request")], cls)


class PolicyTests(unittest.TestCase):
    def policies(self):
        return {p.kind_id: p for p in build_command_kinds(FRESH)}

    def requirements(self, kind):
        return [(r.item, r.allowed_values) for r in self.policies()[kind].freshness]

    def test_all_four_are_state_changing_physical_non_idempotent_and_safety_sensitive(self) -> None:
        self.assertEqual(set(self.policies()), set(COMMANDS))
        for policy in self.policies().values():
            self.assertEqual((policy.state_changing, policy.physical, policy.idempotent, policy.safety_sensitive),
                             (True, True, False, True), policy.kind_id)

    def test_required_values(self) -> None:
        stopped = tuple(STOPPED_STATES)
        cameras = [(f"app.{c}.{f}", stopped) for c in ("main", "wide") for f in ("state", "rtsp_state")]
        cap = lambda kind: (f"capability:{kind}", None)  # noqa: E731
        self.assertEqual(self.requirements(ARM_DEPLOY),
                         [("mount.move_type", ("none",)), ("mount.arm_closed", (True,)), cap(ARM_DEPLOY)])
        self.assertEqual(self.requirements(SCENERY_START),
                         [("mount.move_type", ("none",)), ("mount.arm_closed", (False,)), *cameras, cap(SCENERY_START)])
        self.assertEqual(self.requirements(SCENERY_STOP),
                         [(f"app.{c}.{f}", None) for c in ("main", "wide") for f in ("state", "rtsp_state")] + [cap(SCENERY_STOP)])
        self.assertEqual(self.requirements(ARM_PARK),
                         [("mount.move_type", ("none",)), ("mount.arm_closed", (False,)), *cameras, cap(ARM_PARK)])

    def test_the_windows_are_exactly_what_the_integrator_supplied(self) -> None:
        freshness = ControlFreshness(timedelta(seconds=7), timedelta(seconds=11))
        for policy in build_command_kinds(freshness):
            for requirement in policy.freshness:
                expected = timedelta(seconds=11) if requirement.item.startswith("capability:") else timedelta(seconds=7)
                self.assertEqual(requirement.max_age, expected, (policy.kind_id, requirement.item))

    def test_no_defaults_and_no_hard_coded_window_in_the_package(self) -> None:
        with self.assertRaises(TypeError):
            ControlFreshness()  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            ControlFreshness(FIVE_MINUTES)  # type: ignore[call-arg]
        for bad in (timedelta(0), timedelta(seconds=-1), 5, None):
            with self.assertRaises(ValueError):
                ControlFreshness(bad, FIVE_MINUTES)  # type: ignore[arg-type]
        for path in SOURCES:
            text = path.read_text(encoding="utf-8")
            self.assertEqual(re.findall(r"timedelta\(\s*[^0)]", text), [], path.name)
            self.assertNotRegex(text, r"seconds\s*=\s*\d|timeout\s*=\s*\d", path.name)

    def test_item_names_and_words_come_from_the_existing_provider(self) -> None:
        names = set(DEVICE_ITEM_NAMES) | set(APP_ITEM_NAMES)
        for policy in self.policies().values():
            for requirement in policy.freshness:
                if not requirement.item.startswith("capability:"):
                    self.assertIn(requirement.item, names, requirement.item)
        self.assertEqual(set(STOPPED_STATES), {"cancel", "complete", "idle"})

    def test_registration(self) -> None:
        registry = CommandKindRegistry()
        register_command_kinds(registry, FRESH)
        self.assertEqual(set(registry.kind_ids()), set(COMMANDS))


class TransportPhaseTests(unittest.TestCase):
    def test_a_command_goes_out_after_the_handshake_with_the_fixed_message(self) -> None:
        device = ControlDevice()
        reply = control_transport(device).send_command(HOST, SCENERY_START)
        self.assertEqual(reply.code, 0)
        self.assertEqual(device.methods, ["get_verify_str", "verify_client", "pi_is_verified", "iscope_start_view"])
        self.assertEqual(device.commands(), [wire_message(COMMANDS[SCENERY_START], device.commands()[0]["id"])])
        self.assertEqual(len(device.connect_addresses), 1)
        self.assertTrue(all(s.closed for s in device.sockets))

    def test_an_error_reply_is_returned_not_raised_and_says_nothing_about_effects(self) -> None:
        device = ControlDevice()
        device.command_reply = {"code": 7, "result": None}
        self.assertEqual(control_transport(device).send_command(HOST, ARM_PARK).code, 7)

    def test_failures_before_the_command_frame_are_pre_send(self) -> None:
        cases = {}
        d = ControlDevice(); d.connect_errors.append(ConnectionRefusedError()); cases["connect_failed"] = d
        d = ControlDevice(); d.connect_errors.append(socket.timeout()); cases["connect_timeout"] = d
        d = ControlDevice(); d.accept_signature = False; cases["auth_rejected"] = d
        d = ControlDevice(); d.fail_send = OSError("handshake send"); cases["connection_lost"] = d
        d = ControlDevice(); d.recv_error = OSError("handshake recv"); cases["connection_lost"] = d
        for expected, device in cases.items():
            with self.assertRaises(ControlPreSendError, msg=expected) as ctx:
                control_transport(device).send_command(HOST, ARM_DEPLOY)
            self.assertEqual(ctx.exception.category, expected)
            self.assertEqual(device.commands(), [], expected)  # the command frame never reached the device
            self.assertLessEqual(len(device.connect_addresses), 1)

    def test_a_handshake_that_never_answers_is_pre_send(self) -> None:
        device = ControlDevice()
        device.close_when_empty = True
        device.auth = "absent"  # answers code 103 then nothing further is needed; force silence instead
        device.handle = lambda message: []  # type: ignore[method-assign]
        with self.assertRaises(ControlPreSendError):
            control_transport(device, monotonic=_ticking(0.4)).send_command(HOST, ARM_DEPLOY)
        self.assertEqual(device.commands(), [])

    def test_failures_from_the_moment_the_command_frame_is_sent_are_post_send(self) -> None:
        # the write itself fails
        d = ControlDevice(); d.fail_command_send = OSError("broken pipe")
        with self.assertRaises(ControlPostSendError) as ctx:
            control_transport(d).send_command(HOST, ARM_PARK)
        self.assertEqual(ctx.exception.category, "connection_lost")
        self.assertEqual(d.commands(), [])  # whether the bytes left is exactly what cannot be known: still post-send
        # the write times out
        d = ControlDevice(); d.fail_command_send = socket.timeout()
        with self.assertRaises(ControlPostSendError):
            control_transport(d).send_command(HOST, ARM_PARK)
        # no reply
        d = ControlDevice(); d.command_reply = None
        with self.assertRaises(ControlPostSendError) as ctx:
            control_transport(d, monotonic=_ticking(0.4)).send_command(HOST, ARM_PARK)
        self.assertEqual(ctx.exception.category, "read_timeout")
        self.assertEqual(len(d.commands()), 1)
        # closed connection
        d = ControlDevice(); d.command_reply = None; d.close_when_empty = True
        with self.assertRaises(ControlPostSendError) as ctx:
            control_transport(d).send_command(HOST, ARM_PARK)
        self.assertEqual(ctx.exception.category, "connection_closed")
        # too many interleaved events
        d = ControlDevice(); d.command_reply = None; d.events_after_command = 600
        with self.assertRaises(ControlPostSendError) as ctx:
            control_transport(d).send_command(HOST, ARM_PARK)
        self.assertEqual(ctx.exception.category, "too_many_events")

    def test_an_untyped_internal_error_is_classified_by_where_it_happened(self) -> None:
        d = ControlDevice()
        transport = control_transport(d)
        transport._send = lambda sock, payload: (_ for _ in ()).throw(RuntimeError("bug"))  # type: ignore[method-assign]
        with self.assertRaises(ControlPreSendError) as ctx:  # the first write is the handshake: before the command frame
            transport.send_command(HOST, ARM_PARK)
        self.assertEqual(ctx.exception.category, "control_failed")

    def test_each_call_uses_one_connection_and_failures_are_never_retried(self) -> None:
        for build in (lambda d: setattr(d, "command_reply", None), lambda d: setattr(d, "accept_signature", False)):
            d = ControlDevice()
            build(d)
            with self.assertRaises(Exception):
                control_transport(d, monotonic=_ticking(0.4)).send_command(HOST, ARM_PARK)
            self.assertEqual(len(d.connect_addresses), 1)
            self.assertLessEqual(len(d.commands()), 1)

    def test_authentication_is_mandatory_and_nothing_secret_is_exposed(self) -> None:
        with self.assertRaises(SeestarConfigError):
            SeestarControlTransport(make_config(), None)  # type: ignore[arg-type]
        text = repr(control_transport(ControlDevice()))
        for secret in (HOST, "operator-supplied.pem"):
            self.assertNotIn(secret, text)

    def test_message_ids_do_not_collide_with_the_handshake(self) -> None:
        d = ControlDevice()
        transport = control_transport(d)
        transport.send_command(HOST, ARM_DEPLOY)
        transport.send_command(HOST, SCENERY_STOP)
        ids = [m["id"] for m in d.commands()]
        self.assertEqual(len(set(ids)), 2)
        self.assertTrue(all(i > 1003 for i in ids))


def _ticking(step: float):
    state = {"now": 0.0}

    def clock() -> float:
        state["now"] += step
        return state["now"]

    return clock


class ProviderContractTests(unittest.TestCase):
    def test_it_satisfies_both_provider_protocols_and_the_read_only_provider_is_unchanged(self) -> None:
        provider = make()[0]
        self.assertIsInstance(provider, CommandCapableProvider)
        self.assertIsInstance(provider, DeviceProvider)
        self.assertIsInstance(provider, SeestarProvider)
        names = {n for n in dir(SeestarProvider) if not n.startswith("_")}
        self.assertFalse(names & {"submit_command", "poll_command", "cancel_command"})
        self.assertNotIsInstance(SeestarProvider(make_config(), FakeSeestarTransport()), CommandCapableProvider)

    def test_capabilities_name_the_four_commands_and_replace_the_generic_unsupported_entries(self) -> None:
        provider, runtime, _, connection, *_ = make()
        report = runtime.capability_report(connection)
        entries = {e.name: e for e in report.entries}
        for kind in COMMANDS:
            e = entries[kind]
            self.assertEqual((e.supported_by_provider, e.available_now, e.requires_safety_gate, e.simulated), (True, True, True, False))
            self.assertEqual(e.confirmation.value, "implemented_untested")
        self.assertNotIn("mount.motion", entries)
        self.assertNotIn("camera.mode_control", entries)
        base = {e.name for e in SeestarProvider(make_config(), FakeSeestarTransport()).describe_capabilities(None)}
        self.assertIn("mount.motion", base)
        self.assertEqual(provider.descriptor.implementation_label, "tsn-dss-seestar-control/1")
        before = {e.name: e.available_now for e in provider.describe_capabilities(None)}
        self.assertTrue(all(before[k] is None for k in COMMANDS))

    def test_cancellation_is_refused_and_contacts_nothing(self) -> None:
        provider, _, _, _, read, control, _ = make()
        calls = list(read.calls)
        result = provider.cancel_command("conn-0001", "cmd-0001")
        self.assertIs(result.outcome, ProviderCancelOutcome.REFUSED)
        self.assertEqual(result.evidence, "")
        self.assertEqual((read.calls, control.calls), (calls, []))


class SubmissionClassificationTests(unittest.TestCase):
    def submit(self, provider, connection, command_id="cmd-1", kind=ARM_DEPLOY, key=None):
        return provider.submit_command(connection.connection_id, command_id, kind, key)

    def test_structural_pre_send_refusals_say_no_effect_and_never_reach_the_control_channel(self) -> None:
        provider, _, _, connection, read, control, _ = make()
        for label, call in {
            "kind_not_supported": lambda: self.submit(provider, connection, kind="seestar.arm.goto"),
            "idempotency_key_not_supported": lambda: self.submit(provider, connection, key="k"),
            "not_connected": lambda: provider.submit_command("conn-nope", "cmd-2", ARM_DEPLOY, None),
        }.items():
            with self.assertRaises(ProviderCommandRejected, msg=label) as ctx:
                call()
            self.assertEqual((ctx.exception.category, ctx.exception.effect_possible), (label, False))
        self.assertEqual(control.calls, [])

    def test_a_failed_or_mismatched_identity_check_is_pre_send(self) -> None:
        provider, _, _, connection, read, control, _ = make()
        read.fail_next("device_state", SeestarUnreachable("connect_failed"))
        with self.assertRaises(ProviderCommandRejected) as ctx:
            self.submit(provider, connection)
        self.assertFalse(ctx.exception.effect_possible)
        self.assertTrue(ctx.exception.category.startswith("identity_check_failed:"))
        other = copy.deepcopy(read.state_reply)
        other["result"]["device"]["sn"] = "OTHER-SERIAL"
        read.state_reply = other
        with self.assertRaises(ProviderCommandRejected) as ctx:
            self.submit(provider, connection, "cmd-2")
        self.assertFalse(ctx.exception.effect_possible)
        self.assertEqual(control.calls, [])

    def test_pre_send_transport_failure_says_no_effect(self) -> None:
        provider, _, _, connection, _, control, _ = make()
        control.outcome = ControlPreSendError("auth_rejected")
        with self.assertRaises(ProviderCommandRejected) as ctx:
            self.submit(provider, connection)
        self.assertEqual((ctx.exception.category, ctx.exception.effect_possible), ("not_sent:auth_rejected", False))

    def test_post_send_failures_are_transport_loss_with_the_effect_unknown_never_a_no_effect_claim(self) -> None:
        for outcome in (ControlPostSendError("read_timeout"), ControlPostSendError("connection_lost"),
                        ControlPostSendError("connection_closed"), RuntimeError("untyped"), ValueError("untyped")):
            provider, _, _, connection, _, control, _ = make()
            control.outcome = outcome
            with self.assertRaises(ProviderConnectionError) as ctx:
                self.submit(provider, connection)
            self.assertNotIsInstance(ctx.exception, ProviderCommandRejected)
            self.assertEqual(len(control.calls), 1)

    def test_a_device_reply_that_is_not_a_clean_acceptance_keeps_the_effect_possible(self) -> None:
        replies = [RpcReply("", 1, None, None), RpcReply("", 103, 0, None), RpcReply("", None, 0, None),
                   RpcReply("", -1, "err", None)]
        for reply in replies:
            provider, _, _, connection, _, control, _ = make()
            control.outcome = reply
            with self.assertRaises(ProviderCommandRejected) as ctx:
                self.submit(provider, connection)
            self.assertIs(ctx.exception.effect_possible, True, reply)
        for bad_result in (1, None, True, "0", 0.5):
            provider, _, _, connection, _, control, _ = make()
            control.outcome = RpcReply("", 0, bad_result, None)
            with self.assertRaises(ProviderCommandRejected) as ctx:
                self.submit(provider, connection, kind=SCENERY_START)
            self.assertIs(ctx.exception.effect_possible, True, bad_result)

    def test_acceptance(self) -> None:
        provider, _, _, connection, _, control, _ = make()
        control.outcome = RpcReply("", 0, 0, None)
        receipt = self.submit(provider, connection, kind=SCENERY_START)
        self.assertIsNotNone(receipt.accepted_at)
        provider2, _, _, connection2, _, control2, _ = make()
        control2.outcome = RpcReply("", 0, "anything", None)  # only the start request has a result requirement
        self.submit(provider2, connection2, kind=ARM_PARK)
        self.assertEqual(control2.calls, [(HOST, ARM_PARK)])

    def test_a_command_id_is_never_submitted_twice_even_after_a_failure(self) -> None:
        provider, _, _, connection, _, control, _ = make()
        control.outcome = ControlPostSendError("read_timeout")
        with self.assertRaises(ProviderConnectionError):
            self.submit(provider, connection)
        for _ in range(2):
            with self.assertRaises(ProviderCommandRejected) as ctx:
                self.submit(provider, connection)
            self.assertEqual((ctx.exception.category, ctx.exception.effect_possible), ("duplicate_command", False))
        self.assertEqual(len(control.calls), 1)

    def test_the_exact_kind_reaches_the_control_channel_and_nothing_else_is_forwarded(self) -> None:
        for kind in COMMANDS:
            provider, _, _, connection, _, control, _ = make()
            self.submit(provider, connection, kind=kind)
            self.assertEqual(control.calls, [(HOST, kind)])


class PollTests(unittest.TestCase):
    def poll(self, provider, connection, kind):
        provider.submit_command(connection.connection_id, "cmd-1", kind, None)
        return provider.poll_command(connection.connection_id, "cmd-1").status

    def test_arm_commands(self) -> None:
        for kind, start_closed in ((ARM_DEPLOY, True), (ARM_PARK, False)):
            provider, _, _, connection, read, *_ = make()
            set_mount(read, close=start_closed)
            self.assertIs(self.poll(provider, connection, kind), PS.ACKNOWLEDGED, kind)  # nothing visible yet
            set_mount(read, close=start_closed, move="manual")
            self.assertIs(provider.poll_command(connection.connection_id, "cmd-1").status, PS.IN_PROGRESS)
            set_mount(read, close=not start_closed, move="manual")
            self.assertIs(provider.poll_command(connection.connection_id, "cmd-1").status, PS.IN_PROGRESS)
            set_mount(read, close=not start_closed)
            self.assertIs(provider.poll_command(connection.connection_id, "cmd-1").status, PS.REPORTED_COMPLETE)
            set_mount(read, close=start_closed)
            self.assertIs(provider.poll_command(connection.connection_id, "cmd-1").status, PS.ACKNOWLEDGED)

    def test_scenery_start_and_stop(self) -> None:
        provider, _, _, connection, read, *_ = make()
        set_mount(read, close=False)
        self.assertIs(self.poll(provider, connection, SCENERY_START), PS.ACKNOWLEDGED)
        mixed = load_fixture("app_state_idle.json")
        mixed["result"]["View"]["state"] = "working"
        read.app_reply = mixed
        self.assertIs(provider.poll_command(connection.connection_id, "cmd-1").status, PS.IN_PROGRESS)
        set_cameras(read, "ready")
        self.assertIs(provider.poll_command(connection.connection_id, "cmd-1").status, PS.REPORTED_COMPLETE)
        provider2, _, _, connection2, read2, *_ = make()
        set_cameras(read2, "ready")
        self.assertIs(self.poll(provider2, connection2, SCENERY_STOP), PS.ACKNOWLEDGED)
        read2.app_reply = mixed
        self.assertIs(provider2.poll_command(connection2.connection_id, "cmd-1").status, PS.IN_PROGRESS)
        set_cameras(read2, "idle")
        self.assertIs(provider2.poll_command(connection2.connection_id, "cmd-1").status, PS.REPORTED_COMPLETE)

    def test_unknown_values_never_claim_progress_or_completion(self) -> None:
        provider, _, _, connection, read, *_ = make()
        read.state_reply["result"]["mount"]["move_type"] = None
        read.state_reply["result"]["mount"]["close"] = None
        self.assertIs(self.poll(provider, connection, ARM_DEPLOY), PS.ACKNOWLEDGED)
        read.state_reply["result"].pop("mount")
        self.assertIs(provider.poll_command(connection.connection_id, "cmd-1").status, PS.ACKNOWLEDGED)
        # the target position alone is not completion while the movement state is unknown, and vice versa
        read.state_reply["result"]["mount"] = {"move_type": None, "close": False}
        self.assertIs(provider.poll_command(connection.connection_id, "cmd-1").status, PS.ACKNOWLEDGED)
        read.state_reply["result"]["mount"] = {"move_type": "none", "close": None}
        self.assertIs(provider.poll_command(connection.connection_id, "cmd-1").status, PS.ACKNOWLEDGED)

    def test_a_failure_is_never_reported_and_a_poll_command_sends_nothing(self) -> None:
        for kind in COMMANDS:
            provider, _, _, connection, read, control, _ = make()
            provider.submit_command(connection.connection_id, "cmd-1", kind, None)
            sent = list(control.calls)
            for mount in ({"close": True, "move_type": "none"}, {"close": False, "move_type": "none"}, {"close": None, "move_type": "x"}):
                read.state_reply["result"]["mount"].update(mount)
                for app in ("idle", "ready"):
                    set_cameras(read, app)
                    status = provider.poll_command(connection.connection_id, "cmd-1").status
                    self.assertIsNot(status, PS.REPORTED_FAILED)
            self.assertEqual(control.calls, sent)

    def test_unknown_commands_and_unreadable_devices(self) -> None:
        provider, _, _, connection, read, *_ = make()
        with self.assertRaises(ProviderConnectionError):
            provider.poll_command(connection.connection_id, "cmd-never")
        provider.submit_command(connection.connection_id, "cmd-1", ARM_DEPLOY, None)
        with self.assertRaises(ProviderConnectionError):
            provider.poll_command("conn-other", "cmd-1")
        read.fail_next("device_state", SeestarUnreachable("connect_failed"), times=4)
        read.fail_next("app_state", SeestarUnreachable("connect_failed"), times=4)
        with self.assertRaises(ProviderConnectionError):
            provider.poll_command(connection.connection_id, "cmd-1")


class ExecutorIntegrationTests(unittest.TestCase):
    """The adapter behind the real DB-04 executor, still against fakes only."""

    def blocked(self, executor, connection, kind, fragment="state_unsafe"):
        record = admit(executor, connection, kind)
        self.assertIs(record.state, S.SAFETY_BLOCKED, kind)
        self.assertIn(fragment, record.history[-1].evidence)
        self.assertIs(connection.state, C.READY)
        return record

    def test_value_constraints_gate_every_command(self) -> None:
        provider, runtime, executor, connection, read, control, _ = make()
        # folded arm, cameras idle: only deploy may start; start/park need an open arm, stop needs nothing but known states
        self.assertIs(admit(executor, connection, ARM_DEPLOY).state, S.VALIDATED)
        executor.check_deadline  # noqa: B018  (surface untouched)
        provider2, runtime2, executor2, connection2, read2, *_ = make()
        for kind in (SCENERY_START, ARM_PARK):
            self.blocked(executor2, connection2, kind, "state_unsafe:mount.arm_closed")
        set_mount(read2, close=False, move="manual")
        for kind in (ARM_DEPLOY, SCENERY_START, ARM_PARK):
            self.blocked(executor2, connection2, kind, "state_unsafe:mount.move_type")
        set_mount(read2, close=False)
        self.blocked(executor2, connection2, ARM_DEPLOY, "state_unsafe:mount.arm_closed")
        set_cameras(read2, "ready")
        for kind in (SCENERY_START, ARM_PARK):
            self.blocked(executor2, connection2, kind, "state_unsafe:app.main.state")
        self.assertIs(admit(executor2, connection2, SCENERY_STOP).state, S.VALIDATED)

    def test_unknown_stale_or_missing_evidence_blocks_with_its_own_reason(self) -> None:
        provider, runtime, executor, connection, read, *_ = make()
        read.state_reply["result"]["mount"]["move_type"] = None
        self.blocked(executor, connection, ARM_DEPLOY, "state_unknown:mount.move_type")
        read.state_reply["result"]["mount"].pop("move_type")
        self.blocked(executor, connection, ARM_DEPLOY, "state_unavailable:mount.move_type")

    def test_the_gate_is_applied_again_right_before_submission(self) -> None:
        provider, runtime, executor, connection, read, control, _ = make()
        record = admit(executor, connection, ARM_DEPLOY)
        self.assertIs(record.state, S.VALIDATED)
        set_mount(read, close=False)  # the arm opened by other means between admission and submission
        executor.submit(record.command_id)
        self.assertIs(record.state, S.SAFETY_BLOCKED)
        self.assertEqual(control.calls, [])

    def test_a_clean_submission_is_only_an_acknowledgement(self) -> None:
        provider, runtime, executor, connection, read, control, _ = make()
        record = admit(executor, connection, ARM_DEPLOY)
        executor.submit(record.command_id)
        self.assertIs(record.state, S.ACKNOWLEDGED)
        self.assertEqual(control.calls, [(HOST, ARM_DEPLOY)])
        set_mount(read, close=False)
        executor.poll(record.command_id)
        self.assertIs(record.state, S.ACKNOWLEDGED)  # provider-reported completion; no EffectVerifier yet, so never success
        self.assertIs(connection.state, C.BUSY)

    def test_pre_send_failure_is_failed_and_leaves_no_uncertainty(self) -> None:
        provider, runtime, executor, connection, read, control, _ = make()
        control.outcome = ControlPreSendError("connect_failed")
        record = admit(executor, connection, ARM_DEPLOY)
        executor.submit(record.command_id)
        self.assertIs(record.state, S.FAILED)
        self.assertEqual(executor.unresolved_devices(), frozenset())
        self.assertIs(connection.state, C.READY)

    def test_every_other_failure_is_unknown_result_with_uncertainty(self) -> None:
        outcomes = {
            "post_send": ControlPostSendError("read_timeout"),
            "device_error": RpcReply("", 1, None, None),
            "untyped": RuntimeError("bug"),
        }
        for label, outcome in outcomes.items():
            provider, runtime, executor, connection, read, control, _ = make()
            control.outcome = outcome
            record = admit(executor, connection, ARM_PARK if label == "device_error" else ARM_DEPLOY)
            if record.state is S.SAFETY_BLOCKED:  # the park needs an open arm and stopped cameras
                set_mount(read, close=False)
                record = admit(executor, connection, ARM_PARK)
            executor.submit(record.command_id)
            self.assertIs(record.state, S.UNKNOWN_RESULT, label)
            self.assertEqual(len(executor.unresolved_devices()), 1, label)
            self.assertEqual(len(control.calls), 1, label)

    def test_no_retry_and_later_safety_sensitive_commands_stay_blocked(self) -> None:
        provider, runtime, executor, connection, read, control, _ = make()
        control.outcome = ControlPostSendError("connection_closed")
        record = admit(executor, connection, ARM_DEPLOY)
        executor.submit(record.command_id)
        self.assertIs(record.state, S.UNKNOWN_RESULT)
        with self.assertRaises(InvalidTransition):
            executor.submit(record.command_id)
        again = runtime.connect(runtime.open_connection(runtime.discover().devices[0]))
        runtime.refresh_evidence(again)
        blocked = admit(executor, again, SCENERY_STOP)
        self.assertIs(blocked.state, S.SAFETY_BLOCKED)
        self.assertEqual(blocked.history[-1].event, CommandEvent.UNCERTAINTY_NOT_CLEARED.value)
        self.assertEqual(len(control.calls), 1)

    def test_a_restarted_process_starts_with_unknown_history_and_sends_nothing(self) -> None:
        provider, runtime, executor, connection, read, control, _ = make(baseline=False)
        record = admit(executor, connection, ARM_DEPLOY)
        self.assertIs(record.state, S.SAFETY_BLOCKED)
        self.assertEqual(control.calls, [])

    def test_the_adapter_never_imports_or_uses_the_executor(self) -> None:
        text = (PACKAGE / "provider.py").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"(?m)^\s*(from|import)\s+.*command_executor")


class BoundaryTests(unittest.TestCase):
    def imports(self, path: Path):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    yield 0, alias.name
            elif isinstance(node, ast.ImportFrom):
                yield node.level, node.module or ""

    def test_only_stdlib_the_neutral_runtime_and_the_existing_seestar_packages(self) -> None:
        stdlib = set(sys.stdlib_module_names)
        modules = {p.stem for p in SOURCES}
        banned = {"socket", "subprocess", "multiprocessing", "ctypes", "asyncio", "sqlite3", "os", "pathlib", "shutil", "tempfile",
                  "http", "urllib", "pickle", "shelve", "dbm", "logging"}
        for path in SOURCES:
            for level, module in self.imports(path):
                top = module.split(".")[0]
                if level == 1:
                    self.assertIn(top or path.stem, modules | {""}, (path.name, module))
                elif level == 2:
                    self.assertIn(top, {"device_runtime", "seestar_provider", "seestar_preview"}, (path.name, module))
                    if top == "seestar_preview":
                        self.assertEqual((path.name, module), ("states.py", "seestar_preview.readiness"))
                else:
                    self.assertIn(top, stdlib, (path.name, module))
                    self.assertNotIn(top, banned, (path.name, module))

    def test_no_gpl_client_no_key_material_and_no_cryptography(self) -> None:
        for path in SOURCES:
            text = path.read_text(encoding="utf-8")
            for _level, module in self.imports(path):
                self.assertNotIn(module.split(".")[0], {"seestarpy", "cryptography"}, path.name)
            self.assertNotIn("BEGIN " + "RSA PRIVATE KEY", text)
            self.assertNotIn("GNU GENERAL PUBLIC LICENSE", text.upper())

    def test_the_wire_method_names_exist_in_one_module_of_the_engine(self) -> None:
        holders = set()
        for path in (ROOT / "tsn_dss").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if any(re.search(rf"[\"']{m}[\"']", text) for m in METHODS):
                holders.add(path.relative_to(ROOT).as_posix())
        self.assertEqual(holders, {"tsn_dss/engine/seestar_control/commands.py"})

    def test_the_neutral_runtime_and_the_read_only_packages_know_nothing_of_this_package(self) -> None:
        for folder in ("device_runtime", "seestar_provider", "seestar_preview", "opencv_isolated_decoder", "opencv_preview_decoder"):
            for path in (ROOT / "tsn_dss" / "engine" / folder).glob("*.py"):
                self.assertNotIn("seestar_control", path.read_text(encoding="utf-8"), path.name)
        for path in (ROOT / "tsn_dss" / "engine" / "device_runtime").glob("*.py"):
            self.assertNotIn("seestar", path.read_text(encoding="utf-8").lower(), path.name)

    def test_nothing_outside_the_executor_calls_a_providers_command_methods(self) -> None:
        pattern = re.compile(r"\.(submit_command|poll_command|cancel_command)\(")
        users = {p.relative_to(ROOT).as_posix() for base in ("tsn_dss", "tools") for p in (ROOT / base).rglob("*.py")
                 if pattern.search(p.read_text(encoding="utf-8"))}
        self.assertEqual(users, {"tsn_dss/engine/device_runtime/command_executor.py"})

    def test_importing_the_package_opens_no_connection_and_loads_no_third_party_code(self) -> None:
        import subprocess

        code = ("import sys, socket; sys.modules['socket'].socket = None; import tsn_dss.engine.seestar_control;"
                "bad=[m for m in sys.modules if m.split('.')[0] in ('cv2','numpy','cryptography','seestarpy')]; print(bad)")
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "[]")

    def test_the_package_surface(self) -> None:
        self.assertEqual(set(seestar_control.__all__), {
            "ARM_DEPLOY", "ARM_PARK", "ARM_PHRASE", "CLEAR_PHRASE", "COMMANDS", "CommandDriver", "CommandOutcome", "ControlAttachError",
            "ControlCommand", "ControlFreshness", "ControlHandle", "ControlPostSendError", "ControlPreSendError", "GRANT_PHRASE",
            "OperatorClearance", "OperatorPermit", "PendingRecovery", "PermitError", "SCENERY_PARAMS",
            "SCENERY_START",
            "SCENERY_STOP", "SeestarCommandProvider", "SeestarControl", "SeestarControlTransport", "build_command_kinds",
            "register_command_kinds"})

    def test_the_read_only_allow_list_is_untouched(self) -> None:
        from tsn_dss.engine.seestar_provider.protocol import READ_METHODS

        self.assertEqual(set(READ_METHODS), {"get_device_state", "iscope_get_app_state", "test_connection", "scope_get_equ_coord"})


if __name__ == "__main__":
    unittest.main()
