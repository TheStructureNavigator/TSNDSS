"""DB-03 Wave 4B - Windows launcher spike (parent half). Diagnostic only; not part of TSN DSS.

Checks, with no OpenCV, no RTSP, no shared memory, no network and no real addresses or secrets:
  C1  environment is as expected and an overlapped duplex Pipe can be created
  C2  a dedicated worker module is started with subprocess.Popen and receives the pipe handle
  C3  two-way handshake (nonce) and a PING/PONG round trip
  C4  the parent's main module is NOT executed again in the child
  C5  the child sees an explicit environment (a planted probe variable does not leak) and numeric-only argv
  C6  bounded poll: a stuck worker never blocks the parent past its deadline
  C7  terminate -> bounded wait -> kill -> bounded wait ends the stuck worker
  C8  after the worker is gone the parent sees EOF (never a hang) on the pipe
  C9  lifeline: closing the parent's end makes a worker with a stuck main thread exit by itself
  C10a the handle counter itself works (typed Win32 call, reacts to one extra handle, rejects an invalid handle)
  C10 handles are released: process handle/fd count returns to baseline after 5 full cycles
       (UNVERIFIED, never PASS, when the counter cannot be trusted)

Run:   py spike_launcher.py            (or: python spike_launcher.py)
Opts:  --report FILE   also write the report to FILE
       --debug-stderr  keep the worker's stderr in a temp file and print it when a worker fails to start
       --counter-only  run only the handle-counter self-check (C10a) and exit
"""
import argparse
import os
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WINDOWS = sys.platform == "win32"
MARKER = os.path.join(tempfile.gettempdir(), "tsn_spike_main_marker.txt")

# ---- C4 evidence: this line runs every time THIS file is executed as __main__ (or imported). ----
with open(MARKER, "a", encoding="ascii") as _fh:
    _fh.write("parent-main-executed pid=%d name=%s\n" % (os.getpid(), __name__))

from multiprocessing.connection import Pipe  # noqa: E402

LINES = []
RESULTS = []


def scrub(text):
    """Keep the report free of personal paths (user name in profile, temp and script directories)."""
    for real, label in ((HERE, "<spike-dir>"), (tempfile.gettempdir(), "<temp>"), (os.path.expanduser("~"), "<home>")):
        if real and len(real) > 3:
            text = text.replace(real, label).replace(real.replace("\\", "/"), label)
    return text


def say(text=""):
    text = scrub(text)
    LINES.append(text)
    print(text, flush=True)


def record(name, ok, detail=""):
    record_status(name, "PASS" if ok else "FAIL", detail)


def record_status(name, status, detail=""):
    """status is PASS, FAIL or UNVERIFIED (the check could not be carried out; never counted as PASS)."""
    RESULTS.append((name, status))
    say("[%s] %s%s" % (status, name, (" - " + detail) if detail else ""))


def child_env():
    keys = ["PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "PATHEXT", "COMSPEC", "LANG", "LC_ALL"]
    env = {k: os.environ[k] for k in keys if k in os.environ}
    env["PYTHONPATH"] = HERE
    return env


class Reading:
    """One handle-count measurement. ``count`` is None when the counter is unavailable; there is no sentinel number."""

    def __init__(self, count, method, errors):
        self.count, self.method, self.errors = count, method, errors


def _win_api():
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)          # private binding: no shared argtypes with other code
    k.GetCurrentProcess.argtypes = []
    k.GetCurrentProcess.restype = wintypes.HANDLE               # a pseudo-handle (-1) must not be truncated to a 32-bit int
    k.GetCurrentProcessId.argtypes = []
    k.GetCurrentProcessId.restype = wintypes.DWORD
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k.GetProcessHandleCount.restype = wintypes.BOOL
    k.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    k.CreateEventW.restype = wintypes.HANDLE
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    return k, ctypes, wintypes


def win_query(handle):
    """GetProcessHandleCount for ``handle``. Returns (count, 0) or (None, GetLastError()); a failure is never a number."""
    k, ct, wt = _win_api()
    count = wt.DWORD(0)
    ct.set_last_error(0)
    ok = k.GetProcessHandleCount(handle, ct.byref(count))
    if not ok:
        return None, ct.get_last_error()
    return int(count.value), 0


def read_handle_count():
    errors = []
    if WINDOWS:
        k, ct, wt = _win_api()
        # Method 1: the current-process pseudo-handle, correctly typed.
        count, err = win_query(k.GetCurrentProcess())
        if count is not None:
            return Reading(count, "pseudo-handle", errors)
        errors.append("pseudo-handle: error %d" % err)
        # Method 2: a real handle to this process (query-limited access). Closed again before returning.
        real = k.OpenProcess(0x1000, False, k.GetCurrentProcessId())
        if not real:
            errors.append("OpenProcess: error %d" % ct.get_last_error())
            return Reading(None, "none", errors)
        try:
            count, err = win_query(real)
        finally:
            k.CloseHandle(real)
        if count is not None:
            return Reading(count, "OpenProcess handle (the query handle itself is included in the count)", errors)
        errors.append("OpenProcess handle: error %d" % err)
        return Reading(None, "none", errors)
    if os.path.isdir("/proc/self/fd"):
        return Reading(len(os.listdir("/proc/self/fd")), "/proc/self/fd", errors)
    errors.append("no /proc/self/fd on this platform")
    return Reading(None, "none", errors)


def legacy_counter_probe():
    """The call exactly as the first spike version made it. Informational: shows what that call returned."""
    if not WINDOWS:
        return "n/a (not Windows)"
    try:
        import ctypes
        count = ctypes.c_ulong(0)
        ok = ctypes.windll.kernel32.GetProcessHandleCount(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(count))
        return "returned %r, count %d, GetLastError %d" % (ok, count.value, ctypes.GetLastError())
    except Exception as exc:
        return "raised %s" % type(exc).__name__


def counter_self_check():
    """Returns (status, detail). PASS only when the counter is shown to work; otherwise UNVERIFIED or FAIL."""
    first = read_handle_count()
    if first.count is None or first.count <= 0:
        return "UNVERIFIED", "counter unavailable (%s); legacy call: %s" % ("; ".join(first.errors) or "no reading", legacy_counter_probe())
    notes = ["method %s, reading %d" % (first.method, first.count), "legacy call: %s" % legacy_counter_probe()]
    # Sensitivity: one extra handle must be visible, and closing it must bring the count back.
    if WINDOWS:
        k, ct, wt = _win_api()
        extra = k.CreateEventW(None, True, False, None)
        if not extra:
            return "UNVERIFIED", "could not create a test handle (error %d); " % ct.get_last_error() + "; ".join(notes)
        raised = read_handle_count().count
        k.CloseHandle(extra)
    else:
        extra = os.open(os.devnull, os.O_RDONLY)
        raised = read_handle_count().count
        os.close(extra)
    restored = read_handle_count().count
    if raised is None or restored is None:
        return "UNVERIFIED", "counter became unavailable during the sensitivity test; " + "; ".join(notes)
    if raised < first.count + 1 or restored != first.count:
        return "FAIL", "counter does not react to one extra handle (before %d, with extra %s, after close %s); " % (
            first.count, raised, restored) + "; ".join(notes)
    notes.append("one extra handle: %d -> %d -> %d" % (first.count, raised, restored))
    # Invalid-handle control (Windows): a bad handle must be reported as an error, never as a number.
    if WINDOWS:
        k, ct, wt = _win_api()
        bad, err = win_query(wt.HANDLE(0))
        if bad is not None:
            return "FAIL", "an invalid handle was answered with a count (%r): the wrapper cannot tell failures from values; " % (bad,) + "; ".join(notes)
        notes.append("invalid handle rejected (GetLastError %d, expected 6)" % err)
    else:
        notes.append("invalid-handle control: n/a on POSIX")
    return "PASS", "; ".join(notes)


class Worker:
    def __init__(self, debug_stderr=False):
        self.parent_end, child_end = Pipe(duplex=True)
        self.stderr_path = None
        stderr = subprocess.DEVNULL
        if debug_stderr:
            self.stderr_path = os.path.join(tempfile.gettempdir(), "tsn_spike_worker_stderr_%d.txt" % os.getpid())
            stderr = open(self.stderr_path, "wb")
        handle = child_end.fileno()
        argv = [sys.executable, "-m", "spike_worker_main", "--ctl", str(handle)]
        kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr, env=child_env(), cwd=HERE, close_fds=True)
        if WINDOWS:
            os.set_handle_inheritable(handle, True)
            si = subprocess.STARTUPINFO(lpAttributeList={"handle_list": [handle]})
            kwargs["startupinfo"] = si
        else:
            kwargs["pass_fds"] = (handle,)
        try:
            self.popen = subprocess.Popen(argv, **kwargs)
        finally:
            child_end.close()                 # the parent MUST drop its copy of the child's end
            if debug_stderr:
                stderr.close()
        self.child_end_closed = child_end.closed

    def request(self, payload, deadline):
        self.parent_end.send_bytes(payload)
        if not self.parent_end.poll(deadline):
            return None
        return self.parent_end.recv_bytes(4096)

    def contain(self, term_wait=5.0, kill_wait=5.0):
        steps, start = [], time.perf_counter()
        if self.popen.poll() is None:
            self.popen.terminate()
            steps.append("terminate")
            try:
                self.popen.wait(timeout=term_wait)
            except subprocess.TimeoutExpired:
                pass
        if self.popen.poll() is None:
            self.popen.kill()
            steps.append("kill")
            try:
                self.popen.wait(timeout=kill_wait)
            except subprocess.TimeoutExpired:
                pass
        return steps, self.popen.poll(), time.perf_counter() - start

    def close(self):
        try:
            self.parent_end.close()
        except Exception:
            pass


def parse_hello(raw):
    if not raw or not raw.startswith(b"HELLO:"):
        return None
    import ast
    return ast.literal_eval(raw[6:].decode("utf-8"))


def run(args):
    say("DB-03 Wave 4B Windows launcher spike")
    say("python %s | %s | %d-bit | %s" % (sys.version.split()[0], sys.platform, 64 if sys.maxsize > 2 ** 32 else 32, os.path.basename(sys.executable)))
    say("note: Python >= 3.13 is required later for the isolated decoder (informational here): %s"
        % ("OK" if sys.version_info >= (3, 13) else "THIS INTERPRETER IS OLDER"))
    os.environ["SPIKE_SECRET_PROBE"] = "planted-probe-not-a-secret"
    say()

    # C1
    try:
        a, b = Pipe(duplex=True)
        kind = type(a).__name__
        a.close(); b.close()
        record("C1 environment and pipe creation", True, "connection type %s (expected PipeConnection on Windows)" % kind)
    except Exception as exc:
        record("C1 environment and pipe creation", False, "%s: %s" % (type(exc).__name__, exc))
        return

    baseline = read_handle_count()

    # C2/C3/C4/C5 on one worker
    try:
        w = Worker(args.debug_stderr)
    except Exception as exc:
        record("C2 Popen with inherited handle", False, "%s: %s" % (type(exc).__name__, exc))
        return
    nonce = "n%d" % (int(time.time() * 1000) % 10 ** 9)
    try:
        raw = w.request(b"INIT:" + nonce.encode("ascii"), 15.0)
        hello = parse_hello(raw)
        if hello is None:
            detail = "no HELLO within 15 s; worker exit code %r" % (w.popen.poll(),)
            if w.stderr_path and os.path.exists(w.stderr_path):
                with open(w.stderr_path, "rb") as fh:
                    detail += "\n   worker stderr: " + fh.read().decode("utf-8", "replace").strip()[-800:]
            elif not args.debug_stderr:
                detail += " (re-run with --debug-stderr to see why the worker died)"
            record("C2 Popen with inherited handle", False, detail)
            w.contain(); w.close()
            return
        record("C2 Popen with inherited handle", True, "worker pid %d started; parent closed its copy of the child end: %s" % (hello["pid"], w.child_end_closed))
        record("C3 handshake", hello["nonce"] == nonce, "nonce echoed %s" % ("correctly" if hello["nonce"] == nonce else "WRONG: %r" % hello["nonce"]))
        pong = w.request(b"PING", 5.0)
        record("C3 PING/PONG", pong == b"PONG", "reply %r" % (pong,))
        markers = open(MARKER, encoding="ascii").read().splitlines()
        mine = [m for m in markers if m.startswith("parent-main-executed pid=%d " % os.getpid())]
        others = [m for m in markers if m.startswith("parent-main-executed") and m not in mine and ("pid=%d " % hello["pid"]) in m]
        record("C4 parent main not re-executed in the child",
               len(others) == 0 and not hello["parent_main_loaded"],
               "worker main file %r; parent module loaded in worker: %s; marker lines written by the worker pid: %d"
               % (hello["main_file"], hello["parent_main_loaded"], len(others)))
        env_ok = (not hello["secret_probe_visible"]) and hello["argv_is_numeric_only"] and hello["has_systemroot"]
        record("C5 explicit environment and numeric-only argv", env_ok,
               "probe variable leaked: %s; argv numeric only: %s; SYSTEMROOT present (needed on Windows): %s"
               % (hello["secret_probe_visible"], hello["argv_is_numeric_only"], hello["has_systemroot"]))

        # C6: stuck worker, bounded poll
        w.parent_end.send_bytes(b"HANG")
        t0 = time.perf_counter()
        answered = w.parent_end.poll(0.5)
        waited = time.perf_counter() - t0
        record("C6 bounded poll on a stuck worker", (not answered) and 0.4 <= waited < 2.0,
               "poll(0.5) returned %s after %.2f s (expected False, about 0.5 s)" % (answered, waited))

        # C7: containment
        steps, code, took = w.contain()
        record("C7 terminate/kill containment", code is not None and took < 11.0,
               "steps %s, exit code %r, %.2f s" % (steps, code, took))

        # C8: EOF after death
        t0 = time.perf_counter()
        outcome = "no exception"
        try:
            ready = w.parent_end.poll(3.0)
            if ready:
                w.parent_end.recv_bytes(4096)
        except EOFError:
            outcome = "EOFError"
        except OSError as exc:
            outcome = "OSError(%s)" % type(exc).__name__
        elapsed = time.perf_counter() - t0
        record("C8 EOF visible after worker death", outcome in ("EOFError",) or outcome.startswith("OSError"),
               "%s after %.2f s (a hang or 'no exception' would be a FAIL)" % (outcome, elapsed))
    except Exception as exc:
        record("C2-C8 sequence", False, "%s: %s" % (type(exc).__name__, exc))
        w.contain()
    finally:
        w.close()

    # C9: lifeline
    try:
        w2 = Worker(args.debug_stderr)
        raw = w2.request(b"INIT:lifeline", 15.0)
        if parse_hello(raw) is None:
            record("C9 lifeline watchdog", False, "second worker did not start (exit code %r)" % (w2.popen.poll(),))
            w2.contain()
        else:
            w2.parent_end.send_bytes(b"HANG")
            time.sleep(0.5)
            w2.parent_end.close()             # simulates the parent disappearing
            try:
                code = w2.popen.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                code = None
            if code is None:
                w2.contain()
            record("C9 lifeline watchdog", code == 3,
                   "worker exit code %r (3 = left by itself when the pipe closed; None/other = orphan risk)" % (code,))
        w2.close()
    except Exception as exc:
        record("C9 lifeline watchdog", False, "%s: %s" % (type(exc).__name__, exc))

    # C10a: is the counter itself trustworthy?
    status, detail = counter_self_check()
    record_status("C10a handle counter self-check", status, detail)

    # C10: handle release over 5 cycles
    try:
        for _ in range(5):
            wc = Worker(args.debug_stderr)
            if parse_hello(wc.request(b"INIT:cycle", 15.0)) is None:
                raise RuntimeError("cycle worker did not start")
            wc.contain()
            wc.close()
        time.sleep(0.3)
        after = read_handle_count()
        if status != "PASS" or baseline.count is None or after.count is None:
            record_status("C10 handles released", "UNVERIFIED",
                          "no usable measurement (counter self-check %s; before %s, after %s). "
                          "This is NOT evidence that handles are released." % (status, baseline.count, after.count))
        else:
            growth = after.count - baseline.count
            record("C10 handles released", growth <= 2,
                   "open handle/fd count before %d, after 5 cycles %d (growth %d, allowed <= 2; method %s)"
                   % (baseline.count, after.count, growth, after.method))
    except Exception as exc:
        record("C10 handles released", False, "%s: %s" % (type(exc).__name__, exc))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report")
    parser.add_argument("--debug-stderr", action="store_true")
    parser.add_argument("--counter-only", action="store_true")
    args = parser.parse_args()

    def too_long():
        print("SPIKE TIMEOUT: the whole run exceeded 150 s; something blocked", flush=True)
        os._exit(2)

    timer = threading.Timer(150.0, too_long)
    timer.daemon = True
    timer.start()
    open(MARKER, "w").close()
    # the marker file was truncated after this module already wrote its own line; write it again for a clean count
    with open(MARKER, "a", encoding="ascii") as fh:
        fh.write("parent-main-executed pid=%d name=%s\n" % (os.getpid(), __name__))
    if args.counter_only:
        status, detail = counter_self_check()
        record_status("C10a handle counter self-check", status, detail)
    else:
        run(args)
    passed = sum(1 for _, st in RESULTS if st == "PASS")
    failed = sum(1 for _, st in RESULTS if st == "FAIL")
    unverified = sum(1 for _, st in RESULTS if st == "UNVERIFIED")
    say()
    if failed:
        verdict = "FAIL"
    elif unverified or not RESULTS:
        verdict = "UNVERIFIED"
    else:
        verdict = "PASS"
    say("RESULT: %s (%d/%d checks passed, %d failed, %d unverified)" % (verdict, passed, len(RESULTS), failed, unverified))
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write("\n".join(LINES) + "\n")
    timer.cancel()
    sys.exit(0 if verdict == "PASS" else 1)


if __name__ == "__main__":
    main()
