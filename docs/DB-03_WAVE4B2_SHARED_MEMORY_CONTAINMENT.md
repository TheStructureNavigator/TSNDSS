# DB-03 Wave 4B-2 — shared memory, hard deadlines and process containment (offline)

Status: **implemented offline; verified on Linux (CPython 3.13) only; not yet run on Windows.** Base: `origin/master` `48abca3` (4B-1 accepted on Windows offline). Implements phase 4B-2 of `docs/DB-03_WAVE4B_PROCESS_ISOLATION_DESIGN.md` (revision 3) with the owner decisions of the 4B-2 review (below). **No decoder, no OpenCV, no RTSP, no network, no device**; `OPEN`/`READ` and every image exist only in fixture workers — the production worker still answers them with `invalid_state`. Nothing is connected to the preview manager (4B-3). No hardware claim.

## Owner decisions applied

| # | Decision | Where |
|---|---|---|
| 1 | Layout v2: `pixel_crc32` u32 at offset 48; `LAYOUT_VERSION` 1 → 2; other versions are refused; the CRC is checked on the **parent's own copy**; fences are still checked before **and** after the copy; the CRC is not a defence against a hostile worker | `segment.py`, `shm.py`, `process.py::_take_image` |
| 2 | Production worker: handshake, segment attach, PING, cleanup; production `OPEN`/`READ` stay `invalid_state`; real `OPEN`/`READ` and their deadlines are tested through fixtures only | `worker_main.py`, `tests/process_fixtures.py` |
| 3 | D10 with condition: ≤ 2 workers per host process, counted until the process is *confirmed* gone; an unreapable worker keeps its slot; `containment_failed` is **not** a permanent loss — the slot returns after a confirmed exit; never freed by a timeout, `kill()` or a lost pipe | `process.py` (registry) |
| 4 | 4B-1 tests updated narrowly: exact file set, shared-memory ban narrowed to `shm.py`, `atexit` only in `process.py`, `zlib` only in `shm.py` | `tests/test_isolated_boundaries.py` |

D13 is unchanged (codes 1–21 and 255, status table version 1); no code was added. Parent-side conditions use only existing categories (`worker_limit`, `shm_unavailable`, `open_deadline_exceeded`, `read_deadline_exceeded`, …); a misuse by the caller (read before open, second open) raises `InvalidTransition` and does not fail the worker.

## What exists

| File | Change |
|---|---|
| `shm.py` (new) | `ParentSegment` (creates, reads a validated header **copy**, copies pixels to `bytes`, unlinks the name, closes; no view escapes), `WorkerSegment` (attach with `track=False`, record handshake, `publish`; never creates or unlinks), `crc32_of`. The only module with `multiprocessing.shared_memory` and `zlib`. |
| `segment.py` | Layout v2 (`crc32` field, reserved bytes now 52..63), `unpack_header(..., segment_length=)` for header-only reads. |
| `worker_main.py` | `ProductionHandler` attaches the segment named in `INIT` and writes nonce and its pid into the shared header **before** `HELLO`; attach failure → `shm_unavailable`. |
| `process.py` | `WorkerConfig` gains `open_margin_s`, `read_margin_s`, `slot_bytes` (0 = no segment, the 4B-1 behaviour), `max_width`, `max_height`, `max_live_workers`; segment creation and handshake verification; `open()` / `read()` with parent-enforced deadlines; process-wide registry (`live_worker_count`, `reclaim_abandoned_workers`, `stop_all_workers`, `atexit`); `StopReport` gains `uncertain`, `segment_released`, `slot_released`; `WorkerImage`. |
| tests | `test_isolated_slot.py` (54), `test_isolated_limit.py` (21), `test_opencv_isolated_wave4b2_traceability.py` (7), probes and fixtures; manifest `opencv_isolated_wave4b2_traceability.json` (PRV-ISO-018..033). |

## Shared-memory protocol as implemented

* **Ownership.** The parent creates the segment (random 128-bit hex name) *before* spawning; it is the only side that ever unlinks it. The worker attaches with `track=False` (Python ≥ 3.13), so the worker's resource tracker cannot destroy the parent's segment when the worker exits (test: a separate process attaches and exits; the name survives and stderr is empty).
* **Handshake** (all inside `start_deadline_s`): `INIT` (version, table version, nonce, segment name, slot size, limits) → worker attaches, validates magic/version/slot size, writes `nonce` and `pid` into the header → `HELLO` echoes the nonce → the parent checks the echoed nonce **and** the nonce read from the shared header (proof of a shared mapping), and that both fences are zero → `PING`/`PONG` → only then READY and, on POSIX, **the name is unlinked**. The header pid is recorded as `worker_pid` for diagnosis and is **never** compared with `popen.pid` (a Windows venv launcher can make them differ).
* **Slot ownership** is decided by message order, never by looking into the segment: the worker may write only between receiving `READ` and sending `IMAGE_READY`; the parent reads only after `IMAGE_READY` for the outstanding `op_id`.
* **Read path (`_take_image`).** sequence must be exactly `last + 1` → header copy #1 validated (magic, version, slot size) → nonce/pid identity → `validate_image` (fences equal the sequence, header repeats the message, `nbytes = w·h·bpp`, ≤ slot, ≤ limits) → pixel copy into parent `bytes` → header copy #2 must equal copy #1 and the fences must still equal the sequence → CRC32 of the **copy** must equal the header CRC. Any failure → `worker_protocol_error`, no pixels are delivered, the worker is contained.
* **What the checks do not do.** A hostile worker can write plausible pixels and a matching CRC; nothing here stops that (design §13). A write to the pixels *after* the parent's copy cannot reach the delivered copy (test), and a write to a fence during the parent's ownership is caught by the second header read.

## Deadlines and containment (D12 values unchanged)

`open`: `open_timeout_ms` + `open_margin_s` (1.5 s). `read`: `read_timeout_ms` + `read_margin_s` (1.0 s). A miss → `open_deadline_exceeded` / `read_deadline_exceeded`, then the existing containment (graceful `CLOSE` only if idle, terminate → bounded wait → kill → bounded wait). Every wait is a 50 ms `poll` slice that also checks the stop flag. The parent is never blocked on the worker. Tested against a worker that hangs, one that spins in Python while holding the GIL, one that answers nothing, and one that is slow but within its deadline. Windows has a single termination step (`terminate` = `kill`).

**READY is not OPEN.** `decoder_open` becomes true only after a `RESULT` for `OPEN` with the outstanding `op_id` and status 0. Any other `RESULT` status maps through the D13 table; an unknown status, a wrong `op_id` or an image in answer to `OPEN` is a protocol violation.

## Concurrency model (no lock across a wait the worker can stretch)

* `_lock` is held only for state, counters and flag changes — never across `poll`, `recv`, `send`, a copy, `wait` or a sleep.
* An operation registers with `_io()` (counter under the lock). `stop()` sets the stop flag, runs containment, waits **at most 1.0 s** for operations to leave, and closes the pipe **and the parent's mapping** only when no operation is active. If one is still active, the **last operation to leave** closes them (`_io` finalizer). A thread can therefore never use a closed buffer; it sees `worker_ipc_lost` instead.
* The module-level registry lock guards only dictionary/list updates and `poll()` of abandoned processes (a non-blocking call).
* One operation at a time; a second one gets `InvalidTransition` without failing the worker. A terminated object is never reused (restart = new `WorkerProcess`, new segment, sequence from 1).

## Process limit (D10 with the owner's condition)

* A worker is **reserved before any resource is created** (no segment, no pipe, no `Popen`) and counted until `Popen.poll()` reports an exit, or until it is known that no process ever existed (spawn failure, refused start). This counts a worker from slightly *before* `Popen`; that is deliberate and conservative.
* `containment_failed`: the `Popen` moves to the abandoned list and **keeps its slot**; `StopReport.slot_released` is false and `uncertain` contains `process_unreaped`.
* Recovery: `reclaim_abandoned_workers()` (explicit) and every `start()` (automatic) release exactly those abandoned workers whose `poll()` confirms an exit; a `poll()` that raises is not evidence. Concurrent recovery releases each slot once.
* A forgotten, un-stopped worker stays registered (strong reference) until `stop()` or the exit handler — by design; the garbage collector cannot free a slot.

## Exit cleanup and parent death — what is and is not guaranteed

| Situation | Behaviour | Verified |
|---|---|---|
| Normal interpreter exit, worker alive | `atexit` handler (`stop_all_workers`, registered after `multiprocessing` was imported so it runs first) contains every live worker, bounded | Linux |
| Same, worker ignores SIGTERM and hangs on `CLOSE` | kill step; interpreter exits in < 15 s | Linux |
| Host dies hard (no `atexit`) after the handshake | worker ends through its reader-thread lifeline watchdog; **no segment name remains** (unlinked at handshake end) | Linux |
| Host dies hard **before** the handshake ends | the segment *name* exists on POSIX for that window; CPython's resource tracker (a helper process) removed it in the test. **This is a property of CPython observed on Linux, not a guarantee of this code.** | Linux |
| Windows | no unlink; the mapping disappears with its last handle (process termination closes it) | **not run** |
| Worker holds the GIL in a stuck call, host dies | the watchdog thread cannot run; the worker survives until killed (L8, unchanged) | not testable offline |

`atexit` is best effort: it does not run when the host crashes, is killed or calls `os._exit`. No full cleanup guarantee is claimed.

## Findings and risks reported (none required a contract change)

1. **Layout v2** is a deliberate break: v1 headers are refused. No segment ever existed under v1, so no migration is needed.
2. **Resource tracker (POSIX).** The first `ParentSegment` starts CPython's tracker helper in the host (a third process, not counted by D10). Observed: no leak warnings with `track=False` in the worker; unlinking in the parent unregisters it. It is the only backstop for the pre-handshake window.
3. **Importing the package now imports `multiprocessing.shared_memory`** (no segment is created at import). The 4B-1 import test no longer forbids that module name; it still forbids OpenCV, NumPy and PIL.
4. **Registry placement.** The design put the live-worker registry in `decoder.py` (4B-3); it lives in `process.py` because the limit is a launcher property.
5. **`StopReport.uncertain` tokens**: `stream_terminated` (a stream was open and the process was ended by force), `operation_interrupted`, `process_unreaped`, `segment_not_released`. They state what the parent cannot know (design U5); nothing about the device is inferred.
6. **Windows venv launcher — analysis (unverified on Windows; the production launcher is unchanged).**
   * *Can `sys.executable` start an intermediate process?* Yes, probably: inside a venv, `Scripts\python.exe` is a redirector (copied from CPython's `venvlauncher`) that reads `pyvenv.cfg`, starts the base interpreter as a **child** and waits for it. Outside a venv (system or `py -3.13` interpreter) there is none. Whether the operator's Windows runs so far used a venv is not recorded; the 4B-1 and spike results therefore do not settle this.
   * *Do `Popen.pid` / `poll()` then describe the launcher?* Yes. While the launcher waits for its child, `poll()` is `None` as long as the real worker runs, so a normal run looks right. The risk is `terminate()`/`kill()` (both `TerminateProcess` on Windows): they end the **launcher**. Whether the real worker then dies depends on the launcher's job object (CPython's launcher is believed to use kill-on-close; **not verified here**). If it does not die, the worker is ended by its lifeline watchdog once the parent closes the pipe — except a worker spinning with the GIL held (L8), which would survive as an orphan.
   * *D10 / containment in that case.* `poll()` of the launcher would confirm "gone" and free the slot while the real worker might live: the slot guarantee would then be weaker than stated, exactly for the stuck-with-GIL case. Handshake, deadlines and the pipe lifeline are unaffected.
   * *Do the existing tests detect it?* Before this addition: no — they only probed `Popen.pid`. Now `LauncherIdentityTests` and the hang tests also require the pid written into the shared header (`worker_pid`, the real worker) to be gone, including for a spinning worker; `test_the_production_worker_attaches_and_answers_ping` no longer demands `worker_pid == pid` on Windows. Identities are written to stderr as `LAUNCHER-DIAG …`.
   * *Not decided offline.* If the operator's run shows `same=False` together with a surviving real worker, that is a finding for a separate decision (for example a Job Object or launching the base interpreter directly); **the production launcher was not changed**.
7. **`slot_bytes` defaults to 0** (no segment) so that 4B-1 behaviour is unchanged; 4B-3 sets it from `ImageLimits.max_image_bytes` (D4).

## Results

Linux, CPython 3.13.16: Wave 4B suites 207 tests OK (the 4B-1 suites now have 125: the original 122 plus 3 boundary tests; 4B-2 adds 82). Full repository: 1111 tests, 4 failures + 56 errors, **identical IDs** to the recorded baseline of 60 pre-existing failures. Mutation checks (CRC check, post-copy header re-read, slot release on kill, close-while-active, name unlink, sequence check, nonce check removed one at a time): every mutant fails at least one test.

## Running the tests (Windows operator)

```
cd /d <repo>
git pull
py -m unittest tests.test_isolated_protocol tests.test_isolated_states tests.test_isolated_worker_handler tests.test_isolated_boundaries tests.test_isolated_launcher tests.test_isolated_handles tests.test_isolated_slot tests.test_isolated_limit tests.test_opencv_isolated_traceability tests.test_opencv_isolated_wave4b2_traceability -v
```

Expected: 207 tests, no FAIL and no ERROR. Expected skips: the 4B-1 POSIX/pty cases; in 4B-2 the cases that need `/dev/shm` return early or skip. Areas where Windows may differ and a failure is a finding, not a test bug: `ParentSegmentTests` / `HandshakeSegmentTests` (named mapping, `unlink` is a no-op, the mapping may be page-rounded — checks use `>=`), `test_the_mapping_is_closed_only_after_an_active_copy_has_finished`, `UnreapableTests` (fake processes), `ExitCleanupTests.test_a_normal_exit_without_stop_contains_the_worker` and `test_an_exit_with_a_stubborn_worker_is_bounded_and_ends_it` (venv launcher and `terminate`), and `SteadyStateTests` (handle counts now include the mapping). Please report the Python version, whether `py` runs from a venv, the `LAUNCHER-DIAG` lines (run with `2>&1`), and the full output of any failure.
