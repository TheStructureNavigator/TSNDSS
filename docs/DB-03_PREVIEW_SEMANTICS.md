# DB-03 preview semantics (Wave 1, local)

Status: **local DB-03 semantics, not part of DSS-CTR-013** (decision D2: no contract revision in Wave 1). DSS-CTR-013 defines Preview as read-only runtime image or stream evidence (REQ-016, 018, 022, 033, 059) and is silent on stream lifecycle and image freshness. REQ-058 concerns Telemetry samples only and is **not** reinterpreted as an image requirement (D3).

Nothing in Wave 1 has been run against hardware. All default values below are provisional.

## Modules (`tsn_dss/engine/device_runtime/`)

| Module | Content |
|---|---|
| `preview_models.py` | `PreviewPixels`, `SourceImage`, `PreviewImageEvidence`, `ImageLimits`, enums, errors |
| `preview_freshness.py` | `FreshnessPolicy`, `ArrivalRecord`, `classify_image_freshness` (pure) |
| `preview_stream.py` | `PreviewStream`, `PixelBuffer`, `PreviewSource` protocol, state and transition table |
| `preview_simulator.py` | `SimulatedPreviewSource` and scenarios |

The package `__init__` is unchanged; use the submodules directly.

## Metadata and pixels

`PreviewImageEvidence` is metadata only (identity, host receipt time, shape, SHA-256 digest, freshness, `simulated`). `PreviewPixels` holds decoded bytes in memory (row-major, tightly packed; GRAY8, RGB8, BGR8). Pixels are never written to disk. Evidence is runtime evidence only, never a canonical Capture or Frame record.

## Time

`host_observed_at` is the host receipt time. The physical exposure time is **unknown** unless the source reports one, and such a report is an unverified claim (`provider_reported_at`).

## Stream states

IDLE → OPENING → RECEIVING ⇄ STALLED; OPENING/RECEIVING/STALLED → LOST; any state → CLOSED (idempotent). LOST and CLOSED are never reactivated: reopening means a new stream with a new id. Source faults become LOST and do not raise; the source is released exactly once. A stream never starts, stops or configures a device; the source protocol is `open`, `read`, `close`.

## Freshness (precedence order)

1. no image → UNKNOWN (`no_image_yet`)
2. host clock inconsistent with history → UNKNOWN (`clock_regression`)
3. stream not receiving → STALE (`stream_not_receiving`)
4. age > `max_age` → STALE (`exceeds_max_age`); age exactly `max_age` is FRESH
5. trailing run of identical digests with count ≥ `repeat_threshold` and span ≥ `repeat_span`:
   all images carry a source time that does not advance → STALE (`stall_corroborated`); source time advances → FRESH; otherwise → UNKNOWN (`repeated_content_uncorroborated`)
6. otherwise FRESH

Repeated content alone is never STALE. Defaults: `max_age` 5 s, `repeat_threshold` 5, `repeat_span` 10 s (to be calibrated on hardware).

## Limits

Defaults: 4096×4096 per image, 32 MiB per image, buffer ≤ 4 images and ≤ 64 MiB. Absolute ceilings: 16384 px per side, 256 MiB per image, 16 images. Oversize or malformed images make the stream LOST (`invalid_image`) and are not stored.

## Non-goals of Wave 1

RTSP, decoders, OpenCV, Seestar code, stream orchestration across cameras, readiness gating, camera start/stop (DB-04/05), hardware evidence.
