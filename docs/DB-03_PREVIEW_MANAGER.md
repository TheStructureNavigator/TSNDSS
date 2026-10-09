# DB-03 preview manager and readiness gate (Wave 2, local)

Status: **local DB-03 semantics, not part of DSS-CTR-013**. Offline evidence only: no hardware, RTSP, decoder or Seestar code is involved. REQ-058 (Telemetry) is not reinterpreted, and REQ-057/REQ-060 (Command safety evidence) stay with DB-04.

Modules in `tsn_dss/engine/device_runtime/`: `preview_readiness.py` (gate) and `preview_manager.py` (manager). `device_runtime/__init__.py` is unchanged; import the submodules directly.

## Manager

`PreviewStreamManager(connection=, gate=, source_factory=, cameras=, ...)` serves one Connection for its whole life. A new Connection means a new manager.

- 1 or 2 distinct camera labels; at most 2 active streams (`OPENING`, `RECEIVING`, `STALLED`); at most 1 active stream per camera.
- A stream opens only through an explicit `open_stream(label)` that passes the gate. Construction, `poll`, `view` and `close_*` never open anything.
- `open_stream` returns an `OpenOutcome` and does not raise for refusals: `unknown_camera`, `connection_not_usable`, `already_active`, `stream_limit`, `clock_failure`, `gate_denied`, `source_factory_failed`, `open_failed`.
- Order of checks: camera known → Connection usable → not already active → limit → clock → gate → source factory → `stream.open()`. The factory is called only after the gate allows, so a denial never builds or opens a source.
- Cameras are isolated. `poll_all` polls each camera on its own; exceptions are converted to fixed category tokens (`unexpected_error`, source categories) and carry no device data.
- `close_all()` closes every stream independently, releases each source once, and is idempotent. A Connection that is not usable (new, connecting, disconnecting, disconnected, failed) makes the next `poll`, `view` or `open_stream` call `close_all()`.

## Readiness gate

Evidence (`ReadinessEvidence`) names provider, Connection, device, camera, availability and host observation time. `evaluate_readiness` is pure; `ReadinessGate` reads evidence from a `ReadinessEvidenceProvider` (read-only protocol) on every check and caches nothing.

Precedence: no evidence → identity mismatch → future-dated → predates the last stream end of this camera → older than `max_age` → UNKNOWN → UNAVAILABLE → ALLOWED. Only fresh AVAILABLE evidence for exactly this provider, Connection, device and camera allows. Age equal to `max_age` is fresh. Default `max_age` 10 s is provisional. Provider errors and wrongly typed evidence are denials. `Connection` readiness is a precondition the manager checks, never a substitute for camera evidence.

## Loss and recovery

`LOST` is never reactivated. Reopening a camera builds a new `PreviewStream` with a new `stream_id`, after a new positive gate decision whose evidence is **strictly newer** than the end of the previous stream (loss or close). Evidence cached before the loss, or taken at the same instant, is refused. Restored connectivity implies nothing about camera readiness.

## Freshness

`view(label)` re-runs the liveness check at read time. `fresh=True` requires: the clock worked, the liveness check ran, the stream is `RECEIVING`, and the Wave 1 classifier says FRESH. Clock failure, liveness failure, backward clock, stalled/lost streams and uncorroborated repeated content all give `fresh=False`. `fresh_pixels(label)` returns pixels only under the same condition. Wave 1 semantics are unchanged.

## Not in Wave 2

Seestar evidence mapping, RTSP/decoder sources, camera start/stop/arm/park (DB-04/05), item-level freshness enforcement for Commands (DB-04), hardware validation.
