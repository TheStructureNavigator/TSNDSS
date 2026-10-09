"""Dedicated, importable worker entry point (DB-03 Wave 4B-1): ``python -P -m <this module> --ctl <handle> --proto 1``.

Started by ``process.WorkerProcess`` through ``subprocess.Popen``; never imports or re-executes the parent's main
module. The only thing that arrives on the command line is a numeric pipe handle and the protocol number: no host, no
address, no key, no segment name.

4B-1 scope: the readiness handshake (``INIT`` -> ``HELLO``, ``PING`` -> ``PONG``) and a clean ``CLOSE``. ``OPEN`` and
``READ`` are answered with ``INVALID_STATE`` (the decoder arrives in 4B-3). OpenCV is never imported here.

Threads: a dedicated reader thread blocks on the control pipe and queues commands; when the pipe closes (the parent is
gone) it ends the process at once with ``os._exit``, even while the main thread is stuck. The main thread executes
commands. The worker never uses pickle (``send``/``recv``).
"""

from __future__ import annotations

import os
import queue
import sys
import threading

from . import protocol as P

__all__ = ["EXIT_PARENT_GONE", "EXIT_PROTOCOL", "ProductionHandler", "main", "serve"]

EXIT_PARENT_GONE = 3
EXIT_PROTOCOL = 4
_MIN_PYTHON = (3, 13)


def _adopt(handle: int):
    if sys.platform == "win32":
        from multiprocessing.connection import PipeConnection
        return PipeConnection(handle)
    from multiprocessing.connection import Connection
    return Connection(handle)


class ProductionHandler:
    """Handshake-only behavior. Subclassed by test fixtures; ``handle`` returns (raw replies, done)."""

    def __init__(self) -> None:
        self.initialised = False

    def handle(self, message: P.Message) -> tuple[list[bytes], bool]:
        if isinstance(message, P.Init):
            if self.initialised:
                os._exit(EXIT_PROTOCOL)
            self.initialised = True
            status = P.Status.OK
            if sys.version_info[:2] < _MIN_PYTHON:
                status = P.Status.PYTHON_UNSUPPORTED
            elif message.segment_name:
                status = P.Status.SHM_UNAVAILABLE          # the shared slot is attached in 4B-2
            return [P.encode(P.Hello(P.PROTOCOL_VERSION, P.STATUS_TABLE_VERSION, int(status), message.nonce))], False
        if not self.initialised:
            os._exit(EXIT_PROTOCOL)
        if isinstance(message, P.Ping):
            return [P.encode(P.Pong(message.op_id))], False
        if isinstance(message, (P.Open, P.Read)):
            return [P.encode(P.Result(message.op_id, int(P.Status.INVALID_STATE)))], False
        if isinstance(message, P.Close):
            return [P.encode(P.Result(message.op_id, int(P.Status.OK)))], True
        os._exit(EXIT_PROTOCOL)                             # a message that only a worker may send

    def after_reply(self) -> None:
        """Hook run after the replies were sent (fixtures use it to misbehave after a successful answer)."""


def serve(ctl, handler: ProductionHandler) -> int:
    commands: "queue.Queue[bytes]" = queue.Queue()

    def reader() -> None:
        try:
            while True:
                commands.put(ctl.recv_bytes(P.MAX_MESSAGE_BYTES))
        except (EOFError, OSError):
            os._exit(EXIT_PARENT_GONE)                      # lifeline watchdog: the parent end is gone

    threading.Thread(target=reader, name="control-reader", daemon=True).start()
    while True:
        raw = commands.get()
        try:
            message = P.decode(raw)
        except P.ProtocolError:
            os._exit(EXIT_PROTOCOL)
        replies, done = handler.handle(message)
        for reply in replies:
            ctl.send_bytes(reply)
        handler.after_reply()
        if done:
            os._exit(0)                                     # process exit closes every handle; no close() racing the reader


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    values: dict[str, str] = {}
    it = iter(args)
    for flag in it:
        if flag in ("--ctl", "--proto"):
            values[flag] = next(it, "")
    try:
        handle = int(values["--ctl"])
        proto = int(values["--proto"])
    except (KeyError, ValueError):
        return 64
    if proto != P.PROTOCOL_VERSION or handle < 0:
        return 64
    return serve(_adopt(handle), ProductionHandler())


if __name__ == "__main__":
    sys.exit(main())
