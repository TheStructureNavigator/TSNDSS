"""DB-02 wire protocol, read-only allow-list, authentication prerequisite, retries, UDP, config."""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import unittest
from unittest import mock

from tsn_dss.engine.seestar_provider import (
    READ_METHODS,
    RsaKeyFileAuthenticator,
    SeestarAuthError,
    SeestarConfigError,
    SeestarMethodNotAllowed,
    SeestarProtocolError,
    SeestarProviderConfig,
    SeestarTimeout,
    SeestarUnreachable,
    TcpSeestarTransport,
)
from tsn_dss.engine.seestar_provider.protocol import (
    ALLOWED_STATE_KEYS,
    FORBIDDEN_STATE_KEYS,
    MAX_FRAME_BYTES,
    SCAN_PROBE,
    encode_read_request,
    parse_scan_reply,
    split_frames,
)

try:
    from seestar_support import (
        HOST,
        KEY_PATH,
        ScriptedDevice,
        ScriptedUdpSocket,
        StepClock,
        StubAuthenticator,
        load_fixture,
        make_config,
        make_transport,
    )
except ModuleNotFoundError:  # pragma: no cover
    from tests.seestar_support import (
        HOST,
        KEY_PATH,
        ScriptedDevice,
        ScriptedUdpSocket,
        StepClock,
        StubAuthenticator,
        load_fixture,
        make_config,
        make_transport,
    )

BANNED_METHODS = (
    "scope_move_to_horizon", "scope_park", "scope_goto", "scope_speed_move", "scope_sync",
    "scope_set_track_state", "iscope_start_view", "iscope_stop_view", "iscope_start_stack",
    "pi_output_set2", "pi_reboot", "pi_shutdown", "move_focuser", "set_setting", "set_control_value",
    "start_auto_focuse", "start_solve", "start_polar_align", "play_sound", "send_command",
    "random_command", "begin_streaming", "get_verify_str", "verify_client", "pi_is_verified",
)


class AllowListTests(unittest.TestCase):
    def test_allow_list_is_exactly_three_idempotent_reads(self) -> None:
        self.assertEqual(set(READ_METHODS), {"get_device_state", "iscope_get_app_state", "test_connection"})
        self.assertTrue(all(spec.idempotent for spec in READ_METHODS.values()))

    def test_encoding_uses_documented_frame_shape(self) -> None:
        raw = encode_read_request(7, "get_device_state", {"keys": ["device", "mount"]})
        self.assertTrue(raw.endswith(b"\r\n"))
        self.assertEqual(
            json.loads(raw),
            {"id": 7, "verify": True, "method": "get_device_state", "params": {"keys": ["device", "mount"]}},
        )
        self.assertEqual(json.loads(encode_read_request(1, "test_connection")), {"id": 1, "verify": True, "method": "test_connection"})

    def test_every_physical_or_generic_method_is_refused_at_encoding(self) -> None:
        for method in BANNED_METHODS:
            with self.assertRaises(SeestarMethodNotAllowed, msg=method):
                encode_read_request(1, method, {})

    def test_state_key_filter_never_requests_secret_blocks(self) -> None:
        self.assertTrue(ALLOWED_STATE_KEYS.isdisjoint(FORBIDDEN_STATE_KEYS))
        for key in FORBIDDEN_STATE_KEYS:
            with self.assertRaises(SeestarMethodNotAllowed):
                encode_read_request(1, "get_device_state", {"keys": [key]})
        with self.assertRaises(SeestarMethodNotAllowed):
            encode_read_request(1, "get_device_state", {"keys": []})
        with self.assertRaises(SeestarMethodNotAllowed):
            encode_read_request(1, "test_connection", {"x": 1})

    def test_transport_refuses_banned_methods_before_opening_any_socket(self) -> None:
        device = ScriptedDevice()
        transport = make_transport(device)
        for method in BANNED_METHODS:
            with self.assertRaises(SeestarMethodNotAllowed, msg=method):
                transport._exchange(HOST, method, {})
        self.assertEqual(device.connect_addresses, [])

    def test_transport_public_surface_is_only_typed_reads_and_udp_discovery(self) -> None:
        public = {n for n in dir(TcpSeestarTransport) if not n.startswith("_") and callable(getattr(TcpSeestarTransport, n))}
        self.assertEqual(public, {"read_device_state", "read_app_state", "test_connection", "discover_via_udp"})


class WireExchangeTests(unittest.TestCase):
    def test_single_read_is_one_short_lived_connection_with_exact_request(self) -> None:
        device = ScriptedDevice()
        reply = make_transport(device).read_device_state(HOST, ["device"])
        self.assertEqual(device.connect_addresses, [(HOST, 4700)])
        (sock,) = device.sockets
        self.assertTrue(sock.closed)
        self.assertEqual(sock.received, [{"id": 1, "verify": True, "method": "get_device_state", "params": {"keys": ["device"]}}])
        self.assertEqual(reply.code, 0)
        self.assertEqual(reply.device_timestamp, "1234.567890123")
        self.assertEqual(set(reply.result), {"device"})

    def test_each_read_opens_a_new_connection(self) -> None:
        device = ScriptedDevice()
        transport = make_transport(device)
        transport.test_connection(HOST)
        transport.read_app_state(HOST)
        self.assertEqual(len(device.sockets), 2)
        self.assertTrue(all(s.closed for s in device.sockets))

    def test_interleaved_events_and_fragmented_frames_are_handled(self) -> None:
        device = ScriptedDevice()
        device.events_before_reply = 3
        device.chunk_size = 7
        reply = make_transport(device).read_app_state(HOST)
        self.assertEqual(reply.code, 0)
        self.assertIn("SecondView", reply.result)

    def test_too_many_interleaved_events_is_a_protocol_error(self) -> None:
        device = ScriptedDevice()
        device.events_before_reply = 600
        with self.assertRaises(SeestarProtocolError):
            make_transport(device, make_config(read_retries=0)).read_app_state(HOST)

    def test_oversized_frame_is_rejected(self) -> None:
        with self.assertRaises(SeestarProtocolError):
            split_frames(b"x" * (MAX_FRAME_BYTES + 1))

    def test_non_json_frames_are_skipped(self) -> None:
        frames, rest = split_frames(b"garbage\r\n{\"id\":1}\r\npartial")
        self.assertEqual(frames, [b"garbage", b'{"id":1}'])
        self.assertEqual(rest, b"partial")


class AuthenticationPrerequisiteTests(unittest.TestCase):
    def test_handshake_runs_before_the_read_and_only_as_prerequisite(self) -> None:
        device = ScriptedDevice(auth="required")
        transport = make_transport(device, authenticator=StubAuthenticator())
        reply = transport.read_device_state(HOST, ["device"])
        self.assertEqual(reply.code, 0)
        self.assertEqual(device.methods, ["get_verify_str", "verify_client", "pi_is_verified", "get_device_state"])
        self.assertEqual(device.unexpected, [])

    def test_rejected_signature_stops_before_any_read(self) -> None:
        device = ScriptedDevice(auth="required")
        device.accept_signature = False
        transport = make_transport(device, make_config(read_retries=2), authenticator=StubAuthenticator())
        with self.assertRaises(SeestarAuthError) as ctx:
            transport.read_device_state(HOST, ["device"])
        self.assertEqual(ctx.exception.category, "auth_rejected")
        self.assertNotIn("get_device_state", device.methods)
        self.assertEqual(len(device.sockets), 1)  # auth failures are never retried
        self.assertTrue(device.sockets[0].closed)

    def test_firmware_without_handshake_skips_it(self) -> None:
        device = ScriptedDevice(auth="absent")
        make_transport(device, authenticator=StubAuthenticator()).test_connection(HOST)
        self.assertEqual(device.methods, ["get_verify_str", "test_connection"])

    def test_no_authenticator_means_no_handshake(self) -> None:
        device = ScriptedDevice()
        make_transport(device).test_connection(HOST)
        self.assertEqual(device.methods, ["test_connection"])


class RetryAndFailureTests(unittest.TestCase):
    def test_idempotent_read_retries_exactly_configured_times_on_timeout(self) -> None:
        for retries in (0, 1, 2):
            device = ScriptedDevice(auth="required")  # silent without a handshake -> read timeout
            transport = make_transport(device, make_config(read_retries=retries))
            with self.assertRaises(SeestarTimeout):
                transport.test_connection(HOST)
            self.assertEqual(len(device.sockets), 1 + retries)
            self.assertTrue(all(s.closed for s in device.sockets))

    def test_connect_failures_are_unreachable_and_retried(self) -> None:
        device = ScriptedDevice()
        device.connect_errors.extend([ConnectionRefusedError(), OSError()])
        with self.assertRaises(SeestarUnreachable):
            make_transport(device, make_config(read_retries=1)).test_connection(HOST)
        self.assertEqual(len(device.connect_addresses), 2)

    def test_connect_timeout_is_a_timeout(self) -> None:
        device = ScriptedDevice()
        device.connect_errors.append(socket.timeout())
        with self.assertRaises(SeestarTimeout):
            make_transport(device, make_config(read_retries=0)).test_connection(HOST)

    def test_recovery_after_one_transient_failure(self) -> None:
        device = ScriptedDevice()
        device.connect_errors.append(ConnectionResetError())
        reply = make_transport(device, make_config(read_retries=1)).test_connection(HOST)
        self.assertEqual(reply.code, 0)
        self.assertEqual(len(device.connect_addresses), 2)

    def test_peer_closing_mid_read_is_unreachable(self) -> None:
        device = ScriptedDevice(auth="required")
        device.close_when_empty = True
        with self.assertRaises(SeestarUnreachable) as ctx:
            make_transport(device, make_config(read_retries=0)).test_connection(HOST)
        self.assertEqual(ctx.exception.category, "connection_closed")

    def test_overall_read_deadline_is_enforced_even_when_data_keeps_arriving(self) -> None:
        def slow_device() -> ScriptedDevice:
            device = ScriptedDevice()
            device.events_before_reply = 5
            device.chunk_size = 10  # many small reads before the reply frame completes
            return device

        generous = make_transport(slow_device(), make_config(read_retries=0, read_timeout_s=60), monotonic=StepClock(0.001))
        self.assertEqual(generous.test_connection(HOST).code, 0)
        strict = make_transport(slow_device(), make_config(read_retries=0, read_timeout_s=0.1), monotonic=StepClock(0.2))
        with self.assertRaises(SeestarTimeout) as ctx:
            strict.test_connection(HOST)
        self.assertEqual(ctx.exception.category, "read_timeout")

    def test_error_text_never_contains_host_or_key_path(self) -> None:
        device = ScriptedDevice()
        device.connect_errors.append(OSError(f"cannot reach {HOST} using {KEY_PATH}"))
        with self.assertRaises(SeestarUnreachable) as ctx:
            make_transport(device, make_config(read_retries=0)).test_connection(HOST)
        text = f"{ctx.exception} {ctx.exception!r} {ctx.exception.category}"
        self.assertNotIn(HOST, text)
        self.assertNotIn(KEY_PATH, text)


class UdpDiscoveryTests(unittest.TestCase):
    def _transport(self, sock: ScriptedUdpSocket, **config):
        return TcpSeestarTransport(
            make_config(allow_udp_discovery=True, host=None, **config),
            udp_factory=lambda: sock,
            monotonic=StepClock(0.1),
        )

    def test_udp_discovery_is_opt_in_and_creates_no_socket_when_disabled(self) -> None:
        created: list[object] = []
        transport = TcpSeestarTransport(make_config(), udp_factory=lambda: created.append(1))
        with self.assertRaises(SeestarConfigError):
            transport.discover_via_udp()
        self.assertEqual(created, [])

    def test_probe_targets_and_reply_parsing(self) -> None:
        scan = json.dumps(load_fixture("scan_reply.json")).encode()
        sock = ScriptedUdpSocket([(b"not json", ("192.0.2.9", 4720)), (scan, ("192.0.2.77", 4720)), (scan, ("192.0.2.78", 4720))])
        found = self._transport(sock, broadcast_targets=("192.0.2.255",)).discover_via_udp()
        self.assertTrue(sock.closed)
        self.assertEqual(sock.sent[0], (SCAN_PROBE, ("192.0.2.255", 4720)))
        self.assertIn((socket.SOL_SOCKET, socket.SO_BROADCAST, 1), sock.options)
        self.assertEqual([(a.sn, a.model, a.ip) for a in found], [("0badc0de", "Seestar S30 Pro", "192.0.2.78")])  # last reply wins

    def test_silence_is_a_valid_empty_result(self) -> None:
        self.assertEqual(self._transport(ScriptedUdpSocket([])).discover_via_udp(), ())

    def test_scan_reply_without_serial_is_ignored(self) -> None:
        self.assertIsNone(parse_scan_reply(b'{"result": {"product_model": "x"}}', "192.0.2.1"))
        self.assertIsNone(parse_scan_reply(b"[]", "192.0.2.1"))


@unittest.skipUnless(
    __import__("importlib").util.find_spec("cryptography") is not None, "cryptography not installed"
)
class RsaSignerTests(unittest.TestCase):
    def test_signature_verifies_with_a_throwaway_key_generated_in_memory(self) -> None:
        import base64

        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "throwaway.pem")
            with open(path, "wb") as handle:
                handle.write(pem)
            signature = RsaKeyFileAuthenticator(path).sign("challenge-text")
        try:
            key.public_key().verify(base64.b64decode(signature), b"challenge-text", padding.PKCS1v15(), hashes.SHA1())
        except InvalidSignature:  # pragma: no cover
            self.fail("signature did not verify")

    def test_missing_or_garbage_key_fails_with_fixed_category_and_no_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            garbage = os.path.join(tmp, "not-a-key.pem")
            with open(garbage, "w") as handle:
                handle.write("not a pem")
            for path in (os.path.join(tmp, "absent.pem"), garbage):
                with self.assertRaises(SeestarAuthError) as ctx:
                    RsaKeyFileAuthenticator(path).sign("c")
                self.assertEqual(ctx.exception.category, "key_unusable")
                self.assertNotIn(path, f"{ctx.exception!r} {ctx.exception}")

    def test_missing_crypto_library_is_a_fixed_category(self) -> None:
        with mock.patch.dict(sys.modules, {"cryptography.hazmat.primitives": None}):
            with self.assertRaises(SeestarAuthError) as ctx:
                RsaKeyFileAuthenticator("any.pem").sign("c")
        self.assertEqual(ctx.exception.category, "crypto_library_missing")

    def test_authenticator_repr_hides_the_path(self) -> None:
        self.assertNotIn("any.pem", repr(RsaKeyFileAuthenticator("any.pem")))

    def test_key_not_configured_is_refused(self) -> None:
        with self.assertRaises(SeestarAuthError):
            RsaKeyFileAuthenticator("")


class ConfigTests(unittest.TestCase):
    def test_there_is_no_default_host_and_no_fallback_address(self) -> None:
        config = SeestarProviderConfig()
        self.assertIsNone(config.host)
        self.assertFalse(config.is_complete)
        self.assertFalse(config.allow_udp_discovery)

    def test_completeness_needs_a_key_and_a_host_or_opt_in_discovery(self) -> None:
        self.assertTrue(make_config().is_complete)
        self.assertTrue(make_config(host=None, allow_udp_discovery=True).is_complete)
        self.assertFalse(make_config(host=None).is_complete)
        self.assertFalse(make_config(key_path=None).is_complete)

    def test_invalid_values_are_rejected_with_fixed_categories(self) -> None:
        for kwargs in (
            {"host": "http://192.0.2.1"}, {"host": "192.0.2.1:4700"}, {"host": " "}, {"host": "bad host"},
            {"provider_id": ""}, {"tcp_port": 0}, {"connect_timeout_s": 0}, {"read_retries": 9},
            {"preview_camera": "x"}, {"broadcast_targets": ("not-an-ip",)}, {"key_path": " "},
        ):
            with self.assertRaises(SeestarConfigError, msg=kwargs):
                make_config(**kwargs)

    def test_hostnames_and_ipv6_literals_are_accepted(self) -> None:
        make_config(host="seestar.example.net")
        make_config(host="2001:db8::1")

    def test_repr_str_and_redacted_hide_host_and_key_path(self) -> None:
        config = make_config()
        for text in (repr(config), str(config), json.dumps(config.redacted()), repr(TcpSeestarTransport(config))):
            self.assertNotIn(HOST, text)
            self.assertNotIn(KEY_PATH, text)

    def test_with_host_returns_a_new_config(self) -> None:
        config = make_config()
        moved = config.with_host("192.0.2.77")
        self.assertEqual((config.host, moved.host), (HOST, "192.0.2.77"))
        self.assertEqual(moved.key_path, config.key_path)


class HandshakeBoundaryAndHygieneTests(unittest.TestCase):
    def test_handshake_sends_only_the_three_fixed_messages_with_fixed_shapes(self) -> None:
        device = ScriptedDevice(auth="required")
        make_transport(device, authenticator=StubAuthenticator()).test_connection(HOST)
        sent = device.sockets[0].received
        self.assertEqual([m["id"] for m in sent], [1001, 1002, 1003, 1])
        self.assertEqual([m["method"] for m in sent[:3]], ["get_verify_str", "verify_client", "pi_is_verified"])
        self.assertEqual(sent[0]["params"], "verify")
        self.assertEqual(set(sent[1]["params"]), {"sign", "data"})
        self.assertEqual(sent[2]["params"], "verify")
        self.assertEqual(sent[3]["method"], "test_connection")

    def test_a_hostile_challenge_cannot_inject_methods(self) -> None:
        device = ScriptedDevice(auth="required")
        device.challenge = '"}, {"method": "scope_park", "params": {}}'
        make_transport(device, authenticator=StubAuthenticator()).test_connection(HOST)
        self.assertEqual(device.unexpected, [])
        self.assertNotIn("scope_park", device.methods)

    def test_boolean_reply_ids_never_match_numeric_request_ids(self) -> None:
        device = ScriptedDevice()
        original = device.handle

        def spoof(message):
            frames = original(message)
            for frame in frames:
                if frame.get("id") == message.get("id"):
                    frame["id"] = True  # JSON true == 1 in Python
            return frames

        device.handle = spoof  # type: ignore[method-assign]
        with self.assertRaises(SeestarTimeout):
            make_transport(device, make_config(read_retries=0)).test_connection(HOST)

    def test_payload_bearing_objects_do_not_repr_their_contents(self) -> None:
        reply = make_transport(ScriptedDevice()).read_device_state(HOST, ["device"])
        self.assertNotIn("0badc0de", repr(reply))
        self.assertNotIn("SYNTHETIC", repr(reply))
        scan = parse_scan_reply(json.dumps(load_fixture("scan_reply.json")).encode(), "192.0.2.50")
        self.assertNotIn("192.0.2.50", repr(scan))
        self.assertNotIn("0badc0de", repr(scan))


if __name__ == "__main__":
    unittest.main()
