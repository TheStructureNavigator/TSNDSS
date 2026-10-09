# DB-03 optional OpenCV decoder (Wave 4, offline)

Status: **offline only; no hardware claim; no real OpenCV was ever run.** The adapter is tested against a fake `cv2` module. No real RTSP connection, no OpenCV installation and no NumPy are used. OpenCV is **not** a dependency of this repository (`requirements.txt` is unchanged); the adapter imports it lazily and refuses cleanly when it is absent.

Package: `tsn_dss/engine/opencv_preview_decoder/`. It implements the existing `seestar_preview.ImageDecoder` interface unchanged. No Wave 1–3 runtime, DB-01 or DB-02 code was modified.

## 1. Research results (Phase A)

Findings come from the sources listed at the end. Items marked *unverified* were not confirmed from a source or by running OpenCV.

**Version and licence**
- Latest PyPI release of `opencv-python-headless` at the time of research: **5.0.0.93**, uploaded 2026-07-02, `requires_python >=3.6`, wheels tagged `cp37-abi3` for Linux (manylinux2014/2_28, x86_64 and aarch64), Windows (win32, win_amd64) and macOS (arm64, x86_64), plus an sdist.
- Licence: OpenCV under **Apache 2.0** (the PyPI field says "Apache 2.0"); the packaging repository scripts are MIT. **All wheels ship FFmpeg licensed under LGPLv2.1** (PyPI description). Non-headless Linux wheels additionally ship Qt 5 under LGPLv3, which is a reason to prefer the **headless** wheel for a decoder that never shows a window.
- Consequence of the LGPL FFmpeg: it is distributed dynamically inside the wheel, which is the intended use; redistributing this repository together with a bundled wheel would need the LGPL notices. Not a legal opinion.
- The adapter requires **OpenCV ≥ 4.8** because a maintainer states that both timeout properties are available from 4.8.0 in packages (opencv-python issue 788). **Behavior on the 5.x line is unverified**: its videoio code was not read or run.

**Timeout mechanisms**
- Constants (OpenCV header): `CAP_PROP_OPEN_TIMEOUT_MSEC = 53` and `CAP_PROP_READ_TIMEOUT_MSEC = 54`, both documented as *open-only* and *applicable for FFmpeg and GStreamer back-ends only*. They are passed as `params` to `VideoCapture(filename, apiPreference, params)`.
- Implementation (FFmpeg backend source): defaults are **30000 ms for open and 30000 ms for read**. A libavformat *interrupt callback* returns "abort" once the elapsed time exceeds `timeout_after_ms`; `open()` arms it with the open timeout (which therefore spans the whole open including stream probing) and `grabFrame()` with the read timeout. Both are reset to 0 afterwards.
- Reports that read timeouts did not work in `opencv-python` (issue 788: blocking for about 90 s) were closed by a maintainer in June 2023 as fixed in 4.8.0. Forum reports of `read()` stalling for tens of seconds exist; their versions are unknown.
- `OPENCV_FFMPEG_CAPTURE_OPTIONS` (environment, process-global) replaces the backend's default RTSP options (`prefer_tcp`) when set. The adapter never reads or writes it; an operator-set value still affects the capture.

**Blocking risk**
- `open()`, `read()` and `release()` are all synchronous C++ calls. The Python bindings release the GIL around them (`ERRWRAP2` / `PyAllowThreads`), so another Python thread keeps running, but the *calling* thread is stuck until the call returns.
- In the FFmpeg backend source read, `close()` takes **no lock**, while `open()` does. Calling `release()` from one thread while another is inside `read()` is therefore a data race on the same capture object and must be treated as unsafe.
- Timeouts depend on FFmpeg checking the interrupt callback. They do not cover everything: a blocking name resolution (hostnames; IP literals avoid it), a codec or driver stall, or a bug in a given build can exceed them. *Unverified* for each OpenCV/FFmpeg build; only a test against the real build can show it.

**Soft timeout versus hard containment**
- **Soft timeout** (what exists here): a request, honored by cooperating code, that a call give up after N ms. The thread stays in the same process and the same address space; if the cooperation fails, nothing recovers it.
- **Hard containment**: a mechanism that works even when the code never cooperates, which requires being able to kill the thing that is stuck and have the operating system reclaim its sockets and memory, in practice a separate process. A worker *thread* can give the caller its control back but cannot be killed and cannot safely release the capture it holds.

## 2. Architecture

```
build_preview_manager(decoder_factory=make_opencv_decoder_factory(config)) -> manager -> RtspPreviewSource -> OpenCvImageDecoder -> cv2.VideoCapture(FFMPEG)
```

- `OpenCvDecoderConfig(open_timeout_ms=5000, read_timeout_ms=3000, limits=ImageLimits())`: timeouts 100–60000 ms (tighter than OpenCV's 30 s defaults), limits reuse Wave 1 `ImageLimits` (4096×4096, 32 MiB per image by default).
- `OpenCvImageDecoder.open(endpoint)`: lazy import; refuses a missing import (`opencv_unavailable`), a version below 4.8 (`opencv_version_unsupported`) or unreadable (`opencv_version_unknown`), a module without the needed constants (`opencv_incompatible`); builds exactly one `cv2.VideoCapture(address, cv2.CAP_FFMPEG, [open timeout, read timeout])`; refuses a capture that is not open (`open_failed`) or whose `getBackendName()` is not `FFMPEG` (`backend_mismatch`, `backend_unverified`). There is no fallback backend, and a decoder is single use (`invalid_state` on a second open).
- `read()`: one `cap.read()`. A failed read (`ok` false, exception) is `read_failed`, i.e. a lost stream, because OpenCV cannot tell end of stream, timeout and decode error apart and a capture whose read was aborted is not trusted afterwards. There is **no reconnect and no retry**; recovery is a new stream after new readiness evidence (Wave 2). A frame is validated *before* it is copied: 2-D or 1-/3-channel `uint8` only (GRAY8, BGR8), width, height and byte size within `ImageLimits`; anything else is `unsupported_depth`, `unsupported_format`, `dimensions_exceed_limits`, `image_exceeds_limits` or `decoder_bad_output`.
- `close()`: releases exactly once and is idempotent. If another thread is still inside `open` or `read`, the release is **deferred** until that call returns (`release_pending` is visible), never executed underneath it. The deferred release runs after the running call has let go of the lock, and is re-checked there, so a `close` arriving at the last moment is not lost (a lost-wakeup window found and fixed during the pre-publication review; `deferred_release_error` records a failing deferred release). A concurrent second `open` or `read` is refused (`busy`).
- Every failure is a `DecoderError` with a fixed category; OpenCV's messages (which may contain the stream address) are dropped and the exception chain is cut. The decoder keeps no copy of the address, and its `repr` is redacted. It never calls `VideoCapture.set`, starts no thread and no process, and sends no control request of any kind.

## 3. Limits that remain (read before any hardware use)

1. **There is no hard timeout.** The only bounds are OpenCV's soft timeouts. If FFmpeg never returns, the thread that called `open` or `read` is stuck, and with the single-threaded Wave 1/2 manager that blocks polling of **both** cameras.
2. A deferred `close` releases the capture only when the blocked call finally returns. If it never returns, the capture, its socket and its memory are never released for the life of the process.
3. The manager's own liveness (`STALLED`, staleness) can only run between calls; it cannot interrupt a blocked one.
4. Behavior of OpenCV 5.x, of the actual FFmpeg build inside the wheel, of name resolution and of the Seestar's RTSP server under a timeout is unverified.
5. Whether opening the stream of an active camera has any side effect on the device is still unknown (open since Research Wave 1).

## 4. Hard-containment variants (decision taken: C, as a later wave; nothing implemented here)

**Decision (owner):** variant C, one decoder process per camera, is the target architecture and becomes a separate wave, **DB-03 Wave 4B**, required before any unattended or continuous hardware use. Variant D is allowed only as a transitional way to run one short, attended, single-camera validation, after separate consent. This wave implements neither.

| Variant | What it gives | Cost and consequences |
|---|---|---|
| **A. Soft timeouts only (this wave)** | Cooperative bounds on open/read | No containment of a true hang. Needs an operator or external watchdog able to kill the process. |
| **B. Worker thread with a deadline** | The caller regains control after a deadline | The stuck thread and capture are leaked, `release()` cannot safely run while the thread is inside OpenCV, leaks accumulate per hang, and the process may need a hard exit. Gives liveness for the manager but **not** resource containment. Low effort. |
| **C. One decoder process per camera** (a `ProcessIsolatedDecoder` implementing `ImageDecoder`; the child runs the code of this wave) | `kill` frees the socket and memory whatever the child was doing; also contains crashes and segfaults in FFmpeg | Frame transport (pipe or shared memory, up to 32 MiB per image), process start-up (slower on Windows `spawn`), a heartbeat and deadline protocol, new tests on Windows, and a deliberate exception to the package boundary tests that forbid `subprocess` and `multiprocessing`. The `ImageDecoder` interface does **not** need to change: `read()` simply enforces its own deadline and `close()` kills. |
| **D. Run the whole preview under an external supervisor** | A hung process is killed and restarted | Coarse: everything in the process, including the other camera, is lost. Fine for a first attended validation. |

Interface finding: `ImageDecoder` and `RtspPreviewSource` need **no change** for any variant. What the interface cannot do is *force* a decoder to return promptly; that is a property each implementation must provide, and the in-process OpenCV implementation can only approximate it.

## 5. Fitness for a first controlled hardware validation

Suitable **only** as an attended, short, single-camera run, started from a disposable process that the operator can kill from outside (variant D), with the readiness gate required and no unattended operation. Not suitable for unattended or two-camera continuous use until variant C (or an equivalent) is decided. A real run is also the only way to answer limits 4 and 5 above.

## Sources
- OpenCV `videoio.hpp` (4.x): timeout property constants, `CAP_FFMPEG = 1900`, constructors with `params`.
- OpenCV `cap_ffmpeg_impl.hpp` (4.x): default timeouts, interrupt callback, `OPENCV_FFMPEG_CAPTURE_OPTIONS`, `close()` without a lock.
- OpenCV `cv2_util.hpp` (4.x): `ERRWRAP2` and `PyAllowThreads` release the GIL.
- PyPI `opencv-python-headless` 5.0.0.93 metadata (licence, wheels, FFmpeg LGPLv2.1 statement).
- opencv-python issue 788 (read timeout reports and the 4.8.0 resolution).
