# DB-03 Wave 4B-3a — decoder worker side (offline)

Status: **Windows offline verified, in the scenarios tested (accepted); Linux verified.** Implements the worker side of `docs/DB-03_WAVE4B3_INTEGRATION_DESIGN.md` (owner decisions E1–E9 with their conditions). Base `master` `dd70fbf`; implementation commit `8c55612`. **Fake `cv2` only: no real OpenCV/FFmpeg, no RTSP, no network, no device. Not hardware verified; not production ready.** 4B-3b (parent adapter) and 4B-3c (composition, manifest) are not started.

## What exists

| File | Change |
|---|---|
| `worker_decoder.py` (new) | `DecoderHandler(ProductionHandler)`: `OPEN`/`READ`/`CLOSE` over the Wave 4 adapter. The adapter is imported and built lazily at the first `OPEN` through an injectable `decoder_factory` (default: the real `OpenCvImageDecoder`). Limits come from `INIT` (width, height, slot = maximum image bytes; `max_buffer_bytes` = slot), timeouts from `OPEN`. Images are written only through `WorkerSegment.publish`, announced with `IMAGE_READY` (`decode_ns` = worker monotonic duration of the adapter's `read`, diagnostic only). Statuses only, no text; categories outside the D13 table → `worker_internal`; an unexpected exception → `worker_internal`. |
| `decoder_main.py` (new) | Dedicated entry (`python -P -m …decoder_main --ctl H --proto 1`), same two-number command line and serve loop/lifeline as `worker_main`. |
| `process.py` | Additive only: constant `DECODER_ENTRY`; `WorkerConfig.decoder_worker: bool = False` (needs `slot_bytes > 0`); the entry is chosen from the flag when no test seam is given. `PRODUCTION_ENTRY` and the default behaviour are unchanged. |
| `__init__.py` | exports `DECODER_ENTRY`. |
| `worker_main.py`, `protocol.py`, `segment.py`, `shm.py`, `states.py`, D13 | **unchanged.** |
| tests | `tests/decoder_worker_fixture.py` (test-only entry: real handler and adapter, fake `cv2` from `tests/opencv_fakes.py`), `tests/test_isolated_worker_decoder.py` (35), `tests/test_isolated_boundaries.py` (narrowed per E2, +3 tests). |

## Mapping implemented (worker side)

`OPEN` → `OpenCvImageDecoder(OpenCvDecoderConfig(open_ms, read_ms, ImageLimits(max_w, max_h, slot)))` → `open(StreamEndpoint("worker", address))`; success `RESULT(0)`, failure `RESULT(code)`. `READ` → `decoder.read()` → `PreviewPixels` → format code (GRAY8 = 1, BGR8 = 3; anything else `unsupported_format`) → size check against the slot → `publish(seq, …)` → `IMAGE_READY(op, seq, w, h, fmt, nbytes, decode_ns)`. `CLOSE` → `decoder.close()` once, `RESULT(0)` (`release_failed` if it raises), exit. The decoder is single use: after any error status every later `OPEN`/`READ` is `invalid_state`; the parent ends the worker. The address is passed on and not kept.

## Deviations and decisions taken within the approved design

1. `decoder_main` repeats the 15 lines of argument parsing instead of adding a parameter to `worker_main` (which stays frozen); a test checks the two exit with 64 alike.
2. A refused command (`not_open`, `invalid_state`) also marks the handler dead: the parent treats any non-OK status as failure anyway, and the simple rule keeps the state machine of the worker trivial.
3. E3 (Python check at the adapter's `open`), E4–E5 (gate re-check, one `ImageLimits`), E6 (`intermediate_launcher` token) and E7 (parent-side duration measurement) are **parent-side and belong to 4B-3b/4B-3c**. Open point for 4B-3b: measuring the real duration of start/open/read/stop is best done where the deadlines live (`WorkerProcess`, additive) rather than only in the adapter — to be confirmed at that gate.
4. The Linux sandbox has no OpenCV; the one test that starts the production decoder entry checks the `opencv_unavailable` answer and skips the `OPEN` step if `cv2` happens to be installed (so that no test can ever try a real connection).

## Results (Linux)

* Wave 4B suites: **245 tests OK** (4B-1/4B-2: 207 + 3 boundary; 4B-3a: 35).
* Full repository: 1150 tests, 4 failures + 56 errors, **identical IDs** to the recorded 60-failure baseline.
* Mutation checks on `worker_decoder.py` (constant status, swapped limits, no release, no slot-size check, no dead flag, swapped timeouts, BGR as gray, sequence kept constant, eager OpenCV import): all 9 mutants fail at least one test.
* No leaked process, slot or segment.

## Windows acceptance record (operator, Python 3.13, commit `8c55612`)

| Configuration | Result |
|---|---|
| Windows, Python 3.13, **no venv** | full 4B-3a suite: 245 tests in 94.945 s, OK (5 skipped), 0 FAIL, 0 ERROR. `LauncherIdentityTests`: `ok_ops` popen_pid=7744 worker_pid=7744 same=True; `spin_read` popen_pid=19260 worker_pid=19260 same=True. |
| Windows, Python 3.13, **temporary venv** (since removed) | `LauncherIdentityTests` and `DecoderWorkerContainmentTests`: 6 tests in 8.188 s, OK, 0 FAIL, 0 ERROR. `ok_ops` popen_pid=7220 worker_pid=5832 same=**False**; `spin_read` popen_pid=9372 worker_pid=8904 same=**False**. |

Scope of the claim: the worker side (real `DecoderHandler` and real Wave 4 adapter behind a fake `cv2`) behaves as tested on Windows, including hard deadlines against a blocked, spinning and crashing capture and the real worker being gone after containment. The full suite ran once without venv; the venv run covered the two classes named above only.

Not established: any behaviour of real OpenCV/FFmpeg or RTSP; hardware; production readiness. In a venv the process the launcher starts is an intermediate one (pids differ); the observed runs show the real worker gone after containment, but the mechanism is unknown and `D10` still counts `Popen.poll()` of the started process, so the slot guarantee is only as strong as that observation — unchanged and undecided (`intermediate_launcher` diagnostics belong to 4B-3b). There is no cleanup guarantee after a sudden death of the host (`atexit` is best effort; before the handshake the POSIX segment name relies on CPython's resource tracker; on Windows the mapping disappears with its last handle). Process isolation is not a sandbox.

## Limits

Everything above runs against a fake `cv2`; real OpenCV/FFmpeg behaviour (blocking calls, version quirks, C-level logging, cold import time inside the `OPEN` deadline, stale buffered frames) is untested. Process isolation is not a sandbox. The address cannot be wiped from memory. No Windows run yet.

## Windows validation (operator, Python 3.13)

```
cd /d <repo>
git fetch origin
git checkout feat/db03-wave4b3a-worker      (or the test branch named at publication)
py -3.13 -m unittest tests.test_isolated_protocol tests.test_isolated_states tests.test_isolated_worker_handler tests.test_isolated_boundaries tests.test_isolated_launcher tests.test_isolated_handles tests.test_isolated_slot tests.test_isolated_limit tests.test_isolated_worker_decoder tests.test_opencv_isolated_traceability tests.test_opencv_isolated_wave4b2_traceability -v > w4b3a.txt 2>&1
```

Expected: 245 tests, no FAIL/ERROR. If run inside a venv, include the `LAUNCHER-DIAG` lines.
