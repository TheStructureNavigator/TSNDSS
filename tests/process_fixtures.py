"""Test-only worker entry for DB-03 Wave 4B: ``python -P -m tests.process_fixtures --ctl H --proto 1 --fixture NAME``.

It reuses the production ``serve`` loop (reader-thread watchdog included) with handlers that misbehave on purpose.
Started through the launcher's private test seam; production code has no way to select a fixture. No network, no OpenCV.
"""

from __future__ import annotations

import json
import os
import signal
import struct
import sys
import threading
import time

from tsn_dss.engine.opencv_isolated_decoder import protocol as P
from tsn_dss.engine.opencv_isolated_decoder.worker_main import ProductionHandler, _adopt, serve


def hang() -> None:
    time.sleep(10 ** 6)


def spin() -> None:
    while True:
        pass


def is_console(fd: int) -> bool:
    """Is ``fd`` a real console/terminal? ``isatty`` is NOT used on Windows: the C runtime reports every character device,
    the null device included, as a tty there, so ``isatty`` cannot tell NUL from a console. ``GetConsoleMode`` succeeds only
    for a console handle."""
    if sys.platform == "win32":
        import ctypes
        import msvcrt
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetConsoleMode.restype = wintypes.BOOL
        try:
            handle = msvcrt.get_osfhandle(fd)
        except OSError:
            return False
        return bool(kernel32.GetConsoleMode(handle, ctypes.byref(wintypes.DWORD(0))))
    return os.isatty(fd)


def file_kind(fd: int) -> str:
    if sys.platform == "win32":
        import ctypes
        import msvcrt
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetFileType.argtypes = [wintypes.HANDLE]
        kernel32.GetFileType.restype = wintypes.DWORD
        try:
            return {0: "unknown", 1: "disk", 2: "char", 3: "pipe"}.get(kernel32.GetFileType(msvcrt.get_osfhandle(fd)), "other")
        except OSError:
            return "invalid"
    import stat
    try:
        mode = os.fstat(fd).st_mode
    except OSError:
        return "invalid"
    return "char" if stat.S_ISCHR(mode) else "pipe" if stat.S_ISFIFO(mode) else "disk" if stat.S_ISREG(mode) else "other"


def reads_eof(fd: int, timeout: float = 1.0) -> bool:
    """Behavioral check: reading the null device returns end-of-file at once; a console or an idle pipe would block."""
    result: list = []

    def reader() -> None:
        try:
            result.append(os.read(fd, 1))
        except OSError:
            result.append(b"<error>")

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    thread.join(timeout)
    return (not thread.is_alive()) and result == [b""]


def stdio_report() -> dict:
    return {
        "stdin": {"isatty": (sys.stdin.isatty() if sys.stdin else False), "is_console": is_console(0), "kind": file_kind(0), "reads_eof": reads_eof(0)},
        "stdout": {"is_console": is_console(1), "kind": file_kind(1)},
        "stderr": {"is_console": is_console(2), "kind": file_kind(2)},
    }


class Fixture(ProductionHandler):
    def __init__(self, name: str, report: str | None) -> None:
        super().__init__()
        self.name, self.report, self.pings = name, report, 0
        self.seq = 0

    def hello(self, message, **override) -> list:
        fields = dict(proto_version=P.PROTOCOL_VERSION, status_table_version=P.STATUS_TABLE_VERSION, status=0, nonce=message.nonce)
        fields.update(override)
        return [P.encode(P.Hello(**fields))]

    def write_report(self) -> None:
        main = sys.modules["__main__"]
        data = {
            "argv": sys.argv[1:],
            "env_names": sorted(os.environ),
            "probe_visible": "TSN_SPIKE_PROBE_SECRET" in os.environ,
            "main_spec": getattr(getattr(main, "__spec__", None), "name", None),
            "parent_main_marker": "parent_main_marker" in sys.modules,
            "safe_path": bool(sys.flags.safe_path),
            "cwd_is_temp": os.path.realpath(os.getcwd()) == os.path.realpath(os.environ.get("TEMP") or os.environ.get("TMPDIR") or "/tmp"),
            "heavy_modules": sorted(m for m in sys.modules if m.split(".")[0] in {"cv2", "numpy", "PIL", "av"}),
            "stdio": stdio_report(),
        }
        with open(self.report, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    # --- 4B-2: operations and shared-slot behaviour ---------------------------------------------------

    IMAGE = (4, 2, 3)                                         # width, height, pixel_format (BGR8): 24 bytes

    def synthetic(self, seq: int) -> bytes:
        w, h, fmt = self.IMAGE
        return bytes((seq * 7 + i) % 251 for i in range(w * h * (3 if fmt == 3 else 1)))

    def ready(self, op: int, seq: int, **override) -> bytes:
        w, h, fmt = self.IMAGE
        fields = dict(op_id=op, seq=seq, width=w, height=h, pixel_format=fmt, nbytes=w * h * (3 if fmt == 3 else 1), decode_ns=123)
        fields.update(override)
        return P.encode(P.ImageReady(**fields))

    OPS = {"ok_ops", "slow_open", "hang_read", "spin_read", "crash_read", "read_error", "read_ok_status", "read_wrong_op", "torn_frame",
           "bad_crc", "lying_width", "lying_nbytes", "seq_skip", "seq_repeat", "oversize_nbytes", "header_overwritten", "magic_corrupt",
           "version_corrupt", "no_image", "image_wrong_op", "garbage_after_open", "pixels_changed_after_crc", "exit_after_open_read",
           "format_mismatch", "unknown_pixel_format_in_header", "stale_fence_only", "stuck_stream_open"}

    def slot_handle(self, message):
        n = self.name
        if isinstance(message, P.Init) and n in ("no_attach", "header_nonce_wrong", "shm_refused", "exit_after_attach"):
            if n == "no_attach":                                # claims readiness but never attached the segment
                return self.hello(message), False
            if n == "shm_refused":
                return self.hello(message, status=int(P.Status.SHM_UNAVAILABLE)), False
            replies, done = super().handle(message)             # attaches and records the true handshake
            if n == "header_nonce_wrong":
                self.slot.record_handshake(message.nonce ^ 1, os.getpid())
            if n == "exit_after_attach":
                os._exit(0)
            return replies, done
        if isinstance(message, P.Open):
            if n == "hang_open":
                hang()
            if n == "spin_open":
                spin()
            if n == "crash_open":
                os._exit(139)
            if n == "open_error":
                return [P.encode(P.Result(message.op_id, int(P.Status.OPEN_FAILED)))], False
            if n == "open_unknown_status":
                return [P.encode(P.Result(message.op_id, 77))], False
            if n == "open_wrong_op":
                return [P.encode(P.Result(message.op_id + 1, 0))], False
            if n == "open_image_instead":
                return [self.ready(message.op_id, 1)], False
            if n == "slow_open":
                time.sleep(0.4)
            if n in self.OPS:
                return [P.encode(P.Result(message.op_id, 0))], False
        if isinstance(message, P.Read) and n in self.OPS:
            return self.read_reply(message)
        return None

    def read_reply(self, message):
        n = self.name
        if n in ("stuck_stream_open", "hang_read"):
            hang()
        if n == "spin_read":
            spin()
        if n == "crash_read":
            os._exit(139)
        if n == "exit_after_open_read":
            os._exit(0)
        if n == "read_error":
            return [P.encode(P.Result(message.op_id, int(P.Status.READ_FAILED)))], False
        if n == "read_ok_status":
            return [P.encode(P.Result(message.op_id, 0))], False
        if n == "read_wrong_op":
            return [P.encode(P.Result(message.op_id + 1, 9))], False
        if n == "no_image":
            return [], False                                    # answers nothing: the parent's read deadline must fire
        if n == "garbage_after_open":
            return [b"\x00\x01junk"], False
        self.seq += 1
        seq = self.seq
        if n == "seq_skip" and seq == 2:
            seq = self.seq = 3
        if n == "seq_repeat" and seq == 2:
            seq = self.seq = 1
        w, h, fmt = self.IMAGE
        pixels = self.synthetic(seq)
        self.slot.publish(seq, w, h, fmt, pixels)
        buf = self.slot.buffer
        override = {}
        if n == "torn_frame":
            struct.pack_into("<I", buf, 28, seq - 1 if seq > 1 else 0xFFFF)          # end fence behind the begin fence
        elif n == "stale_fence_only":
            struct.pack_into("<I", buf, 24, seq + 1)
        elif n == "bad_crc":
            struct.pack_into("<I", buf, 48, 0x12345678)
        elif n == "pixels_changed_after_crc":
            buf[64] = (buf[64] + 1) % 256                                           # CRC no longer matches the pixels
        elif n == "lying_width":
            override["width"] = w + 1
        elif n == "lying_nbytes":
            override["nbytes"] = len(pixels) - 3
        elif n == "oversize_nbytes":
            override.update(nbytes=10 ** 7, width=2000, height=1667)
        elif n == "format_mismatch":
            override["pixel_format"] = 1
        elif n == "header_overwritten":
            struct.pack_into("<Q", buf, 12, 1234)                                    # the handshake identity changed
        elif n == "magic_corrupt":
            buf[0:4] = b"XXXX"
        elif n == "version_corrupt":
            struct.pack_into("<H", buf, 4, 9)
        elif n == "unknown_pixel_format_in_header":
            buf[40] = 7
        if n == "image_wrong_op":
            override["op_id"] = message.op_id + 1
        return [self.ready(message.op_id, seq, **override)], False

    def handle(self, message):
        n = self.name
        special = self.slot_handle(message)
        if special is not None:
            return special
        if isinstance(message, P.Init):
            if self.report:
                self.write_report()
            if n == "no_hello":
                hang()
            if n == "exit_before_hello":
                os._exit(0)
            if n == "crash_before_hello":
                os._exit(139)
            if n == "spin_before_hello":
                spin()
            variants = {
                "python_unsupported": dict(status=int(P.Status.PYTHON_UNSUPPORTED)),
                "known_error_status": dict(status=int(P.Status.WORKER_INTERNAL)),
                "unknown_status": dict(status=77),
                "wrong_nonce": dict(nonce=message.nonce ^ 1),
                "wrong_version": dict(proto_version=2),
                "wrong_table_version": dict(status_table_version=2),
            }
            if n in variants:
                return self.hello(message, **variants[n]), False
            if n == "garbage_hello":
                return [b"junk"], False
            if n == "oversize_hello":
                return [b"x" * 5000], False
            if n == "truncated_hello":
                return [self.hello(message)[0][:-2]], False
            if n == "trailing_hello":
                return [self.hello(message)[0] + b"\0"], False
            if n == "pong_instead_of_hello":
                return [P.encode(P.Pong(1))], False
            if n == "reserved_bits_hello":
                raw = bytearray(self.hello(message)[0])
                raw[7] = 1
                return [bytes(raw)], False
        if isinstance(message, P.Ping):
            self.pings += 1
            if n == "no_pong":
                hang()
            if n == "wrong_pong_op":
                return [P.encode(P.Pong(message.op_id + 1))], False
            if n == "die_on_second_ping" and self.pings == 2:
                os._exit(0)
            if n == "hang_on_second_ping" and self.pings == 2:
                hang()
        if isinstance(message, P.Close):
            if n == "hang_on_close":
                hang()
            if n == "ignore_sigterm_hang_on_close":
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                hang()
            if n == "spin_on_close":
                spin()
            if n == "exit_without_reply_on_close":
                os._exit(0)
        return super().handle(message)

    def after_reply(self) -> None:
        if self.name == "stuck_after_ready" and self.pings == 1:
            hang()                                          # main thread stuck; the reader thread must still end the process


def main() -> int:
    values: dict[str, str] = {}
    args = iter(sys.argv[1:])
    for flag in args:
        if flag in ("--ctl", "--proto", "--fixture", "--report"):
            values[flag] = next(args, "")
    if values.get("--fixture") == "ignore_sigterm_from_start":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    return serve(_adopt(int(values["--ctl"])), Fixture(values.get("--fixture", "ok"), values.get("--report")))


if __name__ == "__main__":
    sys.exit(main())
