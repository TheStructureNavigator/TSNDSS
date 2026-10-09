# DB-03 Wave 4B — Decoder process isolation: research and design (revision 3, approved for 4B-1)

Status: **DESIGN APPROVED BY THE OWNER FOR THE START OF 4B-1.** Nothing is implemented by this document. No code, test, contract, manifest or dependency was changed by Phase A; OpenCV was not installed; the telescope was not contacted. Revision 3 records the owner's decisions (C1, D4, D7, D10, D12, D13) and gives every decision D1–D14 an unambiguous status (§0, §15). Decisions marked *initial design assumption* are approved as starting points and **must be validated later** (Wave 5 hardware and Windows measurements); they are not claims about measured behavior.

Revision history: rev 1 — first design. Rev 2 — owner's review: replaced the `__main__`-manipulation launcher with a dedicated worker entry started by `subprocess.Popen` (§4), specified the shared-memory ownership protocol (§7), separated "process ready" from "decoder ready" (§6), restated the freshness boundary and its measurement (§12), separated Linux-verified results from Windows source-reading and unverified assumptions (§1), stated that isolation is not a sandbox (§13). Rev 3 — owner's decisions recorded; statuses made unambiguous; freeze criteria for the error vocabulary added (§15.1 D13); the Windows launcher spike is the first 4B-1 task and is delivered separately.

Standing principle (unchanged): no Preview evidence becomes a Capture, Frame, Dataset, ProcessingRun, Observation or Session automatically. Nothing here persists pixels, writes domain records, or gives a worker access to them.

---

## 0. Status of the decisions (authoritative table)

Vocabulary: **APPROVED** — decided. **APPROVED, initial design assumption** — approved as a starting point, to be validated later; changing it is not a contract change. **PROVISIONALLY APPROVED** — approved subject to the stated freeze criteria. **APPROVED, pending Windows verification** — approved, but the Windows realization is unverified until the stated check passes.

| ID | Topic | Status | Condition / validation still owed |
|---|---|---|---|
| Direction | One decoder process per camera; shared-memory image transport; parent-controlled deadlines; no pooling | **APPROVED** | — |
| C1 | Python ≥ 3.13 for the isolated decoder | **APPROVED** | The minimum Python version of the whole TSN DSS is **not** raised; the isolated decoder alone refuses to start below 3.13 (`python_unsupported`) and its package must stay importable on older Pythons. |
| D1 | Launch mechanism: dedicated `worker_main` via `subprocess.Popen`, inherited pipe, explicit environment, null stdio, no manipulation of `__main__` | **APPROVED, pending Windows verification** | The Windows spike (§16) must pass; otherwise the documented fallback of §4.2 applies. |
| D2 | State set: the seven requested states plus OPENING | **APPROVED** | — |
| D3 | Shared-memory image transport with the §7 ownership protocol | **APPROVED** | Protocol is normative for 4B-1/4B-2 tests. |
| D4 | Slot size = `ImageLimits.max_image_bytes` initially | **APPROVED, initial design assumption** | Re-validate with measured MAIN/WIDE frame sizes in Wave 5. |
| D5 | No pooling, no pre-warm | **APPROVED** | — |
| D6 | Sequential `close_all` accepted; no Wave 2 change in 4B | **APPROVED** | — |
| D7 | Staleness: accept in 4B, document, measure in Wave 5 | **APPROVED, initial design assumption** | Backlog and glass-to-glass measurements in Wave 5; mitigation chosen from data. |
| D8 | No Win32 hardening via `ctypes` in 4B | **APPROVED** | — |
| D9 | `before_connect` re-verification hook | **APPROVED** | — |
| D10 | Process limit = number of configured cameras (≤ 2), counted until reaped | **APPROVED, initial design assumption** | Re-validate after 4B-2 shows real reaping behavior. |
| D11 | Boundary exceptions for the new package (§14.5) | **APPROVED** | Each exception is confined to the module named in §14.5. |
| D12 | Default deadlines (§15.1) | **APPROVED, initial design assumption** | Placeholders; calibrate on the target Windows machine and hardware in Wave 5. |
| D13 | Numeric u16 error vocabulary | **PROVISIONALLY APPROVED** | Freeze in 4B-1 after the four checks of §15.1 D13 pass. |
| D14 | Phasing 4B-1 / 4B-2 / 4B-3 | **APPROVED** | Each phase stops for review (§16). |

Note on D1–D3, D5, D6, D8, D9, D11, D14: these were approved conditionally after the review of revision 1, on condition that the corrections requested there were made. Revision 2 made them; with the owner's approval of the project at the start of 4B-1 they stand as APPROVED (D1 with its Windows condition).

---

## 1. Evidence, by provenance

All experiments ran on **Linux, CPython 3.13**, 4 cores, 16 GiB, **without OpenCV**, using throwaway scripts outside the repository. Windows could not be run. Three kinds of statement are kept apart.

### 1.1 Verified by experiment — Linux only

| # | Result | Experiment |
|---|---|---|
| L1 | `multiprocessing` `spawn` re-executes the parent's main module in the child (as `__mp_main__`); for `py -m tsn_dss.gui.http_api` that means the whole module and its imports. | E1 |
| L2 | Hiding `__main__.__spec__`/`__file__` stops that. **Not accepted as a production mechanism** (global, temporary mutation of the parent's state). | E1 |
| L3 | A dedicated entry run with `subprocess.Popen([python, "-m", "w.worker_main", …])`: the parent's main module is **not** re-executed; start to first message ≈ 52 ms; stdin/stdout/stderr were requested as the null device by the parent (the experiment did not independently inspect the child's descriptors); environment passed explicitly (a secret variable set in the parent did **not** reach the worker); the worker's argv contained no secret. | E7 |
| L4 | An anonymous duplex `multiprocessing.connection.Pipe()` end can be inherited by the `Popen` child (`pass_fds`) and rebuilt there with `Connection(fd)`; the parent keeps full `poll(timeout)` / `send_bytes` / `recv_bytes(maxlength)` semantics. | E7 |
| L5 | If the parent keeps its copy of the child's pipe end, it never sees EOF when the child dies and a blocking `recv()` hangs forever. The parent must close that end after starting the child and must never `recv` without `poll(timeout)` first. | E3 (my first test hung exactly so) |
| L6 | `poll(timeout)` returns `False` for a worker stuck in a call that never returns. `terminate()` ends it in ≈ 1 ms (POSIX SIGTERM). A worker that **ignores SIGTERM** or is **SIGSTOPped** survives `terminate()` and dies only to `kill()`; terminate → `join(1.0)` → kill → `join(2.0)` took ≈ 1.0 s. A crashed worker (`exitcode` 139) shows as `poll()` true plus `EOFError`. | E3 |
| L7 | If the parent dies, a worker stuck in a call **stays alive** (orphan). A worker with a **dedicated reader thread** on the control pipe exits (`os._exit`) ≈ 50 ms after EOF **while its main thread is stuck**. | E4, E7 |
| L8 | The watchdog/reader thread needs the GIL; a stuck call that holds the GIL (pure-Python spin) would silence it. Not tested here as a live case; follows from the interpreter design. | reasoning, flagged |
| L9 | Orphaned workers that inherit the parent's standard streams can make a capturing launcher appear hung. With stdio set to the null device at spawn this does not occur. | E4, E7 |
| L10 | Transport cost per image, median (indicative): `Pipe.recv_bytes` 1 MiB 4.9 ms · 6 MiB 30 ms · 24 MiB 70 ms · 32 MiB 82 ms. Shared memory plus the one `bytes()` copy the parent must make anyway: 0.3 · 1.7 · 7.8 · 27.6 ms. | E2 |
| L11 | Shared memory, POSIX: the creator can **unlink the name right after the worker has attached**; both mappings keep working and no `/dev/shm` entry remains once both exit. | E7 |
| L12 | Shared memory, POSIX, **hazard**: a non-`multiprocessing` child that attaches with the default `track=True` registers the segment with *its own* resource tracker, which **destroys the segment when the child exits** (the parent then got `FileNotFoundError` on `unlink`, plus leak warnings). `SharedMemory(..., track=False)` (Python 3.13) avoids it. | E7 |
| L13 | A minimal Python child peaks at ≈ 14 MiB RSS; a child importing the Wave 4 adapter module peaks at ≈ 23 MiB and 120–170 ms, because importing anything under `tsn_dss.engine` runs `tsn_dss/engine/__init__.py`, which imports the SQLite repository modules (17 modules) and `socket` (import only, nothing opened). | E6 |

### 1.2 Windows — read in the CPython 3.13 standard-library source only (not run)

| # | Statement | Where |
|---|---|---|
| W1 | `Popen.terminate()` is `TerminateProcess(handle, 1)`; `Popen.kill()` is the same call on Windows. The two-step procedure collapses to one step; "ignores SIGTERM" does not exist there. | `subprocess.py`, `popen_spawn_win32.py` |
| W2 | `multiprocessing.Pipe()` on Windows creates **overlapped** named-pipe handles; `PipeConnection` requires overlapped handles. `PipeConnection(handle)` can wrap an inherited handle. | `connection.py` |
| W3 | `subprocess.STARTUPINFO(lpAttributeList={"handle_list": [...]})` restricts which handles a child inherits; Python filters the list. A handle must be marked inheritable (`os.set_handle_inheritable`). | `subprocess.py` |
| W4 | `SharedMemory` on Windows is a named file mapping that disappears when **all** handles are closed (including by process termination); `unlink()` is a no-op there. `track` has no effect there. | `shared_memory.py` |
| W5 | `multiprocessing`'s own `atexit` handler terminates daemonic children and then `join()`s every child **with no timeout**. | `util.py` |

### 1.3 Unverified assumptions (neither run nor read)

U1. That the W2/W3 combination (overlapped pipe handle inherited through `handle_list` and wrapped by `PipeConnection`) works end to end on the target Windows build. **This is the first task of 4B-1 and must be run by the owner on Windows (§16).**
U2. Windows process start-up time, resident size of a worker that has imported OpenCV, `import cv2` time, named-pipe throughput, commit-charge behavior of a 32 MiB mapping.
U3. Whether FFmpeg inside the OpenCV wheel writes stream addresses to the C-level standard error, and which OpenCV/FFmpeg log-level variables exist and take effect in the pinned build.
U4. Console Ctrl+C delivery to workers on Windows.
U5. What killing an RTSP client mid-session does to the Seestar (half-open sessions, how long a slot stays held), and whether opening the stream of an active camera has any side effect. **Unknown until a controlled hardware run.**
U6. OpenCV 5.x timeout behavior (carried over from Wave 4).
U7. The minimum Python version of the owner's environment (see C1).

### 1.4 Pre-existing issue (not introduced here)

OpenCV's FFmpeg backend reads synchronously; there is no capture thread. If the consumer reads slowly, packets queue in the socket and in FFmpeg and the next `read()` can return an **old** frame. This already exists in the in-process Wave 4 adapter. See §12 and decision D7.

---

## 2. Requirements and invariants

R1. The parent is never blocked longer than a published bound by a decoder.
R2. After a deadline miss, protocol violation or IPC loss the worker is gone (or declared unreapable) within a published bound.
R3. The parent trusts nothing the worker sends; every field is re-validated.
R4. No free text, exception message, traceback, host, address or path crosses the IPC boundary or reaches a log.
R5. Processes ≤ number of configured cameras (≤ 2); memory bounded by configuration.
R6. `close()` and `close_all()` are idempotent, bounded, and safe from any thread.
R7. No automatic reconnect or restart; a new worker only for a new stream after new positive gate evidence.
R8. The worker has no code path to start, stop, arm or park anything; it only opens a read stream on an endpoint it was given.
R9. Existing contracts (`ImageDecoder`, `PreviewSource`, `RtspPreviewSource`, manager, gate) do not change.
R10. **The parent's process-global state is not modified to start a worker** (no edits to `sys.modules['__main__']`, `os.environ`, signal handlers or `sys.path`).

---

## 3. Architecture

```
PreviewStreamManager ──(unchanged)──► PreviewStream ──► RtspPreviewSource ──► ProcessIsolatedDecoder     (parent)
                                                                                  │  control channel: duplex Pipe, binary, ≤ 4 KiB
                                                                                  │  image channel:   shared memory, one slot + header
                                                                                  ▼
                                                                         decoder worker process         (child, Popen, one per camera)
                                                                         worker_main ─► WorkerLoop ─► OpenCvImageDecoder (Wave 4, unchanged)
```

Package `tsn_dss/engine/opencv_isolated_decoder/` (separate from the in-process Wave 4 package, whose boundary tests forbid `multiprocessing`/`subprocess`):

| Module | Responsibility |
|---|---|
| `protocol.py` | Pure message codec (`struct`), status-code table, shared-memory header codec and validation. No I/O. |
| `worker_main.py` | **Dedicated importable worker entry point** (`python -m …worker_main`). Argument parsing, handle adoption, reader thread, `WorkerLoop(decoder)`. Production loop wraps `OpenCvImageDecoder`; tests inject fixture decoders. |
| `process.py` | Parent-side `WorkerProcess`: spawn, state machine, deadlines, containment, slot lifecycle. |
| `decoder.py` | `ProcessIsolatedDecoder` (`ImageDecoder`), `make_isolated_decoder_factory`, live-worker registry, exit cleanup. |

Wiring stays at the existing seam: `build_preview_manager(decoder_factory=make_isolated_decoder_factory(config))`. The manager still builds a decoder only after the gate allowed the open.

---

## 4. Launching the worker (D1, corrected)

### 4.1 Options

| | O1. `multiprocessing` spawn (+ hide parent's `__main__`) | **O2. `subprocess.Popen` of a dedicated worker entry + inherited pipe** | O3. `Popen` + named endpoint (`Listener`/`Client`) |
|---|---|---|---|
| Re-executes parent `__main__` (L1) | yes, unless hidden | **no (L3)** | no |
| Modifies parent's process-global state | **yes** (`__main__` attributes) — rejected by R10 | **no** | no |
| Child environment | inherits everything; cannot be scrubbed | **explicit `env=` allowlist (L3)** | explicit |
| Child stdio | inherits the parent's | **null device set at spawn (L3, L9)** | null device |
| Secrets in argv | none | none by design (only numeric handle and random segment name) | needs an endpoint name and key |
| Channel with `poll(timeout)` | yes | **yes** — `Connection` rebuilt from an inherited handle (L4; Windows W2/W3, U1) | `accept()` has no timeout |
| Sockets | no | no | AF_UNIX on POSIX (conflicts with the project's "no socket" boundary) |
| Windows verified | source only | source only (**U1 spike required**) | source only |

### 4.2 Recommendation (D1, corrected)

**O2.** The parent:

1. creates `parent_end, child_end = multiprocessing.connection.Pipe(duplex=True)` (overlapped on Windows, W2);
2. creates the shared-memory segment (§7) with a random name;
3. builds the child command line `[sys.executable, "-m", "tsn_dss.engine.opencv_isolated_decoder.worker_main", "--ctl", <child end handle/fd as a decimal number>, "--proto", "1"]` — **no host, no address, no key, no segment name in argv** (the name travels in the `INIT` message);
4. starts it with `stdin/stdout/stderr = DEVNULL`, `close_fds=True`, an explicit minimal `env` (`PATH`, `SYSTEMROOT`/`TEMP`-class variables the OS needs, and a `PYTHONPATH` that makes the project importable — built as a dictionary for the child only; the parent's `os.environ` is never touched), and handle inheritance limited to the child end (POSIX `pass_fds`; Windows `STARTUPINFO(lpAttributeList={"handle_list": [child_end handle]})` after `os.set_handle_inheritable`);
5. immediately closes its own copy of `child_end` (L5);
6. holds the `Popen` object (a handle, not a stored PID) for `poll()`, `terminate()`, `kill()`, `wait(timeout)`.

A module-level lock serializes spawns (handle inheritance flags are per handle, and Windows inheritable handles could otherwise leak into a concurrent child). That lock guards only this package's own state.

The worker, `worker_main`, adopts the handle with `multiprocessing.connection.Connection(fd)` (POSIX) / `PipeConnection(handle)` (Windows), then starts the **reader thread** (§9) and the command loop.

Why not O1: it needs a global mutation of the parent's `__main__` and cannot scrub the child's environment or stdio. Why not O3: `accept()` cannot time out and AF_UNIX contradicts the no-socket boundary.

**Fallback if the Windows spike (U1) fails**: keep O2 but replace the inherited-handle channel on Windows with a `Listener(r"\\.\pipe\<random>", family="AF_PIPE", authkey=<per-worker random>)` created by the parent and a `Client` in the worker (the key and pipe name sent through the child's environment for that child only, never argv). `accept()` would then run in a short-lived helper thread joined with a timeout. This costs a thread and complexity, which is why it is a fallback.

### 4.3 Worker entry location (note, not a new decision)

An in-package entry (`tsn_dss.engine.opencv_isolated_decoder.worker_main`) forces Python to import `tsn_dss/engine/__init__.py` first, which loads the SQLite repository modules (L13). They are never executed and the worker is given no database path or key, but they are present in the worker. A standalone top-level worker module would avoid that, but it could not reuse the Wave 4 decoder without duplicating or vendoring it. 4B accepts the in-package entry; it is flagged in §13.

### 4.4 Constraint C1 (approved)

`SharedMemory(..., track=False)` (L12) exists in **Python 3.13 and later**. For older Pythons the only workaround is unregistering the segment from the child's resource tracker through a private API. **C1 (approved): the isolated decoder requires Python ≥ 3.13 and refuses to start otherwise (`python_unsupported`).** The minimum Python version of the rest of TSN DSS is not raised: importing the isolated package on an older Python must not fail, and the check happens when a decoder is created.

---

## 5. Worker lifecycle (state machine, D2)

States of `WorkerProcess`: **CREATED, STARTING, READY, OPENING, READING, FAILED, STOPPING, TERMINATED** (the seven requested plus OPENING). READY covers "process and IPC ready"; the flag `decoder_open` is separate (§6).

| State | Meaning | Process | Allowed next |
|---|---|---|---|
| CREATED | Object built, configuration validated. No process, no segment, no import of OpenCV. | none | STARTING, STOPPING |
| STARTING | Segment created, process spawned; running the readiness handshake (§6) within `start_deadline`. | alive | READY, FAILED |
| READY | Handshake complete and verified. Idle. `decoder_open` true or false. | alive | OPENING (if not open), READING (if open), STOPPING, FAILED |
| OPENING | `OPEN` sent, waiting up to `open_deadline`. | alive | READY (open), FAILED |
| READING | `READ` sent; the slot is owned by the worker; waiting up to `read_deadline`. | alive | READY (open) with the slot handed to the parent, FAILED |
| FAILED | A failure reason (fixed category) is recorded. No further operation is accepted. | alive or dead | STOPPING (immediate, automatic) |
| STOPPING | Containment (§10): graceful `CLOSE` only if the channel is still trustworthy, terminate, bounded join, kill, bounded join; resources released. | being ended | TERMINATED |
| TERMINATED | Reaped, or declared unreapable. Terminal; never reused. | dead / abandoned | — |

Every operation is a transition checked against this table (Wave 1 pattern: explicit table, `InvalidTransition`, transition log with fixed evidence tokens). FAILED is never a resting state. A worker-reported expected failure (`read_failed`: the stream ended) also goes FAILED → STOPPING, matching Wave 4. One operation at a time (`busy` otherwise). `close()` from another thread is allowed at any moment: it sets a closing flag and drives STOPPING (killing a process cannot corrupt the parent, unlike releasing an in-flight in-process capture).

---

## 6. Readiness: process started is not decoder ready

Three different readinesses, never conflated:

| Level | Meaning | How it is established |
|---|---|---|
| **Process alive** | the OS created the process | `Popen` returned. **Nothing more is concluded from this.** |
| **Worker READY** (state READY) | protocol and IPC verified in both directions, shared memory verified, watchdog armed | the handshake below |
| **Decoder ready** (`decoder_open` true) | OpenCV imported, backend confirmed, the stream open | a successful `OPEN` (Wave 4 `open()` semantics: lazy import, version gate, `CAP_FFMPEG`, `getBackendName()`) |

Handshake (all within `start_deadline`; any deviation is FAILED):

1. Parent sends `INIT`: protocol version, a random 64-bit **nonce**, the segment name and size, the limits.
2. Worker validates the version and arguments, **attaches** the segment (`track=False`), verifies the segment header magic and layout version written by the parent, starts the **reader thread** (watchdog armed), and writes `worker_pid` and the same nonce into the header's handshake field.
3. Worker replies `HELLO(status=0, nonce)`. A non-zero status (for example `python_unsupported`, `shm_unavailable`) is a refusal, not readiness.
4. Parent checks: `HELLO` fields exactly as expected; the nonce echoed on the pipe equals the nonce the worker wrote into the shared header (proves the segment mapping is genuinely shared both ways); the worker's `Popen` is still running.
5. Parent sends `PING(op_id)`, worker replies `PONG(op_id)` (proves the command loop, not only the reader thread, is running).
6. Only then: state READY. If the parent is on POSIX, it now **unlinks the segment name** (§7.4).

OpenCV is deliberately **not** imported during the handshake, so a missing OpenCV is reported by `OPEN` as `opencv_unavailable` exactly as in Wave 4, and process readiness stays independent of the decoder library.

---

## 7. Shared-memory ownership protocol (D3)

### 7.1 Segment layout

One named segment per worker, size `64 + slot_bytes`.

| Offset | Field | Written by | Purpose |
|---|---|---|---|
| 0 | `magic` `"TDS1"` | parent (creation) | identifies the layout |
| 4 | `layout_version` u16 | parent | |
| 6 | `slot_bytes` u32 | parent | capacity of the pixel area |
| 12 | `handshake_nonce` u64 | worker (step 2) | proves a shared mapping (§6) |
| 20 | `worker_pid` u32 | worker | diagnostic |
| 24 | `begin_seq` u32 | worker | frame-write fence, start |
| 28 | `end_seq` u32 | worker | frame-write fence, end |
| 32 | `width`, `height` u32 | worker | |
| 40 | `pixel_format` u8, `nbytes` u32 | worker | |
| 64 | pixel area, `slot_bytes` | worker | row-major, tightly packed, top row first (PreviewPixels layout) |

**Erratum (4B-2, owner decision):** layout version 2 adds `pixel_crc32` u32 at offset 48 (bytes 52..63 stay zero); the parent verifies it on its own copy. Version 1 headers are refused. See `docs/DB-03_WAVE4B2_SHARED_MEMORY_CONTAINMENT.md`.

All header fields are written with fixed-width little-endian `struct` packing; the parent never interprets header bytes by any other means.

### 7.2 Who creates and who removes

- **The parent creates** the segment (random name from `secrets.token_hex(16)`) **before** spawning, and is the **only** process that ever unlinks it. The worker only attaches, never creates, never unlinks.
- The worker attaches with `track=False` (L12).
- **POSIX**: the parent unlinks the *name* as soon as the handshake completed (§6 step 6, L11). From then on there is no name to leak: both mappings die with their processes even if the parent crashes. Before the handshake completes, a `finally` unlinks on any failure. A resource-tracker registration in the parent stays as a backstop.
- **Windows**: nothing to unlink; the mapping disappears when the parent and the worker have both closed their handles, which process termination does (W4).
- The parent closes its mapping in the same `finally` as containment, after the worker is dead or abandoned.

### 7.3 Ownership of the slot — the rule

The slot is owned by exactly one side at any time, and ownership changes **only** by protocol messages, never by inspecting the segment:

| Phase | Slot owner | Worker may | Parent may |
|---|---|---|---|
| after handshake, before the first `READ` | parent (idle) | nothing | nothing |
| from the moment the parent **sends `READ`** until the worker **sends `FRAME`** | **worker** | write header and pixels | not touch the pixel area or the frame header (it is waiting) |
| from the moment the parent **receives `FRAME`** until the parent **sends the next `READ`** (or `CLOSE`) | **parent** | not touch the segment (it is waiting for the next command) | read header, copy the pixels, validate |
| after the parent sent the next `READ` | worker | (as above) | — |

**When may the worker write?** Only after it received `READ(op_id)` and before it sends `FRAME(op_id)` for that same `op_id`.
**When may the parent copy?** Only after receiving a `FRAME` whose `op_id` is the outstanding one, and before sending the next command.
**When can the slot be reused?** The parent's *sending of the next `READ`* is the release: before sending it, the parent has already copied the pixels into an immutable `bytes` object and finished validating. Because there is one slot and one outstanding operation, there is no second buffer, lock or free-list.

The segment header's `begin_seq`/`end_seq` are **verification fields, not synchronization**; synchronization is the message order.

### 7.4 Detecting an incomplete (torn) frame

A frame can be incomplete if the worker dies or stalls mid-write, if a buggy or compromised worker writes while the parent reads, or if metadata and pixels disagree. Defenses, in order:

1. **Message gating**: the parent reads the slot only after a `FRAME` message. A worker that dies or hangs mid-write never sends `FRAME`; the parent sees EOF or a missed deadline and never reads the slot.
2. **Write fence**: the worker writes `begin_seq = n` (with `n` the frame sequence), then dimensions, format, `nbytes`, then the pixels, then `end_seq = n`, then sends `FRAME(seq = n)`.
3. **Pre-copy checks (parent)**: `begin_seq == end_seq == FRAME.seq`; the header's width, height, format and `nbytes` equal the `FRAME` message's; `nbytes == width × height × bytes_per_pixel`; `nbytes ≤ slot_bytes` and ≤ `ImageLimits.max_image_bytes`; dimensions within `ImageLimits`.
4. **Post-copy check (parent)**: re-read `begin_seq` and `end_seq` **after** the `bytes` copy; they must still equal the frame sequence. A change means the worker wrote during the parent's ownership (a protocol violation) → FAILED, the copy is discarded.
5. `PreviewPixels` re-validates length and ceilings; the stream computes the digest in the parent. The digest is never taken from the worker.

Any failed check is `worker_protocol_error` → containment; no pixels are stored. This does not make a malicious worker harmless (it can still write plausible pixels), but a corrupted or lying one cannot cause an out-of-bounds read, an unbounded copy or a half-frame to be accepted silently.

### 7.5 Cleanup after failure

| Situation | What is released, by whom |
|---|---|
| Spawn fails before the handshake | parent: closes both pipe ends, closes and unlinks the segment (`finally`) |
| Worker dies before the handshake completes | parent: same as above after reaping |
| Worker dies after the handshake (POSIX) | name already unlinked; the parent closes its mapping; the kernel frees the pages when both are gone |
| Worker dies after the handshake (Windows) | parent closes its handle; the mapping is freed |
| Parent crashes after the handshake | POSIX: nothing left by name; the worker exits through its watchdog (§9). Windows: last handle closes with the processes |
| Parent crashes between creation and handshake (POSIX) | one leaked `/dev/shm` entry possible; the parent's resource-tracker registration removes it when the tracker exits. Documented limitation; the window is the start-up time |
| Containment cannot reap the worker | the worker keeps its mapping; the parent closes its own; the registry keeps the `Popen` so the leak is visible and counted |

Tests for each row are in §14.

---

## 8. Control channel and protocol

`multiprocessing.connection.Connection` pair; parent uses only `poll(timeout)`, `send_bytes`, `recv_bytes(maxlength=4096)`. Not used: `Queue` (feeder threads, pickle, hangs after terminate), `send`/`recv` (pickle — **never** unpickle data from the worker), sockets, stdio.

Header (8 bytes, network order): `magic "TDW1"` (4) · `type` u8 · `flags` u8 (0) · `reserved` u16 (0). Every message ≤ 4096 bytes. Anything larger, shorter than its type requires, with trailing bytes, wrong magic, unknown type, non-zero reserved bits or an unexpected `op_id` is a protocol violation → FAILED.

| Type | Dir | Payload |
|---|---|---|
| `INIT` | P→W | `proto_version` u16, `nonce` u64, `shm_name_len` u8, `shm_name`, `slot_bytes` u32, `max_width` u32, `max_height` u32 |
| `HELLO` | W→P | `proto_version` u16, `status` u16, `nonce` u64 |
| `PING` / `PONG` | both | `op_id` u32 |
| `OPEN` | P→W | `op_id` u32, `open_timeout_ms` u32, `read_timeout_ms` u32, `addr_len` u16 (≤ 512), `address` (UTF-8, must start `rtsp://`) |
| `READ` | P→W | `op_id` u32 |
| `CLOSE` | P→W | `op_id` u32 |
| `RESULT` | W→P | `op_id` u32, `status` u16 (for OPEN, CLOSE, and a failed READ) |
| `FRAME` | W→P | `op_id` u32, `seq` u32, `width` u32, `height` u32, `pixel_format` u8 (1 GRAY8, 3 BGR8), `nbytes` u32, `worker_decode_ns` u64 (diagnostic only, §12) |

`status` is a u16 from a fixed table (§15 D13), never a string. The address crosses the boundary exactly once, in `OPEN`, parent → worker.

---

## 9. Hang detection, orphan handling and the worker's threads

- **Parent**: every wait is `poll(deadline)`. The parent never blocks on the worker.
- **Worker**: two threads.
  - the **reader thread** blocks on `recv_bytes(4096)` of the control pipe, validates framing, and puts commands on a queue; on `EOFError`/`OSError` it calls `os._exit(3)` immediately. This is the orphan/lifeline watchdog (L7): it works while the main thread is stuck in a C call.
  - the **main thread** takes commands from the queue and runs them against the decoder.
- Caveat (L8): if the stuck call holds the GIL, the reader thread cannot run; parent-death detection then fails and the worker lives until something kills it. The normal-path containment does not depend on the worker, so this affects only orphan cleanup.
- The worker ignores SIGINT (shutdown is the parent's decision). It never calls `Connection.send`/`recv` (pickle).

---

## 10. Deadlines and containment

Deadlines are set by the parent per operation; the worker's own OpenCV soft timeouts are shorter, so a slow-but-alive decoder reports a category and only a stuck one triggers containment. Values are in §15 D12 (open).

```
contain(worker):
    if worker alive:
        send CLOSE only if the channel is still trusted                        # best effort, short
        popen.terminate()                      # Windows: TerminateProcess (final, W1)
        popen.wait(timeout=T_term)             # bounded
    if worker still alive:                     # POSIX: SIGTERM ignored / process stopped
        popen.kill()
        popen.wait(timeout=T_kill)             # bounded
    if worker still alive:                     # uninterruptible; cannot be reaped
        record containment_failed; keep the Popen in the registry; mark the decoder unusable; no further attempt
    close pipe ends; close the mapping (and unlink if not yet unlinked); registry removal when dead
```

Never an unbounded `wait()`; termination is by `Popen` handle, not by stored PID. After a deadline miss the channel is closed together with the process, so no late reply can be mistaken for the answer to a later operation.

Call-time bounds seen through the existing `ImageDecoder` contract (formulas; numbers depend on D12): `open` = start + open + containment; `read` = read + containment; `close` = graceful + terminate + kill. Today these calls are unbounded.

---

## 11. Integration with existing contracts (unchanged) and the TOCTOU window

- `open(endpoint)` reads `endpoint.address`/`.camera`, creates the worker, runs the §6 handshake, sends `OPEN`; raises `DecoderError(category)`.
- `read()` sends one `READ`, returns `PreviewPixels` (BGR8/GRAY8) or raises `DecoderError`; after any raise the decoder is dead (single use, as Wave 4).
- `close()` runs containment; idempotent; at most one `containment_failed`.
- `RtspPreviewSource` maps `DecoderError` to `PreviewSourceError` exactly as today. `simulated` is `False`. The factory returns a **new** decoder per call; the process cap prevents more than the configured cameras from being alive.

TOCTOU. Windows of exposure between the gate decision and the RTSP handshake:

| Window | Today (Wave 4) | With a worker |
|---|---|---|
| gate → factory → `stream.open()` | ms | ms |
| spawn + handshake until the worker could connect | — | start-up time (52 ms on this Linux box, **unmeasured on Windows**) |
| `OPEN` sent → connection made | ms | ms |

Mitigation (D9, approved): `ProcessIsolatedDecoder` takes an optional `before_connect() -> bool`, wired in the integration layer (`build_preview_manager`) from the same evidence provider and gate as a decoder-factory closure. It runs after worker READY and **before `OPEN`**; `False` → containment and `readiness_revoked`. No change to the manager, `RtspPreviewSource` or `ImageDecoder`. Spawning workers before the gate is not proposed. The remaining window — the device changing state between the last check and the handshake — cannot be closed.

---

## 12. Freshness: what `host_observed_at` means and how it will be measured

**Semantic boundary (unchanged by this design).** `PreviewImageEvidence.host_observed_at` is the parent's clock reading when the parent received the image. It is **not** the exposure time, not the worker's decode time, not the RTSP packet arrival time. `exposure_time_status` stays `UNKNOWN`; `provider_reported_at` stays `None`; the worker never supplies a timestamp that the runtime treats as a time of the scene.

The `FRAME` message carries `worker_decode_ns` (worker monotonic clock) **only as a diagnostic**: it is stored in no evidence object, never feeds the freshness classifier, and is documented as not being an exposure time.

**Wave 5 measurement plan** (design only; to be turned into a procedure in Wave 5):

| Quantity | How | Why |
|---|---|---|
| `read()` call duration | parent monotonic clock around `READ`→`FRAME` | detects stalls and backlog draining |
| Pipeline latency inside the host | parent receipt − `worker_decode_ns` (same machine, monotonic clocks) | cost of isolation: IPC, copy |
| Inter-frame interval | consecutive `host_observed_at` | real frame rate; calibrate `max_age` |
| Backlog / staleness | pause reading for N seconds, resume, observe how many frames arrive back-to-back and how fast | quantifies §1.4 |
| Glass-to-glass latency (ground truth) | operator shows a running time code (screen clock or LED counter) to the camera; compare the time code decoded in the frame against the receipt time | the only way to know the age of the *scene*; operator-assisted, optional |

Results feed decisions D7 and D12; none of them is used to relabel `host_observed_at`.

---

## 13. Isolation is not a security sandbox

Stated plainly so it is not over-claimed:

- The worker runs as **the same OS user with the same filesystem and network access** as the parent. A compromised worker (a media-parsing vulnerability in FFmpeg reached through a hostile stream is the realistic threat) can read and write what the parent can.
- Importing the in-package entry loads `tsn_dss.engine` and its SQLite repository modules into the worker (L13, §4.3). They are never run and no database path or key is passed, but they are there.
- Mitigations in this design reduce the *accidental* blast radius only: explicit minimal environment (project secrets in the parent's environment do not reach the worker, L3), no secrets in argv, null stdio, no pickle from the worker, parent-side re-validation of every field, a process cap, and bounded termination.
- Not provided: privilege separation, filesystem or network confinement, memory caps on Windows, seccomp-class restrictions. These would need `ctypes`/Win32 (Job Objects, low integrity, AppContainer) or OS facilities and are out of scope (D8).
- Process isolation contains **hangs, leaks and crashes**, not hostile code.

---

## 14. Resource budget, failure matrix, cleanup and test plan

### 14.1 Resource budget

Defaults `max_image_bytes` 32 MiB, `max_buffer_images` 4, `max_buffer_bytes` 64 MiB.

| Item | Where | Size |
|---|---|---|
| Interpreter + imports, no OpenCV | worker | ≈ 23 MiB (measured on Linux) |
| OpenCV + FFmpeg runtime | worker | **unmeasured** (assume 100–300 MiB) |
| One decoded frame inside OpenCV | worker | ≤ 32 MiB |
| Segment | shared | 64 B + slot (Windows: committed at creation, U2) |
| Transient `bytes` copy | parent | ≤ 32 MiB, freed after `PreviewPixels` is stored |
| `PixelBuffer` | parent | ≤ 64 MiB (Wave 1) |

Worst case at the defaults ≈ 160 MiB of image memory per camera, ≈ 320 MiB for two, plus two OpenCV runtimes. A 1080p BGR frame (≈ 6 MiB, **not verified for the S30 Pro**) reduces the image part to ≈ 50 MiB per camera. No hard memory cap exists without `ctypes` on Windows (§13); protection is `ImageLimits` in the worker and again in the parent.

### 14.2 Failure matrix

| # | Failure | Detected by | Parent action | Category | Stream |
|---|---|---|---|---|---|
| 1 | Spawn fails | exception from `Popen` | FAILED → STOPPING; release segment | `worker_start_failed` | open fails |
| 2 | No/late `HELLO`, wrong nonce, wrong header | handshake deadline / validation | contain | `worker_handshake_timeout` / `worker_protocol_error` | open fails |
| 3 | `HELLO` status ≠ 0 | status code | contain | mapped, e.g. `python_unsupported` | open fails |
| 4 | `PONG` missing (command loop dead) | deadline | contain | `worker_handshake_timeout` | open fails |
| 5 | `OPEN` hangs | `poll(open_deadline)` | contain | `open_deadline_exceeded` | open fails |
| 6 | `OPEN` fails normally | `RESULT` status | contain | mapped | open fails |
| 7 | `READ` hangs | `poll(read_deadline)` | contain | `read_deadline_exceeded` | LOST |
| 8 | `READ` fails normally | `RESULT` status | contain | mapped | LOST |
| 9 | Worker crashes | EOF / exit code | reap, release | `worker_crashed` | LOST |
| 10 | Pipe lost, process alive | `OSError`/`EOFError` | contain | `worker_ipc_lost` | LOST |
| 11 | Wrong magic / size / type / `op_id` | protocol validation | contain | `worker_protocol_error` | LOST |
| 12 | Torn or inconsistent frame (§7.4) | fence / metadata / post-copy check | discard copy, contain | `worker_protocol_error` | LOST |
| 13 | Late/duplicate reply | cannot occur (channel closed with the process) | — | — | — |
| 14 | `CLOSE` hangs | graceful deadline | terminate → kill | none (close succeeds) | CLOSED |
| 15 | Ignores terminate / is stopped (POSIX) | wait times out | kill + bounded wait | none | — |
| 16 | Survives kill | wait times out | give up, count, mark unusable | `containment_failed` | visible leak |
| 17 | Worker holds the GIL in a stuck call | parent deadline | contain (normal path unaffected); orphan watchdog silent | deadline category | LOST |
| 18 | Parent crashes/killed | worker reader thread EOF | worker `os._exit` | — | no orphan, except row 17 |
| 19 | Parent exits without `close` | own `atexit` registry then `multiprocessing`'s (W5) | bounded containment of every live worker | — | — |
| 20 | disconnect / `close_all` during READING | manager → `close()` (any thread) | closing flag; containment; blocked `read()` wakes on EOF | `worker_ipc_lost` | CLOSED |
| 21 | Segment cannot be created (commit limit) | exception | FAILED before spawn | `shm_unavailable` | open fails |
| 22 | POSIX crash between creation and handshake | n/a | tracker backstop | — | documented limitation |
| 23 | Worker killed while a Seestar session is open | n/a | — | — | **effect on the device unknown (U5)** |
| 24 | Process cap reached | counter | refuse before any resource is created | `worker_limit` | open fails |
| 25 | State changed during spawn (TOCTOU) | `before_connect` | contain before `OPEN` | `readiness_revoked` | open fails |
| 26 | Python < 3.13 | version check at INIT/start | refuse | `python_unsupported` | open fails |

### 14.3 Cleanup paths

Per stream: `RtspPreviewSource.close()` → `ProcessIsolatedDecoder.close()` → containment (§10), idempotent, any thread. disconnect/`close_all`: the Wave 2 manager closes every stream; each close is bounded. Application exit: a registry of live workers plus one `atexit` handler registered after `multiprocessing` is imported (so it runs first, W5). Parent crash: reader-thread watchdog (L7). Segment: §7.5.

### 14.4 Redaction by construction

1. The address is sent once, in `OPEN`, over the private pipe. Never in argv, environment, the segment or a log.
2. The worker's protocol has no string field toward the parent; only fixed-width integers.
3. stdin/stdout/stderr are the null device from the moment of spawn (set by the parent, not by the worker), so FFmpeg's own log lines go nowhere even before the worker's first instruction (U3 concerns whether they would contain addresses at all). The worker also sets OpenCV/FFmpeg log-level variables in its own process before importing OpenCV (names to be confirmed against the pinned build).
4. The parent maps codes to `DecoderError(category)` with `from None`; `repr` of the decoder, `WorkerProcess` and the transition log contain camera labels, states and fixed tokens only.

### 14.5 Test plan (offline: no network, no telescope, no OpenCV)

Hard containment is tested against **real hung processes**, with the decoder injected: production `WorkerLoop(OpenCvImageDecoder)`; tests use `WorkerLoop(fixture)` selected by a test-only entry module in the test tree (`tests/…`) started with the same `Popen` mechanism. Production code gets no "load any module" parameter.

Fixtures: `ok` (synthetic frames through the slot), `hang_open`/`hang_read`/`hang_close`, `spin_gil` (pure-Python spin), `ignore_sigterm` and `stopped` (POSIX only), `crash_open`/`crash_read`, `exit_after_hello`, `no_pong`, `garbage_reply`/`oversize_reply`/`wrong_op_id`/`unknown_type`/`truncated`, `lying_frame`, **`torn_frame`** (begin/end fence mismatch), **`write_during_parent_copy`** (writes the slot while the parent owns it), `wrong_nonce`, `slow_but_alive`, `closed_pipe`, `leak_address`.

Groups: (1) protocol codec and header codec without processes; (2) state machine, full transition table; (3) **launcher**: no parent-`__main__` re-execution with a throw-away main module, `env` allowlist (a planted secret variable is absent in the worker), argv has no host/address/segment name, stdio is the null device, parent's `sys.modules['__main__']`/`os.environ`/`sys.path` unchanged before and after (R10), spawn serialization; (4) **readiness**: process alive but handshake incomplete is not READY, nonce/shared-header cross-check, `PONG` required, OpenCV not imported during the handshake; (5) **shared memory**: ownership rules, torn-frame detection, post-copy re-check, `track=False` (the segment survives the worker's exit on POSIX), name unlinked after the handshake (POSIX), no leaked segment after worker crash / parent kill / containment failure; (6) hard containment per hang fixture with elapsed-time bounds and proof that the process is gone; (7) parent death (middle process killed, worker disappears; Windows liveness probe in the test helper only); (8) failure matrix rows reproducible offline; (9) isolation and the process cap, MAIN hung while WIDE keeps delivering; (10) gate integration: refused gate → no process created, `before_connect` false → no `OPEN`, recovery only through a new stream after new evidence (reuse the Wave 3 `Flow`); (11) redaction across reprs, exceptions, transition log and captured streams; (12) exit cleanup, including a SIGTERM-ignoring fixture, with the interpreter exiting within a bound; (13) boundary tests.

Boundary changes (D11, for review): allowed in the new package — `multiprocessing.connection`, `multiprocessing.shared_memory`, `subprocess` (only in `process.py`), `struct`, `secrets`, `threading`, `atexit`, `time`, `signal`, `enum`, `dataclasses`, `typing`, `os` (only in `worker_main.py`, for `_exit`). Still forbidden: `socket`, `ctypes`, `pickle` and `Connection.send`/`recv`, static imports of `cv2`/`numpy`, file writes, command vocabulary, address literals. The Wave 4 package keeps its boundary test unchanged; the isolated package imports it only inside `worker_main.py`. A 4B manifest would claim only what its tests prove and list memory containment, sandboxing and any hardware claim as not claimed.

---

## 15. Decisions

### 15.1 Decisions D4, D7, D10, D12, D13 — literal text and recorded owner decision

**D4 — Slot size.**
*Literal text:* "The shared-memory pixel area of each worker is `slot_bytes` large. Use the Wave 1 per-image ceiling `ImageLimits.max_image_bytes` (default 33 554 432 bytes = 32 MiB) as `slot_bytes` in 4B-1…4B-3, so that any image the preview runtime accepts fits. After Wave 5 has measured real MAIN and WIDE frame sizes, set `slot_bytes` per camera to the measured maximum plus a stated margin, never above `max_image_bytes`."
*Alternatives:* (a) fixed small default (for example 8 MiB) — risks refusing legitimate large frames; (b) negotiate at `OPEN` from the first frame — extra protocol and a segment resize.
*Recommendation:* the literal text above (32 MiB now, tuned in Wave 5). Cost: up to 32 MiB committed per worker (Windows) even for 6 MiB frames.
**Owner decision: APPROVED as an initial design assumption (re-validate in Wave 5).**

**D7 — Staleness handling.**
*Literal text:* "Wave 4B does not change how frames are fetched. `host_observed_at` remains the parent's receipt time. A stale frame buffered in OpenCV/FFmpeg or the network can therefore be received late and look fresh. 4B documents this, adds the diagnostic fields in §12, and defers any mitigation until Wave 5 has measured the backlog on hardware."
*Alternatives:* (2) drain with `grab()` before returning the newest frame; (3) a free-running worker thread keeping a 'latest frame' with a second slot or seqlock.
*Recommendation:* the literal text (accept and measure). Mitigations 2 and 3 add OpenCV API surface or a second buffer and should be chosen from data.
**Owner decision: APPROVED as an initial design assumption (measure in Wave 5).**

**D10 — Process limit scope.**
*Literal text:* "At most N decoder worker processes may be alive in the host process at any time, where N is the number of configured cameras (maximum 2). A worker counts against N from the moment its process is created until it has been reaped. A worker that could not be reaped (`containment_failed`) keeps counting. Creating a worker when N are counted fails with `worker_limit` before any resource is created."
*Alternatives:* (a) a fixed global 2 regardless of configuration; (b) per-manager limits only (no process-wide counter); (c) allow a replacement worker once the old one is merely *terminated*, not reaped.
*Recommendation:* the literal text. (b) would let two managers exceed the budget; (c) can multiply stuck processes.
**Owner decision: APPROVED as an initial design assumption (re-validate after 4B-2).**

**D12 — Default deadlines.**
*Literal text:* "Defaults, all configurable: `start_deadline` 10 s (process spawn through the §6 handshake); `OPEN` hard deadline = `open_timeout_ms` + 1500 ms (with the Wave 4 default 5000 ms ⇒ 6.5 s); `READ` hard deadline = `read_timeout_ms` + 1000 ms (default 3000 ms ⇒ 4 s); graceful `CLOSE` 500 ms; `terminate` wait 1.0 s; `kill` wait 1.0 s. They are placeholders until Wave 5 calibrates them on the target Windows machine and hardware."
*Resulting bounds:* `read` ≤ 4 s + 2 s = 6 s; `close` ≤ 2.5 s (Windows ≈ 1.5 s, kill = terminate); `open` ≤ 10 + 6.5 + 2 = 18.5 s worst; two hung cameras in `poll_all` ≈ 12 s; sequential `close_all` of two hung workers ≈ 5 s.
*Alternatives:* a longer start deadline if `import` is slow on Windows; shorter margins if the soft timeouts prove reliable.
*Recommendation:* the literal text as placeholders, clearly marked unmeasured on Windows.
**Owner decision: APPROVED as initial design assumptions (placeholders; calibrate in Wave 5).**

**D13 — Error vocabulary.**
*Literal text:* "The control protocol carries `status` as an unsigned 16-bit code. Code 0 is success. Worker-originated codes, each mapping to the `DecoderError` category of the same name used by Wave 4: 1 `opencv_unavailable`, 2 `opencv_version_unknown`, 3 `opencv_version_unsupported`, 4 `opencv_incompatible`, 5 `invalid_endpoint`, 6 `open_failed`, 7 `backend_unverified`, 8 `backend_mismatch`, 9 `read_failed`, 10 `decoder_bad_output`, 11 `unsupported_depth`, 12 `unsupported_format`, 13 `dimensions_exceed_limits`, 14 `image_exceeds_limits`, 15 `length_mismatch`, 16 `release_failed`, 17 `invalid_state`, 18 `busy`, 19 `not_open`, 20 `python_unsupported`, 21 `shm_unavailable`, 255 `worker_internal`. Parent-originated categories, never sent by a worker: `worker_start_failed`, `worker_handshake_timeout`, `worker_protocol_error`, `worker_crashed`, `worker_ipc_lost`, `open_deadline_exceeded`, `read_deadline_exceeded`, `close_deadline_exceeded`, `containment_failed`, `worker_limit`, `readiness_revoked`. An unknown code from a worker is `worker_protocol_error`. No category contains a host, address, path or message text."
*Alternatives:* fewer, coarser categories (less diagnostic power); reuse Wave 4 strings via a numeric table only.
*Recommendation:* the literal text. The numeric values are provisional until 4B-1 freezes them in a test.
**Owner decision: PROVISIONALLY APPROVED — codes 1–21 and 255, to be frozen in 4B-1 after these four checks pass:**
1. **Mapping.** A test enumerates every Wave 4 `DecoderError` category the in-process adapter can raise and asserts each has exactly one worker code and that the round trip code → category → code is the identity; the table has no duplicate codes or categories and no category contains a host, address, path or message text.
2. **Versioning.** The table carries a version number that is exchanged in `INIT`/`HELLO` together with the protocol version; a mismatch is a handshake refusal (`worker_protocol_error`), and a frozen test pins the numeric table so any later change is deliberate and bumps the version. Codes are append-only: a number is never reused or reassigned.
3. **Unknown codes.** A code that is not in the table (including 0 where an error is required, values 22–254 and anything above 255) is treated as `worker_protocol_error` and ends the worker; it is never mapped to a "close enough" category and never raises an uncaught error in the parent.
4. **Separation of parent-originated errors.** Parent-originated categories (`worker_start_failed`, `worker_handshake_timeout`, `worker_protocol_error`, `worker_crashed`, `worker_ipc_lost`, `open_deadline_exceeded`, `read_deadline_exceeded`, `close_deadline_exceeded`, `containment_failed`, `worker_limit`, `readiness_revoked`) have no wire code; a test proves a worker cannot cause one by sending a code except through the unknown-code rule, and that no worker-originated category can be produced by the parent's own failure paths.

### 15.2 Constraint C1 — approved

**C1 — Python ≥ 3.13 for the isolated decoder only (§4.4).** Without `track=False` the segment would need a private-API workaround to survive the worker's exit on POSIX. The project-wide minimum Python version is not raised.

### 15.3 Summary of the decisions approved after the revision-1 review

D1: O2 (Popen + dedicated entry + inherited pipe, explicit env, null stdio); no manipulation of `__main__`; Windows spike first — approved, pending Windows verification. D2: eight states. D3: shared memory with the §7 protocol. D5: no pooling. D6: sequential close accepted. D8: no `ctypes` hardening. D9: `before_connect` hook. D11: boundary exceptions as §14.5. D14: phasing §16.

---

## 16. Phasing and the start of 4B-1

| Phase | Content | Needs |
|---|---|---|
| **4B-1** | (1) **Windows launcher spike** — a standalone diagnostic script run by the owner on Windows, settling U1; (2) `protocol.py`: messages, status table (D13 freeze checks), segment-header codec, with tests; (3) the state-machine table with tests; (4) the launcher with fixture workers only (no OpenCV), POSIX in the repository's tests, Windows gated on the spike | C1, D13 (provisional, frozen at the end of 4B-1), spike result |
| 4B-2 | `WorkerProcess` containment against hang/crash/protocol fixtures; shared-memory protocol tests; parent-death and exit cleanup | D10, D12 values (initial assumptions) |
| 4B-3 | `ProcessIsolatedDecoder`, integration with the Wave 3 `Flow`, `before_connect`, manifest and documentation | D4 (slot), D7 |

Order inside 4B-1: the spike comes first; nothing that depends on the Windows handle-inheritance mechanism is written until its result is known. The protocol, the state machine and the status table do not depend on it. The spike script is a diagnostic aid, not part of the 4B-1 deliverable until the owner agrees to commit it.

---

## 17. Limits that remain after this design

1. Not a sandbox (§13): same user, filesystem, network and, in-package, the SQLite modules loaded.
2. No hard memory cap on Windows without `ctypes`.
3. A kernel-level unreapable process cannot be killed by user-mode means; it is only made visible and counted.
4. The effects of killing an RTSP client mid-session on the Seestar, and of opening an active camera's stream, are unknown (U5).
5. Windows start-up time, memory, throughput and log-variable names are unmeasured (U2, U3); all default deadlines are placeholders.
6. Frames can still be stale inside the receive path (§1.4, D7).
7. If a stuck call holds the GIL, parent-death detection by the worker fails (L8).
8. The Windows launcher path is unverified until the 4B-1 spike (U1).

---

## 18. Appendix — provenance

| Claim | Source |
|---|---|
| L1–L13 | Experiments E1–E4, E6 and E7 on Linux/CPython 3.13 in a scratch directory outside the repository (not committed) |
| W1–W5 | CPython 3.13 standard-library source in the container: `subprocess.py`, `multiprocessing/{connection,shared_memory,util,popen_spawn_win32,spawn}.py` |
| OpenCV timeout semantics and GIL release | Wave 4 research, `docs/DB-03_OPENCV_DECODER.md` |
| U1–U7 | not confirmed here |
