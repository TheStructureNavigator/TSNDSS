# DB-03 Wave 4B-3 — integration of the OpenCV decoder with the isolated worker: approved design

Status: **DESIGN APPROVED BY THE OWNER (E1–E9, with the conditions of section 8.1); 4B-3a (worker side) authorized, 4B-3b and 4B-3c not yet.** Base: `origin/master` `adccdef` (4B-1 and 4B-2 Windows offline verified in the documented scope). This document changes no code, test, manifest, contract or dependency. OpenCV is not installed and no network, RTSP or device is touched. Decisions D13 and D1–D14 are unchanged. No hardware, RTSP or production-readiness claim.

Sources read for this design: `docs/DB-03_WAVE4B_PROCESS_ISOLATION_DESIGN.md` (rev. 3), `docs/DB-03_WAVE4B1_ISOLATED_WORKER.md`, `docs/DB-03_WAVE4B2_SHARED_MEMORY_CONTAINMENT.md`, `Contracts/DSS-CTR-013-…` (REQ-016/018/022/033/052/058/059/064/067), and the code: `opencv_preview_decoder/decoder.py`, `seestar_preview/{source,config,integration}.py`, `device_runtime/{preview_stream,preview_manager,preview_models,preview_readiness}.py`, and the 4B-1/4B-2 package as it is now. Where the design document and the code differ, the code wins and the difference is stated.

---

## 1. Shape of the integration

```
PreviewStreamManager (unchanged)
  └─ PreviewStream (unchanged) ─ RtspPreviewSource (unchanged, internal) ─ ProcessIsolatedDecoder   NEW, parent, implements ImageDecoder
                                                                              │  WorkerProcess (4B-2, one per camera)
                                                                              │  control pipe + shared slot
                                                                              ▼
                                            worker process:  decoder_main ─ DecoderHandler  NEW ─ OpenCvImageDecoder (Wave 4, unchanged) ─ cv2
```

The Wave 2/3/4 and 4B-1/4B-2 contracts (`ImageDecoder`, `PreviewSource`, `RtspPreviewSource`, manager, gate, `WorkerProcess`, protocol, D13) stay as they are; 4B-3 adds three modules and two small additive hooks (section 8).

## 2. API mapping: `ImageDecoder` ⇄ worker protocol

| `ImageDecoder` (parent) | Parent action (`ProcessIsolatedDecoder`) | Wire | Worker action (`DecoderHandler`) |
|---|---|---|---|
| construct | stores config only; no process, no segment, no import | — | — |
| `open(endpoint)` | `WorkerProcess(config).start()` (python check, reserve slot, create segment, spawn, handshake) → `before_connect()` (if it returns false: contain, `readiness_revoked`) → `worker.open(address, open_ms, read_ms)`; the address is read from the endpoint once and not stored | `INIT/HELLO/PING/PONG`, then `OPEN(address, open_timeout_ms, read_timeout_ms)` → `RESULT` | on first `OPEN`: build `OpenCvImageDecoder(OpenCvDecoderConfig(open_ms, read_ms, ImageLimits(max_width, max_height, max_image_bytes=slot_bytes)))` and call its `open(StreamEndpoint("worker", address))`; map `DecoderError.category` → status code |
| `read()` | `worker.read()` → `WorkerImage` → `PreviewPixels(w, h, PixelFormat, bytes)` + `check_limits(limits)`; never returns `None` | `READ` → `IMAGE_READY` (+ slot) or `RESULT(status)` | `decoder.read()` → `PreviewPixels`; write to the slot with `WorkerSegment.publish` (format 1 GRAY8 / 3 BGR8); `IMAGE_READY` |
| `close()` | `worker.stop()`: graceful `CLOSE` (only when idle), terminate, bounded wait, kill, bounded wait; idempotent, any thread | `CLOSE` → `RESULT`, worker exits | `decoder.close()` (releases the `VideoCapture`), reply, `os._exit(0)` |
| `simulated` | `False` | — | — |

Error path: any `WorkerError.category` (a D13 name from `RESULT`, or a parent-originated name) becomes `DecoderError(category)`; `RtspPreviewSource` already passes `DecoderError` categories through to `PreviewSourceError`, and the stream turns it into `lost` with that category. All categories match the stream's category pattern (`^[a-z][a-z0-9_]{0,47}$`; the longest is 24 characters). After any raise the decoder is dead (single use, as in Wave 4); recovery is a new stream after new gate evidence (R7).

## 3. The eleven checks

**3.1 `cv2` and `numpy` import boundaries.** The parent never imports either, statically or lazily: images arrive as `bytes` from the slot. The worker imports `cv2` only inside the existing Wave 4 adapter, which loads it lazily on `open` (so OpenCV is not imported during the handshake, design §6). NumPy is never imported by our code; `cv2` loads it itself and the adapter touches frames only through `.shape`, `.dtype` and `.tobytes()`. The 4B-1 boundary test that bans `opencv_preview_decoder`/`seestar_preview`/`cv2` text in every package file must be narrowed to the three new modules (section 8, E2); `cv2`/`numpy` stay banned as static imports everywhere. Importing the package still imports no OpenCV (existing test, kept).

**3.2 Creating and closing `VideoCapture`.** Only the Wave 4 adapter does it, in the worker's main thread, strictly sequentially (the worker handles one command at a time, so the adapter's "never release under a running call" deferral cannot even arise). `CLOSE` calls `decoder.close()` before the reply; if `release()` blocks, the parent's graceful window (0.5 s) ends, then terminate/kill. A killed worker leaves no capture to release in the parent. The effect on the Seestar of ending an RTSP client abruptly is unknown (U5) and stays flagged by `StopReport.uncertain` (`stream_terminated`).

**3.3 Responsibility for the RTSP address.** `SeestarPreviewConfig.endpoint()` builds it (Wave 3, fixed ports and path); `StreamEndpoint` redacts it. `ProcessIsolatedDecoder.open` reads `endpoint.address` once, passes it to `WorkerProcess.open` (the `OPEN` message, whose repr is redacted) and keeps no copy. The worker passes it to the adapter and keeps none. Python strings cannot be wiped; copies exist in the pipe buffers, the `Open` object and OpenCV until freed — documented, not claimed away.

**3.4 Parameters without exposing addresses in argv or logs.** Unchanged from 4B-1/4B-2: argv holds only two numbers; the environment is an allowlist; stdio is the null device; the address crosses once on the private pipe. Timeouts and limits travel in `OPEN` (timeouts) and `INIT` (width, height, slot size). 4B-3 adds leak tests over reprs, exceptions, transition logs, `StopReport`, the worker's command line and captured stderr, with a sentinel host string.

**3.5 Both cameras and D10.** One `ProcessIsolatedDecoder` (hence one worker) per stream, created by the factory per camera; the manager allows ≤ 2 active streams, `max_live_workers` is 2, so MAIN and WIDE fit exactly. A third live or unreapable worker is refused with `worker_limit` (surfaces as `OPEN_FAILED`). When a stream is lost its source is released immediately (`_lose` → `close()` → `stop()`), so the slot returns before the recovery open. An unreapable worker keeps its slot until a confirmed exit (4B-2). MAIN hanging must not block WIDE's data: they are separate processes, but the manager polls sequentially, so a hung MAIN read costs up to its hard deadline (section 3.8) before WIDE is polled — a pre-existing property of the sequential manager (D6), now bounded instead of unbounded.

**3.6 Geometry, BGR and the 32 MiB limit.** Validation is layered, each layer independent: (1) the Wave 4 adapter in the worker (shape, uint8, channels, dimensions, bytes against `ImageLimits`); (2) the worker maps `PixelFormat` to the wire code and refuses anything but GRAY8/BGR8 (`unsupported_format`; RGB8 cannot come from OpenCV); (3) the parent's `validate_image` (message vs header vs limits); (4) `PreviewPixels` and `check_limits` in the parent; (5) the stream's `check_limits` again (Wave 1). One `IsolatedDecoderConfig.limits: ImageLimits` yields `slot_bytes = max_image_bytes` (D4: 32 MiB default, ceiling 256 MiB by `ABSOLUTE_MAX_SLOT_BYTES`), `max_width`, `max_height`; the composition helper hands the same object to the manager, so the two cannot disagree. Note that the 32 MiB default is the binding limit, not 4096×4096 (BGR: 48 MiB): 3840×2160 BGR is 24.9 MiB and fits; larger frames are refused as `image_exceeds_limits`. The actual S30 Pro frame size is unknown until Wave 5 (D4 re-validation).

**3.7 Slot sequence and integrity.** Reused unchanged from 4B-2: strict sequence (`last + 1`, starting at 1 per worker), identity (nonce, pid) in every header, fences before and after the copy, CRC32 on the parent's own copy, magic/version/slot checks, `worker_protocol_error` and containment on any violation. A new worker means a new segment and sequence restart. The worker side writes through `WorkerSegment.publish` only (the same code the 4B-2 tests exercise).

**3.8 Deadlines and containment.** D12 values unchanged: `start` 10 s, `OPEN` = `open_timeout_ms` + 1.5 s (6.5 s at the Wave 4 default 5000 ms), `READ` = `read_timeout_ms` + 1.0 s (4 s at 3000 ms), graceful `CLOSE` 0.5 s, terminate 1.0 s, kill 1.0 s. The worker's OpenCV soft timeouts are shorter than the parent's hard ones, so a slow-but-alive stream reports `read_failed`/`open_failed` and only a stuck call triggers containment. Worst case seen by the caller (formulas, not measurements): `open_stream` ≈ start 10 + open 6.5 + containment 2.5 ≈ 18.5 s; `poll` per camera ≈ 4 s + containment 2.5 s ≈ 6.5 s; `close` ≈ 2.5 s (Windows ≈ 1.5 s). **New risk R3:** the first `OPEN` also pays the cold `import cv2` in the worker, which counts against the OPEN hard deadline but not against FFmpeg's soft timeout; on a slow Windows disk this could consume the 1.5 s margin. Unmeasured. Proposal: keep the owner-approved placeholders, make the margin visible in `IsolatedDecoderConfig`, measure in Wave 5 (E7).

**3.9 Buffered and delayed frames (D7).** Unchanged by 4B-3 and not mitigated: `host_observed_at` is still the parent's receipt time, exposure time `UNKNOWN`. The pre-existing OpenCV/FFmpeg backlog risk (design §1.4) remains. 4B-3 adds diagnostics only, outside any evidence object: `ProcessIsolatedDecoder.last_read_duration_s` (parent monotonic clock around `READ`→`IMAGE_READY`) and `last_decode_ns` (worker). They exist so Wave 5 can measure the backlog and the isolation cost; they are never used by the freshness classifier.

**3.10 Error classification (D13).** No new code. Mapping: every Wave 4 `DecoderError` category that is in the table maps 1:1 (`status_for_category`); the four `InvalidImage` categories outside the table (`dimensions_out_of_range`, `image_too_large`, `data_not_bytes`, `pixels_missing`) become `worker_internal` (255) by the 4B-1 rule; an unknown status from a worker is `worker_protocol_error`; parent-originated categories (`worker_limit`, `worker_crashed`, `*_deadline_exceeded`, `readiness_revoked`, `containment_failed`, …) are raised by the parent only. A test enumerates both directions for the integrated path.

**3.11 Windows venv launcher and D10.** Operator evidence (4B-2): without venv `popen_pid == worker_pid`; in a venv they differ (the venv redirector is an intermediate process) and in both observed cases — a normal stop and a worker spinning with the GIL held — the real worker was gone after containment. What is not known: the mechanism (job object versus the pipe lifeline) and whether it holds for every state and Windows build. Consequence for D10: `poll()` describes the process the launcher started, so the slot is freed when that process exits; if a real worker ever outlived it, the count would under-report. 4B-3 does not worsen this; options to improve it are decision E6. The decoder worker is unaffected otherwise: handshake, deadlines and the lifeline watchdog do not depend on the pid. The worker holds the GIL inside `cv2` calls only if OpenCV fails to release it (Wave 4 research: it releases it around `read`), which is the case where the lifeline cannot help (L8).

## 4. New production modules (all inside `tsn_dss/engine/opencv_isolated_decoder/`)

| File | Content | Imports (new coupling) |
|---|---|---|
| `decoder.py` | `IsolatedDecoderConfig` (timeouts, `ImageLimits`, `WorkerConfig`, optional margins), `ProcessIsolatedDecoder` (`ImageDecoder`: `open/read/close`, `simulated=False`, redacted repr, `before_connect`, diagnostics), `make_isolated_decoder_factory(config, before_connect=None)` | `..seestar_preview` (`DecoderError`, `StreamEndpoint`), `..device_runtime.preview_models` |
| `worker_decoder.py` | `DecoderHandler(ProductionHandler)`: real `OPEN`/`READ`/`CLOSE`; constructor takes `decoder_factory` (default builds `OpenCvImageDecoder`) so a test entry can inject a fake `cv2` | `..opencv_preview_decoder`, `..device_runtime.preview_models` |
| `decoder_main.py` | the dedicated production entry for the decoder worker (`python -P -m …decoder_main --ctl H --proto 1`), same argv, `serve(ctl, DecoderHandler())` | `.worker_main`, `.worker_decoder` |
| `integration.py` | `build_isolated_preview_manager(...)`: same wiring as Wave 3 `build_preview_manager` but it keeps the gate and wires `before_connect` from a re-check of the same evidence | `..seestar_preview`, `..device_runtime` |

Additive hooks in frozen 4B-2 code (E1, E6): `WorkerConfig.decoder_worker: bool = False` selecting between two named entry constants (`PRODUCTION_ENTRY` unchanged, plus `DECODER_ENTRY`); an `intermediate_launcher` token in `StopReport.uncertain` when the header pid differs from the `Popen` pid. `__init__.py` exports the new names. Nothing in DB-01, DB-02, Waves 1–4, `protocol.py`, `segment.py`, `shm.py`, `states.py` or `worker_main.py` changes.

## 5. Test plan (offline; no OpenCV, no network, no device)

Real worker processes run the real `DecoderHandler` and the real Wave 4 adapter against a fake `cv2` loaded by a test-only entry module `tests/decoder_worker_fixture.py` (production code gets no fixture selection; the fake is `tests/opencv_fakes.py`, reused). New test files:

* `tests/test_isolated_decoder.py` — `ProcessIsolatedDecoder` through `RtspPreviewSource`/`PreviewStream` and the manager: happy path with GRAY8 and BGR8; address reaches the fake `VideoCapture` once and appears nowhere else; open/read/close mapping; every Wave 4 error category through the real worker; unknown status; `python_unsupported`; `worker_limit`; `before_connect` false → no `OPEN` reaches the worker and `readiness_revoked`; refused gate → no process created; recovery only through a new stream after new evidence (reuse the Wave 3 flow); both cameras at once; MAIN hung while WIDE keeps delivering; hard deadlines for a fake `VideoCapture` that blocks in `read`/construction (soft timeout cannot help); close during read from another thread; double close; parent-side limits (oversize frame refused in the worker and, with a lying worker, in the parent).
* `tests/test_isolated_worker_decoder.py` — `DecoderHandler` in process: command sequencing, `busy`/`not_open`/`invalid_state`, category-to-status mapping, publish only through the slot API, `CLOSE` releases the capture, no OpenCV import before `OPEN`.
* `tests/test_isolated_decoder_boundaries.py` — static: coupling confined to the four modules, no `cv2`/`numpy` import anywhere in the package, no `Connection.send/recv`, no address literals, no command vocabulary, the manager remains the only entry (the new factory is not an alternative way to open a stream), argv unchanged.
* `tests/test_isolated_decoder_integration.py` — end to end with a fake runtime/connection (Wave 3 support) and `build_isolated_preview_manager`: gate → manager → worker → image evidence; image evidence stays Preview (REQ-018/033/064 boundary), no Capture/Frame/Dataset object exists.
* `tests/test_opencv_isolated_wave4b3_traceability.py` + `tests/opencv_isolated_wave4b3_traceability.json` — PRV-ISO-034…; REQ claims limited to boundary level (REQ-022, 052, 059, 067; REQ-018/033/064 `partially_verified`/`boundary_verified` as in Wave 4, never `verified`); `not_claimed`: hardware, RTSP behaviour, real OpenCV, staleness, memory containment, sandbox, Windows (until run).
* `docs/DB-03_WAVE4B3_INTEGRATION.md` — results, limits, Windows commands.

Existing 4B-1/4B-2 tests are untouched except the boundary test narrowing (E2) and the manifest/doc cross-references.

## 6. Acceptance criteria

**Offline, Linux (CPython 3.13):** all new suites pass; the existing Wave 4B suites (207) still pass with the narrowed boundary test; full repository run identical to the 60-failure baseline; mutation checks for the new logic (category mapping, `before_connect` placement, address non-retention, limit propagation) all killed; no leaked process, slot or segment after any test; no OpenCV or NumPy needed to run anything.

**Offline, Windows (Python 3.13, with and without venv):** the full Wave 4B suite plus the new suites pass; `LauncherIdentityTests` and the new hang tests still show the real worker gone; skips only where documented. No claim before the operator's run.

**Later, RTSP on hardware (Wave 5; not authorized by this design):** a written attended procedure, one camera first; installed OpenCV version recorded; the first `OPEN` duration including cold import; real frame sizes against the 32 MiB slot (D4); backlog and glass-to-glass freshness (D7); whether killing a client mid-session upsets the Seestar (U5); behavior of the worker spinning in real FFmpeg; Windows memory and start-up; calibration of D12. Each result is a measurement, not a verdict.

## 7. Risks

| # | Risk | Mitigation / status |
|---|---|---|
| R1 | Real `cv2` behavior differs from the fake (blocking calls, version quirks, FFmpeg logging an address to C-level stderr) | stdio is the null device; Wave 5 measures; no claim now |
| R2 | Stale buffered frames look fresh (D7) | unchanged; diagnostics added; Wave 5 |
| R3 | Cold `import cv2` consumes the OPEN hard-deadline margin on Windows | margin configurable; calibrate in Wave 5 (E7) |
| R4 | Intermediate Windows launcher: slot freed on the launcher's exit while the real worker lives | observed benign in two cases; mechanism unknown; options in E6 |
| R5 | `open_stream`/`poll` block the calling thread up to 18.5 s / 6.5 s per camera; the manager is not thread-safe (Wave 2) | bounded now; documented; no manager change |
| R6 | Two 32 MiB mappings + worker frames + parent copies ≈ 160 MiB per camera worst case; no memory cap on Windows (D8) | `ImageLimits`; documented |
| R7 | Process isolation is not a sandbox; an FFmpeg exploit runs as the same user | stated, unchanged |
| R8 | Address strings cannot be wiped from memory | documented, not claimed |
| R9 | Killing a client with an open session may leave the camera holding it (U5) | flagged by `uncertain`; Wave 5 |
| R10 | A second composition path (`integration.py`) could be mistaken for a way around the manager | it only builds the manager; boundary test; `RtspPreviewSource` stays internal |

## 8. Decisions requested

| # | Decision | Recommendation |
|---|---|---|
| E1 | Add a separate decoder entry module and select it through `WorkerConfig.decoder_worker` (default `False`) instead of changing `PRODUCTION_ENTRY`. Additive change to frozen `process.py`. Changing `PRODUCTION_ENTRY` would invalidate 4B-1/4B-2 tests that assert production `OPEN`/`READ` = `invalid_state` and the REQ-022 manifest text. | Approve the additive flag |
| E2 | Narrow the 4B-1 boundary test: coupling to `seestar_preview`, `opencv_preview_decoder` and `device_runtime` only in `decoder.py`, `worker_decoder.py`, `decoder_main.py`, `integration.py`; `cv2`/`numpy` still banned everywhere. (The design said "only inside `worker_main.py`"; a separate module keeps `worker_main.py` frozen.) | Approve |
| E3 | Python ≥ 3.13 check at `open()` (inside `WorkerProcess`), not in the factory, so the category `python_unsupported` reaches the stream instead of the opaque `factory_error`; plus a public `require_supported_python()` for startup. Interprets C1 ("at creation of the isolated decoder") as "when the worker is created". | Approve |
| E4 | `before_connect` wired in a new composition helper that re-checks a second `ReadinessGate` built from the same runtime, connection and config (no `not_before`, the manager already applied it); Wave 3 `build_preview_manager` stays untouched. | Approve |
| E5 | One `ImageLimits` object drives slot size, worker limits and the manager; default 32 MiB slot stays (D4). | Approve |
| E6 | Windows venv launcher and D10: (A) accept and document; (B) additionally report `intermediate_launcher` in `StopReport.uncertain` when header pid ≠ `Popen` pid (additive to `process.py`); (C) confirm the real worker's exit by EOF on the parent's pipe end before releasing the slot (changes `stop()`/D10 semantics; deeper). | **B** now; C only if Wave 5 shows a survivor |
| E7 | Keep D12 placeholders including the 1.5 s OPEN margin; expose the margin in `IsolatedDecoderConfig`; calibrate (cold import) in Wave 5. Alternative: raise the default margin now. | Keep, calibrate |
| E8 | D13 untouched. Confirm that categories outside the table keep mapping to `worker_internal`. | Confirm |
| E9 | Test-only entry `tests/decoder_worker_fixture.py` injects the fake `cv2` into the real `DecoderHandler`; production has no fixture selection. | Approve |

If E1–E9 are approved as recommended, no frozen 4B-1/4B-2 *behavior* changes: only two additive hooks (E1, E6B) and one test narrowing (E2).

### 8.1 Owner decisions and binding conditions (recorded)

| # | Decision | Binding conditions |
|---|---|---|
| E1 | APPROVED | Separate decoder entry point; `PRODUCTION_ENTRY` stays unchanged; `WorkerConfig.decoder_worker` defaults to `False`. |
| E2 | APPROVED | Coupling to Wave 3/4 (`seestar_preview`, `opencv_preview_decoder`, `device_runtime`) only in the four new modules `decoder.py`, `worker_decoder.py`, `decoder_main.py`, `integration.py`. **No exceptions beyond them.** `cv2`/`numpy` stay banned as imports everywhere. |
| E3 | APPROVED | Python ≥ 3.13 is checked at the `open` boundary of the adapter; **the constructor creates no worker**. The existing `WorkerProcess` gate stays. |
| E4 | APPROVED | `before_connect` uses the same decision sources (same runtime, connection, config, gate type) and **must not be bypassable by the new composition**. |
| E5 | APPROVED | One `ImageLimits` is authoritative for adapter, shared memory, worker and manager. |
| E6 | APPROVED as option B | `intermediate_launcher` is a **diagnostic token, not proof of safe termination**. D10 and containment are **not** changed without a separate decision. |
| E7 | APPROVED | D12 values unchanged. The parent **measures the real duration of each operation**, including timeouts and cleanup. |
| E8 | CONFIRMED | D13 unchanged; categories outside the table map to `worker_internal` by the existing rule. |
| E9 | APPROVED | The fake `cv2` exists only in a test entry point; no production fixture selection. |

Phase gates: 4B-3a (worker side) first, then a review stop; 4B-3b and 4B-3c only after separate approval. Nothing is committed without consent.

## 9. Implementation plan

1. **4B-3a, worker side:** `worker_decoder.py`, `decoder_main.py`, `WorkerConfig.decoder_worker` + entry constant, `tests/decoder_worker_fixture.py`, `test_isolated_worker_decoder.py`. Review gate.
2. **4B-3b, parent side:** `decoder.py` (with `before_connect`), `IsolatedDecoderConfig`, factory, `intermediate_launcher` token (if E6B), `test_isolated_decoder.py`, boundary test narrowing and `test_isolated_decoder_boundaries.py`. Review gate.
3. **4B-3c, composition and closure:** `integration.py`, `test_isolated_decoder_integration.py`, manifest and test, `docs/DB-03_WAVE4B3_INTEGRATION.md`, mutation checks, full baseline comparison, Windows commands. Stop for review; commit only on approval; Windows validation on a test branch as for 4B-1/4B-2.
4. Wave 5 (hardware procedure) is a separate design gate.

Nothing is committed. No hardware, RTSP, OpenCV or production-readiness claim is made or implied.
