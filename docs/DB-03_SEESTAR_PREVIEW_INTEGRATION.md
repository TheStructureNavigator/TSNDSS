# DB-03 Seestar preview integration (Wave 3, offline)

Status: **offline only; no hardware claim.** The integration is verified against the DB-02 provider running on a fake transport with synthetic fixtures, and against fake decoders. No RTSP connection is made, no decoder library exists in the repository, and no S30Lab or `seestarpy` code is used. The app-state shape used for readiness has not been observed on a device with an active camera (DB-02 limitation L2). Lifecycle, gate and freshness semantics are the local DB-03 semantics of Waves 1 and 2.

Package: `tsn_dss/engine/seestar_preview/`. It depends only on `device_runtime` (neutral preview modules) and on `seestar_provider.config`, `.errors` and `.normalize`. It never imports DB-02's transport, authentication, provider or protocol modules. `device_runtime` and `seestar_provider` do not reference it.

## Flow

```
DB-02 provider (read-only) -> ProviderRuntime.read_telemetry -> SeestarReadinessEvidenceProvider
   -> ReadinessGate (Wave 2) -> PreviewStreamManager (Wave 2) -> (internal) RtspPreviewSource -> injected ImageDecoder
   -> PreviewStream -> pixels and PreviewImageEvidence
```

`build_preview_manager(...)` wires this. Building it reads nothing and creates no decoder.

## Official API and its limits

A preview stream is opened one way: `build_preview_manager(...)` returns a `PreviewStreamManager`, and `open_stream(camera)` opens a stream only after the readiness gate allowed it. The package exports a configuration, a read-only evidence provider, the `ImageDecoder` interface that adapters implement, and the builder. `RtspPreviewSource` and `make_source_factory` live in the internal module `source` and are not exported; tests and the builder import them from there. Official results (`OpenOutcome`, `PollResult`, `PreviewView`, `CloseReport`, pixels) never hand out a stream, source or decoder.

**This is an API boundary, not a security boundary.** Known ways around the gate, all of which require deliberately leaving the official API:

1. importing `seestar_preview.source` and opening a `RtspPreviewSource` directly;
2. constructing a `PreviewStream` from `device_runtime.preview_stream` with any source (a neutral Wave 1 building block);
3. reaching private attributes of the manager (`_factory`, `_streams`);
4. building a `PreviewStreamManager` by hand with a permissive evidence provider, since the gate is only as strict as the evidence it is given (the official builder always supplies the DB-02 provider);
5. calling an adapter's own `open` (a Wave 4 decoder is ordinary code).

Two limits remain even on the official path: the gate decision and the actual open are not atomic, so the device state can change between them; and nothing verifies that opening the stream of an active camera has no side effect on the device (open question since Research Wave 1). Tests pin the boundary (exports, construction sites, ordering of gate before factory and open) and fail if it is widened by accident; they do not prevent a determined caller.

## Configuration

`SeestarPreviewConfig(host, cameras=("main", "wide"), readiness_max_age_s=10.0)`. The host is validated with DB-02's `validate_host` and is redacted everywhere. Ports and the path are fixed: `main` 4554, `wide` 4555, path `stream`, taken from `normalize.PREVIEW_PORTS`; they cannot be configured. `config.endpoint(camera)` returns a `StreamEndpoint` whose address exists in exactly one place (`config.py`) and is redacted in every representation.

## Readiness evidence

Per camera, from the `app.<camera>.*` telemetry items of a fresh DB-02 sample (five items: mode, stage, state, RTSP state, RTSP port):

- AVAILABLE only if all five are KNOWN and match the DB-02 rule (`scenery`, `RTSP`, `working`, `working`, the camera's own port);
- UNAVAILABLE if all five are KNOWN and do not match;
- UNKNOWN if any item is absent, unknown or stale. This is stricter than `normalize.preview_availability` for partial replies and can only withhold AVAILABLE, never grant it (tested by an equivalence matrix).

Evidence carries the Connection id, provider id and device reference of its Connection and the **host** observation time of the sample. No device timestamp is read or invented. `Connection` state is not an input. Any failure to read yields no evidence. Every call reads anew.

## Source and decoder seam

`ImageDecoder` has `open(endpoint)`, `read()`, `close()` only. `RtspPreviewSource` maps every decoder fault to a `PreviewSourceError` with a fixed category (the decoder's own message is discarded), releases the decoder exactly once, never reopens, invents no time, and marks evidence `simulated` only when the decoder declares itself simulated. A decoder's `read()` must return promptly; honoring that with a blocking library is the Wave 4 adapter's job.

## Not in Wave 3

OpenCV or any real decoder, any network connection, timeouts and hang handling, camera start/stop/arm/park (DB-04/05), hardware validation (Wave 5).
