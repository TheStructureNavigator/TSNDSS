"""Windows handle-count diagnostic, round 1 (DB-03 Wave 4B-3b). DIAGNOSTIC ONLY: not part of the product, not imported by any code.

Question: the test ``SteadyStateTests.test_failed_start_cycles_do_not_leak`` once saw one extra handle (132 x4, then 133 x2) for the
fixture ``wrong_nonce``. Is that (H1) unrelated noise in the test process, (H2) delayed cleanup of garbage, (H3) a real leak, (H4) an
effect of earlier tests, or (H5) a delay in the operating system after TerminateProcess?

This round measures only what the process can see about itself:
  * the handle count of THIS process (GetProcessHandleCount; /proc/self/fd on Linux for a dry run);
  * the live Python objects that are POTENTIALLY related to handles (Popen, Connection, WorkerProcess, ...): a census of candidates,
    not a list of confirmed open handles -- their presence alone is NOT evidence of a leak;
  * how many garbage collections ran during each step;
  * the number of threads.
It reads nothing about other processes, calls no NT API, changes no setting, writes no file, opens no network connection and never
touches an RTSP address, a device or OpenCV. It starts only (a) the project's own fixture workers (``tests/process_fixtures.py``),
which the project's tests start anyway, and (b) ``python -c pass`` as a control. The garbage collector is NOT disabled in this round.

Run from the repository root (branch feat/db03-wave4b3b-parent), Python 3.13:
    py -3.13 tools\\windows_handle_diag\\handle_diag_round1.py > handle_diag_round1.txt 2>&1
Expected run time on Windows: about 1 to 2 minutes. BUDGET_S (240 s) is a budget that is checked between operations and between
phases, NOT a hard timeout: one operation in progress (a cycle takes a few seconds at most; the control subprocess has a 30 s
timeout) is allowed to finish, so the total can exceed the budget by about that much. When the budget is used up the remaining
phases are skipped and the script says so.
Stop at any time with Ctrl+C: the current worker is ended, any remaining worker is stopped, partial results are printed. If the
console is closed instead, a worker ends by itself when its parent disappears (its lifeline watchdog).

Reading the result: steps in the idle phases or in the plain-subprocess control are a hint about background noise in the test process.
They do NOT prove where a step in the wrong_nonce phase came from. The verdict is taken against the criteria in the report.
"""

import gc
import os
import subprocess
import sys
import threading
import time

BUDGET_S = 240.0                             # checked between operations and phases; not a hard timeout (see the module docstring)
CYCLES = 40
CONTROL_CYCLES = 25
IDLE_SAMPLES = 40
SETTLE_AFTER_S = (0, 0.5, 1, 2, 5)          # cumulative offsets after the last cycle
STARTED = time.monotonic()

if sys.version_info < (3, 13):
    sys.exit("needs Python 3.13")
sys.path.insert(0, os.getcwd())
if not (os.path.isdir("tsn_dss") and os.path.isdir("tests")):
    sys.exit("run this from the repository root")

from tsn_dss.engine.opencv_isolated_decoder import WorkerConfig, WorkerError, WorkerProcess, live_worker_count, stop_all_workers  # noqa: E402

FAST = WorkerConfig(start_deadline_s=1.0, ping_deadline_s=0.5, close_graceful_s=0.3, terminate_wait_s=0.5, kill_wait_s=0.5)
WIN = sys.platform == "win32"

if WIN:
    import ctypes
    from ctypes import wintypes

    _k = ctypes.WinDLL("kernel32", use_last_error=True)
    _k.GetCurrentProcess.restype = wintypes.HANDLE
    _k.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k.GetProcessHandleCount.restype = wintypes.BOOL

    def count():
        value = wintypes.DWORD(0)
        return int(value.value) if _k.GetProcessHandleCount(_k.GetCurrentProcess(), ctypes.byref(value)) else None
else:
    def count():
        return len(os.listdir("/proc/self/fd"))

# --- garbage-collection observer: how many collections (per generation) ran, to correlate with a step in the count ---------------
GC_EVENTS = [0, 0, 0]


def _gc_callback(phase, info):
    if phase == "stop":
        GC_EVENTS[info["generation"]] += 1


gc.callbacks.append(_gc_callback)

OWNERS = ("Popen", "Handle", "Overlapped", "PipeConnection", "Connection", "WorkerProcess", "SharedMemory", "Thread")


def census():
    """Count live objects of classes that are POTENTIALLY related to handles, by class name (non-zero entries only).

    These are candidates, not confirmed open handles: an object can be alive without holding an open handle and the other way round.
    Their presence alone is not evidence of a leak; it is only compared with the handle count and with the garbage-collection events."""
    counts = {}
    for obj in gc.get_objects():
        name = type(obj).__name__
        if name in OWNERS:
            counts[name] = counts.get(name, 0) + 1
    return counts


def over_budget():
    return time.monotonic() - STARTED > BUDGET_S


def skip_for_budget(label):
    """Checked between phases: True (and a message) when the budget is used up, so the phase is skipped."""
    if over_budget():
        print(f"[{label}] SKIPPED: the {BUDGET_S:.0f} s budget is used up (checked between phases, not a hard timeout)")
        return True
    return False


def cycle(fixture):
    worker = WorkerProcess(FAST, _entry_module="tests.process_fixtures", _extra_args=("--fixture", fixture))
    try:
        try:
            worker.start()
            worker.ping()
        except WorkerError:
            pass
    finally:
        worker.stop()


def phase(label, work, cycles, with_census):
    series, notes = [], []
    last_gc = list(GC_EVENTS)
    for i in range(cycles):
        if over_budget():
            print(f"[{label}] BUDGET EXCEEDED after {i} cycles; stopping this phase")
            break
        work()
        time.sleep(0.05)
        value = count()
        series.append(value)
        ran = [now - before for now, before in zip(GC_EVENTS, last_gc)]
        last_gc = list(GC_EVENTS)
        if with_census:
            notes.append((i + 1, value, ran, threading.active_count(), census()))
    print(f"[{label}] counts ({len(series)}): {series}")
    if series:
        steps = [(i + 2, series[i + 1] - series[i]) for i in range(len(series) - 1) if series[i + 1] != series[i]]
        print(f"[{label}] changes between consecutive samples (cycle, delta): {steps}")
        print(f"[{label}] first {series[0]}  last {series[-1]}  min {min(series)}  max {max(series)}  net {series[-1] - series[0]:+d}")
    if with_census:
        nonzero = [n for n in notes if n[4] or n[2][2] or n[3] != notes[0][3]]
        print(f"[{label}] samples with live handle-related candidate objects (not confirmed handles), a gen-2 collection, or a changed thread count "
              f"(cycle, count, collections per generation, threads, objects): {nonzero[:12]}{' ...' if len(nonzero) > 12 else ''}")
    return series


def main():
    print("python", sys.version.split()[0], "| platform", sys.platform, "| venv", sys.prefix != getattr(sys, "base_prefix", sys.prefix),
          "| pid", os.getpid(), "| executable", os.path.basename(sys.executable))
    print("gc thresholds", gc.get_threshold(), "| budget", BUDGET_S, "s")
    first = count()
    extra = os.open(os.devnull, os.O_RDONLY)
    try:
        raised = count()
    finally:
        os.close(extra)                          # closed even if the measurement raises
    if first is None or raised is None or not raised > first or count() != first:
        sys.exit(f"the handle counter does not react to one extra handle ({first}, {raised}); results would be meaningless")
    print(f"counter self-test: {first} -> {raised} -> {count()} (reacts to one extra handle)")

    after = None
    pre_idle = phase("1 idle before (no spawning)", lambda: None, IDLE_SAMPLES, False)
    cycles = [] if skip_for_budget("2 wrong_nonce cycles") else phase("2 wrong_nonce cycles", lambda: cycle("wrong_nonce"), CYCLES, True)

    if skip_for_budget("3 settle"):
        pass
    else:
        print("[3 settle after the last cycle] (the pending garbage and the operating system get time)")
        settle, waited = [], 0.0
        for offset in SETTLE_AFTER_S:
            time.sleep(max(0.0, offset - waited))
            waited = offset
            settle.append((offset, count()))
        print(f"[3 settle] (seconds after last cycle, count): {settle}")
        before = count()
        objects = census()
        collected = gc.collect()
        after = count()
        print(f"[3 settle] live handle-related candidate objects before collect (not confirmed handles): {objects}")
        print(f"[3 settle] gc.collect() -> collected {collected}; count before {before} after {after}; candidates after: {census()}")

    post_idle = [] if skip_for_budget("4 idle after") else phase("4 idle after (no spawning)", lambda: None, IDLE_SAMPLES, False)
    control = [] if skip_for_budget("5 control") else phase("5 control: plain python -c pass", lambda: subprocess.run(
        [sys.executable, "-c", "pass"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30), CONTROL_CYCLES, True)

    print("\n=== summary (hints for the human; the verdict is decided against the criteria in the report) ===")
    def drift(series):
        return (series[-1] - series[0]) if series else None
    print(f"idle drift before/after: {drift(pre_idle)} / {drift(post_idle)}   wrong_nonce drift: {drift(cycles)}   control drift: {drift(control)}")
    if cycles:
        ramp = cycles[-1] - cycles[1] if len(cycles) > 1 else 0
        print(f"HINT ramp over the wrong_nonce phase (last - second): {ramp:+d}  ({'looks like growth' if ramp >= 3 else 'no ramp'})")
        if after is not None:
            print(f"HINT back to the starting level after settle+collect: {after == cycles[0]} (start {cycles[0]}, now {after})")
    print("NOTE steps in the idle phases or the subprocess control hint at background noise; they do not prove where a step in the wrong_nonce phase came from.")
    print(f"HINT live worker slots at the end: {live_worker_count()} (must be 0)")
    print("elapsed %.1f s" % (time.monotonic() - STARTED))


try:
    main()
except KeyboardInterrupt:
    print("\ninterrupted by the operator; partial results above")
finally:
    stop_all_workers()
