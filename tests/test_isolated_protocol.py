"""DB-03 Wave 4B-1: binary control protocol, status table (decision D13 freeze checks) and segment-header codec."""

from __future__ import annotations

import re
import struct
import unittest
from pathlib import Path

from tsn_dss.engine.opencv_isolated_decoder import protocol as P
from tsn_dss.engine.opencv_isolated_decoder import segment as S

ROOT = Path(__file__).resolve().parents[1]
VALID_ADDRESS = "rtsp://host.example:4554/stream"


def samples() -> list:
    return [
        P.Init(1, 1, 2 ** 64 - 1, "", 0, 1920, 1080),
        P.Init(1, 1, 7, "psm_ab12.cd-3/x", 1000, 16384, 16384),
        P.Hello(1, 1, 0, 5),
        P.Hello(1, 1, 255, 2 ** 64 - 1),
        P.Ping(1), P.Pong(2 ** 32 - 1), P.Read(9), P.Close(10),
        P.Open(3, 100, 60000, VALID_ADDRESS),
        P.Result(6, 9),
        P.ImageReady(7, 1, 4, 2, 3, 24, 99),
    ]


class RoundTripTests(unittest.TestCase):
    def test_every_message_round_trips_and_fits_the_cap(self) -> None:
        for message in samples():
            raw = P.encode(message)
            self.assertLessEqual(len(raw), P.MAX_MESSAGE_BYTES)
            self.assertEqual(P.decode(raw), message)

    def test_the_largest_messages_fit(self) -> None:
        longest = "rtsp://" + "a" * (P.MAX_ADDRESS_BYTES - 7)
        self.assertEqual(P.decode(P.encode(P.Open(1, 100, 100, longest))).address, longest)
        name = "n" * P.MAX_SEGMENT_NAME_BYTES
        self.assertEqual(P.decode(P.encode(P.Init(1, 1, 1, name, 1, 1, 1))).segment_name, name)

    def test_open_never_shows_its_address(self) -> None:
        message = P.Open(3, 5000, 3000, VALID_ADDRESS)
        for text in (repr(message), str(message), f"{message}"):
            self.assertNotIn("host.example", text)
            self.assertNotIn("rtsp://", text)

    def test_header_layout_is_fixed(self) -> None:
        raw = P.encode(P.Ping(1))
        self.assertEqual(raw[:4], b"TDW1")
        self.assertEqual(raw[4], int(P.MessageType.PING))
        self.assertEqual(raw[5:8], b"\0\0\0")
        self.assertEqual(len(raw), 8 + 4)

    def test_message_types_are_pinned(self) -> None:
        self.assertEqual({t.name: int(t) for t in P.MessageType},
                         {"INIT": 1, "HELLO": 2, "PING": 3, "PONG": 4, "OPEN": 5, "READ": 6, "CLOSE": 7, "RESULT": 8, "IMAGE_READY": 9})


class MalformedInputTests(unittest.TestCase):
    def assertRejected(self, raw, reason=None) -> None:
        with self.assertRaises(P.ProtocolError) as ctx:
            P.decode(raw)
        self.assertEqual(ctx.exception.category, "worker_protocol_error")
        if reason:
            self.assertEqual(ctx.exception.reason, reason)

    def test_not_bytes(self) -> None:
        for bad in (None, "text", 5, [1, 2], object()):
            self.assertRejected(bad, "not_bytes")

    def test_size_limits(self) -> None:
        self.assertRejected(b"", "message_size")
        self.assertRejected(b"TDW1", "message_size")
        self.assertRejected(P.encode(P.Ping(1)) + b"x" * P.MAX_MESSAGE_BYTES, "message_size")

    def test_header_violations(self) -> None:
        good = bytearray(P.encode(P.Ping(1)))
        for index, value, reason in ((0, ord("X"), "magic"), (5, 1, "reserved_bits"), (6, 1, "reserved_bits"),
                                     (7, 1, "reserved_bits"), (4, 0, "message_type"), (4, 10, "message_type"), (4, 255, "message_type")):
            raw = bytearray(good)
            raw[index] = value
            self.assertRejected(bytes(raw), reason)

    def test_every_type_rejects_truncation_and_trailing_bytes(self) -> None:
        for message in samples():
            raw = P.encode(message)
            self.assertRejected(raw[:-1])
            self.assertRejected(raw + b"\0")
            self.assertRejected(raw[:8])

    def test_zero_op_ids_are_reserved(self) -> None:
        for cls in (P.Ping, P.Pong, P.Read, P.Close):
            with self.assertRaises(P.ProtocolError):
                cls(0)
            self.assertRejected(P.encode(cls(1))[:8] + struct.pack("!I", 0), "op_id_range")

    def test_open_field_rules(self) -> None:
        for kwargs in (dict(open_timeout_ms=99), dict(open_timeout_ms=60001), dict(read_timeout_ms=0), dict(read_timeout_ms=True),
                       dict(address=""), dict(address="rtsp://"), dict(address="http://x/y"), dict(address="rtsp://a b"),
                       dict(address="rtsp://a\nb"), dict(address="rtsp://a\x7fb"), dict(address="rtsp://" + "a" * 600),
                       dict(address=5)):
            fields = dict(op_id=1, open_timeout_ms=5000, read_timeout_ms=3000, address=VALID_ADDRESS)
            fields.update(kwargs)
            with self.assertRaises(P.ProtocolError, msg=str(kwargs)):
                P.Open(**fields)

    def test_open_wire_rules(self) -> None:
        head = struct.pack("!IIIH", 1, 5000, 3000, 21)
        frame = lambda payload: P._wrap(P.MessageType.OPEN, payload)
        self.assertRejected(frame(head + b"rtsp://\xff\xfe" + b"a" * 12), "address")             # not UTF-8
        self.assertRejected(frame(struct.pack("!IIIH", 1, 5000, 3000, 513) + b"rtsp://" + b"a" * 506), "address")   # over the cap
        self.assertRejected(frame(struct.pack("!IIIH", 1, 5000, 3000, 30) + b"rtsp://short"), "payload_length")      # length lies

    def test_init_rules(self) -> None:
        for kwargs in (dict(segment_name="bad name", slot_bytes=1), dict(segment_name="n", slot_bytes=0), dict(segment_name="", slot_bytes=5),
                       dict(max_width=0), dict(max_height=16385), dict(nonce=-1), dict(nonce=2 ** 64), dict(proto_version=65536),
                       dict(segment_name="x" * 256, slot_bytes=1), dict(slot_bytes=300 * 1024 * 1024, segment_name="n")):
            fields = dict(proto_version=1, status_table_version=1, nonce=1, segment_name="", slot_bytes=0, max_width=10, max_height=10)
            fields.update(kwargs)
            with self.assertRaises(P.ProtocolError, msg=str(kwargs)):
                P.Init(**fields)

    def test_init_wire_name_length_lies(self) -> None:
        fixed = struct.pack("!HHQB", 1, 1, 1, 200)
        self.assertRejected(P._wrap(P.MessageType.INIT, fixed + b"abc" + struct.pack("!III", 1, 1, 1)), "payload_length")
        self.assertRejected(P._wrap(P.MessageType.INIT, struct.pack("!HHQB", 1, 1, 1, 2) + b"\xff\xfe" + struct.pack("!III", 1, 1, 1)), "segment_name")

    def test_image_ready_field_rules(self) -> None:
        base = dict(op_id=1, seq=1, width=2, height=2, pixel_format=1, nbytes=4, decode_ns=0)
        for kwargs in (dict(seq=0), dict(width=0), dict(height=16385), dict(pixel_format=2), dict(pixel_format=0), dict(nbytes=0),
                       dict(decode_ns=-1), dict(op_id=0)):
            with self.assertRaises(P.ProtocolError, msg=str(kwargs)):
                P.ImageReady(**{**base, **kwargs})

    def test_decode_never_raises_anything_but_protocol_error(self) -> None:
        import random
        rng = random.Random(1234)
        good = [P.encode(m) for m in samples()]
        for _ in range(3000):
            raw = bytearray(rng.choice(good))
            for _ in range(rng.randint(1, 4)):
                raw[rng.randrange(len(raw))] = rng.randrange(256)
            if rng.random() < 0.3:
                raw = raw[: rng.randrange(len(raw) + 1)]
            try:
                P.decode(bytes(raw))
            except P.ProtocolError:
                pass


class StatusTableTests(unittest.TestCase):
    """Decision D13: the four freeze checks (mapping, versioning, unknown codes, separation of parent errors)."""

    PINNED = {
        1: "opencv_unavailable", 2: "opencv_version_unknown", 3: "opencv_version_unsupported", 4: "opencv_incompatible",
        5: "invalid_endpoint", 6: "open_failed", 7: "backend_unverified", 8: "backend_mismatch", 9: "read_failed",
        10: "decoder_bad_output", 11: "unsupported_depth", 12: "unsupported_format", 13: "dimensions_exceed_limits",
        14: "image_exceeds_limits", 15: "length_mismatch", 16: "release_failed", 17: "invalid_state", 18: "busy",
        19: "not_open", 20: "python_unsupported", 21: "shm_unavailable", 255: "worker_internal",
    }

    def test_table_is_frozen(self) -> None:
        self.assertEqual(P.STATUS_TABLE, self.PINNED)
        self.assertEqual(int(P.Status.OK), 0)
        self.assertEqual(P.STATUS_TABLE_VERSION, 1)
        self.assertEqual(P.PROTOCOL_VERSION, 1)

    def test_mapping_covers_every_wave4_decoder_category(self) -> None:
        source = (ROOT / "tsn_dss" / "engine" / "opencv_preview_decoder" / "decoder.py").read_text(encoding="utf-8")
        raised = set(re.findall(r'DecoderError\("([a-z_]+)"\)', source))
        self.assertGreaterEqual(len(raised), 15)
        self.assertEqual(raised - set(P.STATUS_TABLE.values()), set(), "a Wave 4 category has no wire code")
        extras = set(P.STATUS_TABLE.values()) - raised
        self.assertEqual(extras, {"length_mismatch", "python_unsupported", "shm_unavailable", "worker_internal"})

    def test_code_category_code_is_the_identity(self) -> None:
        for code, category in P.STATUS_TABLE.items():
            self.assertEqual(P.category_for_status(code), category)
            self.assertEqual(P.status_for_category(category), code)
        self.assertIsNone(P.category_for_status(0))

    def test_no_duplicates_and_no_free_text(self) -> None:
        categories = list(P.STATUS_TABLE.values())
        self.assertEqual(len(categories), len(set(categories)))
        for category in categories:
            self.assertRegex(category, r"^[a-z][a-z_]*$")

    def test_categories_outside_the_table_become_worker_internal(self) -> None:
        for category in ("dimensions_out_of_range", "image_too_large", "data_not_bytes", "pixels_missing", "", "rtsp://x", "anything"):
            self.assertEqual(P.status_for_category(category), int(P.Status.WORKER_INTERNAL))

    def test_unknown_codes_are_a_protocol_violation(self) -> None:
        for code in (22, 23, 77, 100, 254, 256, 1000, 65535, -1, None, "1", 1.5):
            with self.assertRaises(P.UnknownStatus, msg=repr(code)):
                P.category_for_status(code)
            self.assertIsInstance(P.UnknownStatus(), P.ProtocolError)

    def test_unknown_status_survives_the_wire_so_the_parent_can_apply_its_rule(self) -> None:
        hello = P.decode(P.encode(P.Hello(1, 1, 77, 5)))
        self.assertEqual(hello.status, 77)
        with self.assertRaises(P.UnknownStatus):
            P.category_for_status(hello.status)

    def test_parent_categories_have_no_wire_code(self) -> None:
        self.assertEqual(P.PARENT_CATEGORIES & set(P.STATUS_TABLE.values()), set())
        self.assertEqual(P.PARENT_CATEGORIES, {
            "worker_start_failed", "worker_handshake_timeout", "worker_protocol_error", "worker_crashed", "worker_ipc_lost",
            "open_deadline_exceeded", "read_deadline_exceeded", "close_deadline_exceeded", "containment_failed", "worker_limit",
            "readiness_revoked"})
        for category in P.PARENT_CATEGORIES:
            self.assertNotIn(category, P.STATUS_TABLE.values())
            self.assertEqual(P.status_for_category(category), int(P.Status.WORKER_INTERNAL))   # cannot be forged by a code

    def test_a_worker_can_only_cause_a_parent_category_through_the_unknown_code_rule(self) -> None:
        self.assertEqual(P.ProtocolError.category, "worker_protocol_error")
        self.assertIn(P.ProtocolError.category, P.PARENT_CATEGORIES)
        for code in P.STATUS_TABLE:
            self.assertNotIn(P.category_for_status(code), P.PARENT_CATEGORIES)


class SegmentHeaderTests(unittest.TestCase):
    def header(self, **kw):
        fields = dict(slot_bytes=1000, handshake_nonce=11, worker_pid=22, begin_seq=3, end_seq=3, width=4, height=2, pixel_format=3, nbytes=24)
        fields.update(kw)
        return S.SegmentHeader(**fields)

    def buffer(self, header=None, slot=1000):
        return bytearray(S.pack_header(header or self.header(slot_bytes=slot)) + bytes(slot))

    def test_layout_constants(self) -> None:
        self.assertEqual(S.HEADER_BYTES, 64)
        self.assertEqual(len(S.pack_header(self.header())), 64)
        self.assertEqual(S.pack_header(self.header())[:4], b"TDS1")
        self.assertEqual(S.segment_size(1000), 1064)

    def test_round_trip_and_field_offsets(self) -> None:
        h = self.header()
        raw = S.pack_header(h)
        self.assertEqual(S.unpack_header(raw + bytes(1000)), h)
        self.assertEqual(struct.unpack_from("<H", raw, 4)[0], S.LAYOUT_VERSION)
        self.assertEqual(struct.unpack_from("<I", raw, 6)[0], 1000)
        self.assertEqual(struct.unpack_from("<Q", raw, 12)[0], 11)
        self.assertEqual(struct.unpack_from("<I", raw, 20)[0], 22)
        self.assertEqual(struct.unpack_from("<II", raw, 24), (3, 3))
        self.assertEqual(struct.unpack_from("<II", raw, 32), (4, 2))
        self.assertEqual(raw[40], 3)
        self.assertEqual(struct.unpack_from("<I", raw, 44)[0], 24)
        self.assertEqual(raw[48:], bytes(16))

    def test_unpack_rejects_bad_headers(self) -> None:
        good = self.buffer()
        for mutate, reason in (
            (lambda b: b.__setitem__(slice(0, 4), b"XXXX"), "header_magic"),
            (lambda b: struct.pack_into("<H", b, 4, 2), "header_version"),
            (lambda b: b.__setitem__(48, 1), "header_reserved"),
            (lambda b: b.__setitem__(41, 1), "header_reserved"),
            (lambda b: b.__setitem__(10, 1), "header_reserved"),
            (lambda b: struct.pack_into("<I", b, 6, 0), "slot_bytes_range"),
            (lambda b: struct.pack_into("<I", b, 6, 2 ** 31), "slot_bytes_range"),
        ):
            bad = bytearray(good)
            mutate(bad)
            with self.assertRaises(S.ProtocolError) as ctx:
                S.unpack_header(bytes(bad))
            self.assertEqual(ctx.exception.reason, reason)

    def test_short_buffers_and_slot_expectations(self) -> None:
        with self.assertRaises(S.ProtocolError):
            S.unpack_header(bytes(10))
        with self.assertRaises(S.ProtocolError) as ctx:
            S.unpack_header(S.pack_header(self.header()) + bytes(999))               # segment shorter than header + slot
        self.assertEqual(ctx.exception.reason, "header_segment_short")
        with self.assertRaises(S.ProtocolError) as ctx:
            S.unpack_header(self.buffer(), expected_slot_bytes=999)
        self.assertEqual(ctx.exception.reason, "header_slot_mismatch")
        S.unpack_header(self.buffer(), expected_slot_bytes=1000)

    def test_pack_rejects_out_of_range_fields(self) -> None:
        for kw in (dict(slot_bytes=0), dict(begin_seq=2 ** 32), dict(handshake_nonce=2 ** 64), dict(pixel_format=256), dict(worker_pid=-1)):
            with self.assertRaises(S.ProtocolError):
                S.pack_header(self.header(**kw))


class ImageValidationTests(unittest.TestCase):
    LIMITS = dict(max_width=100, max_height=100, max_image_bytes=30000)

    def notice(self, **kw):
        fields = dict(op_id=1, seq=3, width=4, height=2, pixel_format=3, nbytes=24, decode_ns=5)
        fields.update(kw)
        return P.ImageReady(**fields)

    def header(self, **kw):
        fields = dict(slot_bytes=1000, begin_seq=3, end_seq=3, width=4, height=2, pixel_format=3, nbytes=24)
        fields.update(kw)
        return S.SegmentHeader(**fields)

    def check(self, frame=None, header=None, **limits):
        S.validate_image(frame or self.notice(), header or self.header(), **{**self.LIMITS, **limits})

    def test_a_consistent_frame_is_accepted(self) -> None:
        self.check()
        self.check(self.notice(width=1, height=1, pixel_format=1, nbytes=1), self.header(width=1, height=1, pixel_format=1, nbytes=1))

    def test_torn_frames_are_rejected(self) -> None:
        for header in (self.header(begin_seq=4), self.header(end_seq=2), self.header(begin_seq=0, end_seq=0), self.header(begin_seq=3, end_seq=4)):
            with self.assertRaises(S.ProtocolError) as ctx:
                self.check(header=header)
            self.assertEqual(ctx.exception.reason, "image_fence")
        self.assertFalse(S.fence_is_stable(self.header(begin_seq=3, end_seq=4), 3))
        self.assertTrue(S.fence_is_stable(self.header(), 3))

    def test_header_and_message_must_agree(self) -> None:
        for header in (self.header(width=5), self.header(height=3), self.header(pixel_format=1), self.header(nbytes=25)):
            with self.assertRaises(S.ProtocolError) as ctx:
                self.check(header=header)
            self.assertEqual(ctx.exception.reason, "image_metadata_mismatch")

    def test_size_must_match_the_geometry(self) -> None:
        with self.assertRaises(S.ProtocolError) as ctx:
            self.check(self.notice(nbytes=25), self.header(nbytes=25))
        self.assertEqual(ctx.exception.reason, "image_size_mismatch")

    def test_slot_and_limits_are_enforced(self) -> None:
        with self.assertRaises(S.ProtocolError) as ctx:
            self.check(self.notice(width=20, height=20, nbytes=1200), self.header(width=20, height=20, nbytes=1200))   # 1200 > slot 1000
        self.assertEqual(ctx.exception.reason, "image_exceeds_slot")
        for limits in (dict(max_width=3), dict(max_height=1), dict(max_image_bytes=23)):
            with self.assertRaises(S.ProtocolError) as ctx:
                self.check(**limits)
            self.assertEqual(ctx.exception.reason, "image_exceeds_limits")
        self.check(max_width=4, max_height=2, max_image_bytes=24)                    # exact boundary is accepted

    def test_unknown_pixel_format_code(self) -> None:
        with self.assertRaises(S.ProtocolError):
            S.bytes_per_pixel(2)


if __name__ == "__main__":
    unittest.main()
