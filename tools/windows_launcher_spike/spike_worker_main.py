"""Worker half of the DB-03 Wave 4B Windows launcher spike.

Diagnostic only. No OpenCV, no RTSP, no shared memory, no network, no real addresses or secrets.
It is started by spike_launcher.py as:   python -m spike_worker_main --ctl <handle>
"""
import os
import queue
import sys
import threading
import time

WINDOWS = sys.platform == "win32"


def adopt(handle):
    if WINDOWS:
        from multiprocessing.connection import PipeConnection
        return PipeConnection(handle)
    from multiprocessing.connection import Connection
    return Connection(handle)


def own_handle_count():
    """Informational: how many OS handles this (child) process holds. None if unavailable."""
    try:
        if WINDOWS:
            import ctypes
            from ctypes import wintypes
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.GetCurrentProcess.restype = wintypes.HANDLE
            k.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            k.GetProcessHandleCount.restype = wintypes.BOOL
            count = wintypes.DWORD(0)
            return int(count.value) if k.GetProcessHandleCount(k.GetCurrentProcess(), ctypes.byref(count)) else None
        return len(os.listdir("/proc/self/fd"))
    except Exception:
        return None


def main():
    if len(sys.argv) != 3 or sys.argv[1] != "--ctl":
        sys.exit(64)
    ctl = adopt(int(sys.argv[2]))
    commands = queue.Queue()

    def reader():
        # Lifeline watchdog: if the parent end disappears, leave at once, even if the main thread is stuck.
        try:
            while True:
                commands.put(ctl.recv_bytes(4096))
        except (EOFError, OSError):
            os._exit(3)

    threading.Thread(target=reader, daemon=True).start()

    while True:
        msg = commands.get()
        if msg.startswith(b"INIT:"):
            nonce = msg[5:]
            info = {
                "pid": os.getpid(),
                "nonce": nonce.decode("ascii", "replace"),
                "python": ".".join(map(str, sys.version_info[:3])),
                "main_file": os.path.basename(getattr(sys.modules["__main__"], "__file__", "") or ""),
                "parent_main_loaded": "spike_launcher" in sys.modules,
                "secret_probe_visible": os.environ.get("SPIKE_SECRET_PROBE") is not None,
                "argv_is_numeric_only": all(a == "--ctl" or a.isdigit() for a in sys.argv[1:]),
                "has_systemroot": (os.environ.get("SYSTEMROOT") is not None) if WINDOWS else True,
                "child_handle_count": own_handle_count(),
            }
            ctl.send_bytes(("HELLO:" + repr(info)).encode("utf-8"))
        elif msg == b"PING":
            ctl.send_bytes(b"PONG")
        elif msg == b"HANG":
            time.sleep(10 ** 6)          # main thread stuck in a call that never returns
        elif msg == b"EXIT":
            ctl.close()
            sys.exit(0)


if __name__ == "__main__":
    main()
