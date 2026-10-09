"""DB-03 Wave 4B-1: the worker's command handler, in process (no child process, no I/O)."""

from __future__ import annotations

import unittest
from unittest import mock

from tsn_dss.engine.opencv_isolated_decoder import protocol as P
from tsn_dss.engine.opencv_isolated_decoder import worker_main as W

INIT = P.Init(1, 1, 99, "", 0, 10, 10)
ADDRESS = "rtsp://host.example/stream"


class Exited(Exception):
    def __init__(self, code=None):
        super().__init__(code)
        self.code = code


def patched():
    return mock.patch.object(W.os, "_exit", side_effect=Exited)


def reply(handler, message):
    replies, done = handler.handle(message)
    return [P.decode(r) for r in replies], done


class HandshakeTests(unittest.TestCase):
    def test_init_is_answered_with_matching_versions_and_the_nonce(self) -> None:
        (hello,), done = reply(W.ProductionHandler(), INIT)
        self.assertEqual((hello.proto_version, hello.status_table_version, hello.status, hello.nonce), (1, 1, 0, 99))
        self.assertFalse(done)

    def test_an_old_interpreter_answers_python_unsupported(self) -> None:
        with mock.patch.object(W, "_MIN_PYTHON", (99, 0)):
            (hello,), _ = reply(W.ProductionHandler(), INIT)
        self.assertEqual(P.category_for_status(hello.status), "python_unsupported")

    def test_a_segment_that_does_not_exist_is_refused(self) -> None:
        (hello,), _ = reply(W.ProductionHandler(), P.Init(1, 1, 1, "no_such_segment_x", 100, 10, 10))
        self.assertEqual(P.category_for_status(hello.status), "shm_unavailable")

    def test_ping_is_answered_after_init(self) -> None:
        h = W.ProductionHandler()
        reply(h, INIT)
        (pong,), done = reply(h, P.Ping(5))
        self.assertEqual((pong, done), (P.Pong(5), False))

    def test_close_is_answered_and_ends_the_loop(self) -> None:
        h = W.ProductionHandler()
        reply(h, INIT)
        (result,), done = reply(h, P.Close(6))
        self.assertEqual((result, done), (P.Result(6, 0), True))

    def test_open_and_read_are_not_available_in_this_build(self) -> None:
        h = W.ProductionHandler()
        reply(h, INIT)
        for message in (P.Open(1, 5000, 3000, ADDRESS), P.Read(2)):
            (result,), done = reply(h, message)
            self.assertEqual(P.category_for_status(result.status), "invalid_state")
            self.assertFalse(done)


class ProtocolViolationTests(unittest.TestCase):
    def test_anything_before_init_ends_the_worker(self) -> None:
        for message in (P.Ping(1), P.Close(1), P.Read(1)):
            with patched() as exit_, self.assertRaises(Exited):
                W.ProductionHandler().handle(message)
            exit_.assert_called_with(W.EXIT_PROTOCOL)

    def test_a_second_init_ends_the_worker(self) -> None:
        h = W.ProductionHandler()
        reply(h, INIT)
        with patched() as exit_, self.assertRaises(Exited):
            h.handle(INIT)
        exit_.assert_called_with(W.EXIT_PROTOCOL)

    def test_messages_only_a_worker_may_send_end_the_worker(self) -> None:
        h = W.ProductionHandler()
        reply(h, INIT)
        for message in (P.Hello(1, 1, 0, 1), P.Pong(1), P.Result(1, 0), P.ImageReady(1, 1, 1, 1, 1, 1, 0)):
            with patched() as exit_, self.assertRaises(Exited):
                h.handle(message)
            exit_.assert_called_with(W.EXIT_PROTOCOL)


class EntryPointTests(unittest.TestCase):
    def test_bad_command_lines_are_refused_without_opening_anything(self) -> None:
        for argv in ([], ["--ctl"], ["--ctl", "x", "--proto", "1"], ["--ctl", "5"], ["--ctl", "5", "--proto", "2"], ["--ctl", "-1", "--proto", "1"]):
            with mock.patch.object(W, "_adopt", side_effect=AssertionError("must not adopt a handle")):
                self.assertEqual(W.main(argv), 64, argv)

    def test_exit_codes_are_distinct(self) -> None:
        self.assertEqual((W.EXIT_PARENT_GONE, W.EXIT_PROTOCOL), (3, 4))


if __name__ == "__main__":
    unittest.main()
