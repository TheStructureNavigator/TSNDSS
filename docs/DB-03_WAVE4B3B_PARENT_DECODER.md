# DB-03 Wave 4B-3b — parent-side `ProcessIsolatedDecoder` (offline)

Status: **implemented offline; verified on Linux (CPython 3.13.16) only; not yet run on Windows; not committed.** Implements the parent side of `docs/DB-03_WAVE4B3_INTEGRATION_DESIGN.md` (E1–E9 with their conditions) on top of the 4B-3a worker. Branch `feat/db03-wave4b3b-parent`, base `master` `9991df8`. **Fake `cv2` only: no real OpenCV/FFmpeg, no RTSP, no network, no device. Not hardware verified; not production ready.** 4B-3c (composition helper, `before_connect` wiring to the real gate, manifest) is not started.

## What exists

| File | Change |
|---|---|
| `decoder.py` (new) | `IsolatedDecoderConfig` (timeouts as in Wave 4; **one `ImageLimits`** that defines the slot size and the worker limits; `WorkerConfig` for the D12 deadlines, unchanged values), `ProcessIsolatedDecoder` (`ImageDecoder`: `open`/`read`/`close`, `simulated = False`, redacted repr), `OperationTimings`, `make_isolated_decoder_factory(config, before_connect=None)`. |
| `process.py` | Additive, owner decision E6-B: the token `intermediate_launcher` in `StopReport.uncertain` when the pid of the started process differs from the pid the worker wrote into the shared header. **Diagnostic only; containment, D10 and the release of the slot are unchanged.** |
| `__init__.py` | exports the four new names through a module-level `__getattr__`, so a worker process that imports the package does not load `seestar_preview`. |
| tests | `tests/test_isolated_decoder.py` (43), boundary test extended (+1, `decoder.py` added to the E2 set), `pid_differs` in `tests/decoder_worker_fixture.py`, two assertions made tolerant of the diagnostic token (they would otherwise fail in a Windows venv). |
| unchanged | `ImageDecoder`, manager, gate, `RtspPreviewSource`, `protocol.py`, `segment.py`, `shm.py`, `states.py`, `worker_main.py`, `worker_decoder.py`, `decoder_main.py`, D12, D13, DB-01/02 and earlier DB-03 waves. |

## Behaviour

* **Constructor:** stores configuration; creates no worker, starts no process, imports neither `cv2` nor NumPy (tested in a fresh interpreter, also on a simulated old Python).
* **`open(endpoint)`:** endpoint check (`invalid_endpoint`) → Python gate through `WorkerProcess` creation (`python_unsupported`, E3; the existing gate is kept) → `start()` (reservation, segment, spawn, handshake) → `before_connect(camera)` if given (`False`, `None` or an exception → worker stopped, `readiness_revoked`, `OPEN` never sent, the exception text discarded) → `OPEN(address, open_ms, read_ms)`. The address is read once and kept nowhere. Any failure leaves the decoder dead and the worker ended.
* **`read()`:** `WorkerProcess.read()` → `PreviewPixels` built from the parent-owned copy → `check_limits` against the same `ImageLimits` → returned. Never `None`. A deadline miss, lost pipe, protocol violation, bad slot/CRC/sequence/geometry, unknown format code or an image that fails the parent's limits ends the worker and raises a `DecoderError` with a fixed category. A refused concurrent call is `busy`; use after failure/close is `not_open`.
* **`close()`:** idempotent, any thread; it takes no lock a blocked `read` holds (`WorkerProcess.stop()` wakes the reader). Graceful `CLOSE` only when idle, otherwise terminate → bounded wait → kill → bounded wait. If the process could not be confirmed gone, `close()` raises `DecoderError("containment_failed")` **once**; the slot stays counted until a confirmed exit (`reclaim_abandoned_workers()` or the next start, 4B-2 semantics). `uncertain` / `last_stop_report` expose `stream_terminated`, `operation_interrupted`, `process_unreaped`, `segment_not_released`, `intermediate_launcher`.
* **Single use:** a second `open`, an `open` after `close`, or a reopen after a failure is `invalid_state`; a new stream needs a new decoder (new worker, new segment, sequence from 1).
* **Timings (E7):** `timings` holds the parent's monotonic durations of `start`, `before_connect`, `open` (the `OPEN` exchange), `open_total` (the whole `open`), every `read` (count, last, max, total) and `stop`. Failed steps are included and a failed step already contains the containment and cleanup it triggered (tested: the measured hard-deadline waits are ≥ timeout + margin and ≤ the elapsed wall time). `last_decode_ns` is the worker's own number, kept apart and never a substitute.

## Test seams

`ProcessIsolatedDecoder(..., _worker_factory=WorkerProcess, _python_version=None)` — underscore-prefixed, like `WorkerProcess`'s own seam; the public factory does not expose them. Tests pass a factory that selects the test entry point (`tests/decoder_worker_fixture.py` for the real 4B-3a handler behind a fake `cv2`; `tests/process_fixtures.py` for deliberately broken slots). Production code names no fixture.

## Results (Linux)

* Wave 4B suites: **289 tests OK** (the 245 of 4B-3a, +43 decoder tests, +1 boundary test).
* Full repository: 1194 tests, 4 failures + 56 errors, **identical IDs** to the recorded 60-failure baseline.
* Mutation checks on `decoder.py` and `process.py` (14): limits not authoritative, Python gate lost, `before_connect` ignored, `before_connect` failing open, no parent re-validation, silent `containment_failed`, decoder not dead after a read failure, reads not timed, address stored, worker left running after a refusal, start not timed, token dropped, token always set, constructor creating a worker — every mutant fails at least one test (one mutant was first malformed, so it was re-run correctly).
* No leaked process, slot or segment.

## Deviations and findings

1. **Test seam on the adapter** (`_worker_factory`, `_python_version`): not in the design text; chosen over giving the adapter any entry-point knowledge (E9).
2. **`before_connect` takes the camera label** (`Callable[[str | None], bool]`) so that 4B-3c can build one closure for both cameras.
3. **`close()` raising `containment_failed`:** `RtspPreviewSource` records a close error without raising; the stream is already released, so this only makes the uncertainty visible.
4. **A refusal marks the decoder dead only after an attempt began**; a refused second `open`/`busy` leaves a live decoder untouched.
5. **Windows:** the new `intermediate_launcher` token can appear in a venv; the two older assertions that required an empty `uncertain` were relaxed to ignore only that token. The 3a Windows venv run predates the token.

## Risks that remain

R1 real OpenCV/FFmpeg behaviour (blocking calls, C-level logging, cold import inside the `OPEN` deadline) is untested; R2 stale buffered frames (D7) unchanged; R3 the Windows venv launcher: `intermediate_launcher` is a diagnostic and **not** proof of containment, and D10 still counts the exit of the started process — unchanged and undecided; R4 `open`/`read` block the caller up to the hard deadlines (the manager is not thread-safe, unchanged); R5 no cleanup guarantee after a sudden death of the host; R6 isolation is not a sandbox; R7 the address cannot be wiped from memory.

## Proposed Windows validation (operator, Python 3.13)

Gate for after approval and publication on a test branch:

```
cd /d <repo>
git fetch origin
git checkout <branch named at publication>
git pull
py -3.13 -m unittest tests.test_isolated_protocol tests.test_isolated_states tests.test_isolated_worker_handler tests.test_isolated_boundaries tests.test_isolated_launcher tests.test_isolated_handles tests.test_isolated_slot tests.test_isolated_limit tests.test_isolated_worker_decoder tests.test_isolated_decoder tests.test_opencv_isolated_traceability tests.test_opencv_isolated_wave4b2_traceability -v > w4b3b.txt 2>&1
```
Expected: 289 tests, no FAIL/ERROR, the documented POSIX skips. Then, separately and in a temporary venv: `py -3.13 -m unittest tests.test_isolated_decoder tests.test_isolated_slot.LauncherIdentityTests tests.test_isolated_worker_decoder.DecoderWorkerContainmentTests -v 2>&1` and send the `LAUNCHER-DIAG` lines and whether `IntermediateLauncherTests` and the hang tests pass (the real worker must be gone). Windows is a later operator gate, not claimed here.
