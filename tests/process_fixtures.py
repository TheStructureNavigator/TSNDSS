"""Test-only worker entry for DB-03 Wave 4B: ``python -P -m tests.process_fixtures --ctl H --proto 1 --fixture NAME``.

It reuses the production ``serve`` loop (reader-thread watchdog included) with handlers that misbehave on purpose.
Started through the launcher's private test seam; production code has no way to select a fixture. No network, no OpenCV.
"""

from __future__ import annotations

import json
import os
import signal
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

    def handle(self, message):
        n = self.name
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
