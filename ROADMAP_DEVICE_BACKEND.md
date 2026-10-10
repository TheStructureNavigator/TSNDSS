# TSN DSS Device Backend ROADMAP

## Authority and baseline

This roadmap defines the backend-only implementation path for DSS-CTR-013, the Device & Provider Runtime Contract. It is separate from the historical ROADMAP.md and does not create Wave 10.

Baseline:

- Historical ROADMAP Waves 1-9 are COMPLETE.
- Current schema is v11.
- DSS-CTR-013 is Draft v0.2, independently accepted for backend planning, with requirements DSS-CTR-013-REQ-001 through REQ-067.
- TSNDSS already has TelescopeAdapter, TelescopeAdapterRegistry, SimulatorTelescopeAdapter, SeestarAdapter and TelescopeStateService.
- S30Lab gives experimental evidence for Seestar read-only telemetry, RTSP preview and selected operator-supervised commands, but it is not production readiness.

## Architectural invariants

- Preserve DSS-CTR-001 through DSS-CTR-013 ownership boundaries.
- Keep Device References runtime-only.
- Do not require a database migration.
- Preserve existing TelescopeAdapter compatibility until deliberate migration.
- Do not introduce frontend work, HTTP exposure or MCP exposure.
- Do not introduce persistent command queues or assume global multi-client arbitration.
- Do not treat provider acknowledgement as physical success.
- Do not treat reconnect or process restart as proof of device safety.
- Do not turn preview frames into canonical Frame records.
- Do not implement Acquisition Execution before its contract is accepted.
- Keep vendor-specific Seestar details out of the provider-neutral core.
- Do not claim production readiness from simulator tests or a single hardware experiment.

## Status model

- PLANNED / FIRST ACTIONABLE: ready to implement first.
- PLANNED: future stage with dependencies.
- BLOCKED: cannot begin until the named dependency is accepted.
- COMPLETE: not used. No stage has been formally accepted as complete; every stage keeps its recorded limitations.
- IMPLEMENTED (offline): implemented and verified by deterministic offline tests (Linux, and where stated Windows). No hardware claim. Not formal acceptance.
- HARDWARE_VERIFIED (read-only scope): implemented, verified offline, and validated on real hardware within a stated scope. The scope and the limitations are recorded in an acceptance record. This is not production readiness.
- IMPLEMENTED (offline) + PARTIAL HARDWARE VALIDATION: as IMPLEMENTED (offline), plus hardware evidence that covers only part of the stage's required hardware validation. The covered part and the outstanding acceptance gaps are listed in the stage. Not a hardware-accepted stage.
- Evidence provenance: "operator-reported" means a result reported by the operator that is not recorded in the repository and was not independently reproduced by the roadmap's authors. It is evidence, not formal acceptance.

## Dependency graph

DB-01 -> DB-02 -> DB-03 -> DB-05 -> DB-06A -> DB-06B
DB-01 -> DB-04 -> DB-05
DB-02 -> DB-05

The graph is acyclic. DB-06B remains blocked until DB-06A produces an accepted Acquisition Execution contract.

## DB-01 — Provider Runtime Foundation and Simulator Conformance

**Status:** IMPLEMENTED (offline). Simulator-only stage; no hardware validation is required. Evidence: commit `e2f458a` (provider runtime and deterministic simulator) and `d98d5a4` (coverage and traceability correction), with the DB-01 deterministic tests. Existing TelescopeAdapter compatibility preserved. No separate acceptance record exists; this is not production readiness.

### Objective

Create the provider-neutral runtime foundation required by DSS-CTR-013 using simulator-only validation.

### Scope

Provider identity, runtime Device References, discovery outcomes, Connection identity and lifecycle, Capability Reports, simulated telemetry, simulated preview descriptors and simulator conformance. Command identity and lifecycle types may be defined where needed by the runtime model, but full command execution, deadline orchestration, timeout handling and safety enforcement are deferred to DB-04.

### Contract requirement coverage

Primary: REQ-001 through REQ-017, REQ-030 (partial), REQ-031, REQ-035 through REQ-038, REQ-041 through REQ-043, REQ-055, REQ-056, REQ-057 (partial), REQ-058, REQ-059, REQ-063 (partial).

Boundary: REQ-018, REQ-019, REQ-032, REQ-033, REQ-052, REQ-054, REQ-064, REQ-065, REQ-066, REQ-067.

Type-level preparation only, NOT requirement verification: REQ-020, REQ-039, REQ-040, REQ-044.

REQ-030 and REQ-063 are covered in DB-01 only for Provider, Device, Connection, Capability, Telemetry and Preview. Their Command-outcome portions remain DB-04 responsibilities.

REQ-057 is covered in DB-01 only at the Capability Report model level (support separate from availability, observation time, no proof or freshness claim). Enforcing the obligation against safety-sensitive Commands belongs to DB-04 (REQ-051, REQ-060).

Preparing Command identity and state types in DB-01 does not verify DB-04 command execution, per-Connection exclusivity or safety requirements. REQ-039 and REQ-040 are verified in DB-04, not in DB-01.

### Dependencies

Accepted DSS-CTR-013 v0.2 and the existing telescope adapter scaffold.

### Planned package location

`tsn_dss/engine/device_runtime/`. The package must remain standard-library-only and must not modify `tsn_dss/engine/__init__.py`. It is introduced beside the existing `TelescopeAdapter` runtime in `tsn_dss/engine/telescope.py`, which DB-01 does not change.

### Legacy telescope path

The legacy `TelescopeStateService.slew_to_coordinates` / `slew_to_planned_pointing` path operates on the `TelescopeAdapter` protocol, not on the Provider/Command runtime. It is outside DB-01 and is left unaltered. How it is migrated, wrapped or restricted must be adjudicated during DB-04/DB-05 integration.

### Deliverables

Provider-neutral runtime model, provider registry or registry extension, simulator Provider, runtime Device Reference model, Connection lifecycle model, Capability Report model, simulated Telemetry and Preview evidence descriptors, and tests proving no canonical domain writes.

### Deterministic acceptance tests

Provider registration does not modify canonical domain records. Simulator Provider has stable provider identity and metadata. Discovery covers success, valid empty discovery, nonfatal failure, fatal failure and recovery refresh. Connection lifecycle covers repeated connect/disconnect without duplicate provider calls, reconnect with a new connection_id and failed Connection replacement. Capability Reports distinguish support from current availability. Simulator artifacts are marked simulated.

### Hardware validation requirements

None. DB-01 is simulator-only.

### Explicit non-goals

No Seestar hardware, physical commands, full command executor, timeout orchestration, safety gate engine, frontend/API/MCP exposure, database migration or persistent Device registry.

### Exit criteria

Simulator conformance tests pass deterministically, provider runtime concepts exist without canonical domain writes, and existing TelescopeAdapter compatibility is preserved.

## DB-02 — Seestar Read-Only Integration

**Status:** HARDWARE_VERIFIED (read-only scope). Acceptance record: [docs/DB-02_ACCEPTANCE_RECORD.md](docs/DB-02_ACCEPTANCE_RECORD.md). Scope: one Seestar S30 Pro, firmware 9.31. Not production readiness; limitations L1-L7 apply.

### Objective

Add provider-neutral read-only Seestar integration for discovery, connection state, telemetry and capability reporting.

### Scope

Seestar Provider behind the provider-neutral runtime, Device discovery using read-only evidence, read-only connection state, read-only telemetry mapping, Capability Reports with freshness metadata, and secret redaction.

### Contract requirement coverage

Primary: REQ-001 through REQ-015, REQ-016 (partial: Telemetry only), REQ-017, REQ-035 through REQ-038, REQ-041 through REQ-043, REQ-055, REQ-056, REQ-057 (partial), REQ-058, REQ-059.

Boundary: REQ-018, REQ-019, REQ-032, REQ-033, REQ-052, REQ-054, REQ-064, REQ-065, REQ-066, REQ-067.

Responsibility boundaries between DB-01, DB-02 and DB-04:

- REQ-016: DB-02 verifies the Telemetry half (read-only runtime evidence). Preview reading and streaming belong to DB-03. DB-02 reports Preview availability evidence only and opens no stream.
- REQ-017 and REQ-058: DB-02 maps real device state into provider-reported items. The runtime adds host-observed evidence and the host observation time. Unknown, stale and unavailable states are preserved.
- REQ-055 and REQ-059: discovery, connection reads, Telemetry and capability reads submit no Commands and write no canonical domain records.
- REQ-057 (partial): DB-02 supplies Capability Report freshness evidence taken from a fresh device read. It does not enforce the obligation against safety-sensitive Commands; that is DB-04 (REQ-051, REQ-060).
- REQ-051 is not a DB-02 requirement. It requires each safety-sensitive Command kind to define freshness predicates and is verified in DB-04 and DB-05. It was removed from the DB-02 coverage above.
- REQ-065 is a DB-02 boundary: provider-native target names and coordinates are never mapped into Device References or Telemetry as Target identity.
- Freshness signal: the DB-01 runtime treats a non-raising Provider read as success, so a partially stale telemetry sample still leaves the Connection `ready`. A `ready` Connection therefore does not guarantee that every telemetry item is fresh. Item states (KNOWN, STALE, UNAVAILABLE) are the freshness signal, and item-level freshness enforcement is a DB-04 responsibility (REQ-051, REQ-060): DB-04 freshness predicates must evaluate item states and must never rely on Connection state alone. DB-01 is not modified for this.
- The Seestar authentication handshake is permitted only as a connection prerequisite. It grants no permission for physical control. DB-02 has no Command surface; that is DB-04.

### Dependencies

DB-01 complete, S30Lab read-only evidence (docs/S30Pro_WZRD_research_2026-10-08_UPDATED.md and docs/research/S30Lab/) and a local hardware-validation environment.

### Approved design constraints

- Package location: `tsn_dss/engine/seestar_provider/`, separate from the vendor-neutral `device_runtime`, which DB-02 does not modify.
- The Seestar protocol client is an independent minimal implementation. seestarpy (GPL-3.0) is documented secondary protocol evidence only and none of its source is copied.
- RSA credentials are supplied externally by the operator as a key path. TSNDSS never extracts, embeds, distributes or commits key material.
- Explicitly configured hosts are preferred. UDP discovery is opt-in. There is no fallback IP address.
- TCP sessions are short-lived. Automatic retries apply only to explicitly classified idempotent read operations.
- No generic RPC interface escapes the read-only transport boundary.
- No physical commands, acquisition, preview streaming or telescope movement.

### Deliverables

Seestar read-only adapter, runtime configuration model for host/key path without secret leakage, discovery and telemetry normalization, Capability Reports and hardware validation procedure (operator-run, in docs/DB-02_HARDWARE_VALIDATION.md).

### Deterministic acceptance tests

Read-only discovery returns a runtime Device Reference. Connection state reports provider evidence without becoming persistent Device identity. Telemetry includes host observation time and preserves unknown/stale/unavailable values. Capability Reports include freshness metadata. Static capabilities do not prove command success. Discovery, connection reads, telemetry and capability reads submit no Commands.

### Hardware validation requirements

Required before stage acceptance. Validation is read-only and must not move hardware, start preview, start acquisition, enable heaters or submit state-changing commands.

### Explicit non-goals

No physical movement, state-changing commands, preview runtime, frontend/API/MCP exposure, persistent Device registry or canonical domain writes.

### Exit criteria

Seestar read-only runtime data conforms to provider-neutral models and hardware validation confirms no physical side effects.

### Acceptance record

Accepted as HARDWARE_VERIFIED (read-only scope) on the evidence summarized in [docs/DB-02_ACCEPTANCE_RECORD.md](docs/DB-02_ACCEPTANCE_RECORD.md): offline tests, static read-only audit, and operator-run hardware sessions on a Seestar S30 Pro, firmware 9.31. The acceptance depends on these recorded limitations:

- L1: no independent confirmation that no camera was started (excluded by construction).
- L2: names of unavailable items not recorded; preview-available, stale, valid-empty, fatal-discovery and failed-Connection replacement not exercised on hardware.
- L3: not known whether reads work without the authentication handshake, or whether it ran.
- L4: loss and recovery shown across validator restarts only, not within one session.
- L5: reports carry no timestamps; session order relies on file times and the operator's account.
- L6: one device, firmware 9.31, apparently idle.
- L7: not production readiness.

## DB-03 — Preview Runtime

**Status:** IMPLEMENTED (offline) + PARTIAL HARDWARE VALIDATION. Not hardware-accepted: the required stream loss and recovery and frame freshness validation on real hardware is outstanding. Not production readiness.

### Implementation and validation record

Offline implementation (Linux deterministic tests; Windows offline verification, native and venv, for Waves 4B-2, 4B-3a, 4B-3b and 4B-3c in the tested scenarios):

- Preview semantics, Preview Manager (the only entry for opening streams), Seestar preview integration and OpenCV decoder: `docs/DB-03_PREVIEW_SEMANTICS.md`, `docs/DB-03_PREVIEW_MANAGER.md`, `docs/DB-03_SEESTAR_PREVIEW_INTEGRATION.md`, `docs/DB-03_OPENCV_DECODER.md`.
- Wave 4B process isolation of the decoder (`tsn_dss/engine/opencv_isolated_decoder/`): design `docs/DB-03_WAVE4B_PROCESS_ISOLATION_DESIGN.md`; 4B-1 isolated worker `docs/DB-03_WAVE4B1_ISOLATED_WORKER.md`; 4B-2 shared memory, process limit (D10), deadlines and containment `docs/DB-03_WAVE4B2_SHARED_MEMORY_CONTAINMENT.md`; 4B-3 integration design `docs/DB-03_WAVE4B3_INTEGRATION_DESIGN.md`; 4B-3a worker decoder `docs/DB-03_WAVE4B3A_WORKER_DECODER.md`; 4B-3b parent `ProcessIsolatedDecoder` `docs/DB-03_WAVE4B3B_PARENT_DECODER.md`; 4B-3c composition `build_isolated_preview_manager` (commits `7262f70`, `4a23d4e`).
- Readiness gate timing fix: the gate evaluates the clock after evidence acquisition (commit `23723b8`; regression tests in `tests/test_preview_readiness.py` and `tests/test_preview_manager.py`). Found on hardware as a false `CLOCK_REGRESSION`.
- Wave 5 tooling (merge `f7300cc`): `tools/seestar_preview_validate.py` (RTSP preview validator, commit `3203aa8`, tests `tests/test_seestar_preview_validate.py`) and `tools/seestar_e2e_validate.py` (see the DB-05 section; tests `tests/test_seestar_e2e_validate.py`).

Hardware evidence (Seestar S30 Pro, firmware 9.31), **operator-reported, not independently reproduced, not recorded in the repository**: one end-to-end run of the Wave 5 E2E validator passed (deploy arm, start scenery, MAIN and WIDE preview, stop scenery, park) with 3 decoded frames per camera at 1080x1920 BGR8 and a safe final state. This shows that the isolated preview path decoded real RTSP frames from both cameras once, on one device and firmware. It is a single experiment, not an acceptance record.

### Outstanding acceptance gaps

- G1: stream loss and recovery not validated on hardware (required by this stage).
- G2: frame freshness and stale-frame detection, and the staleness backlog behavior (D7), not measured on hardware (required by this stage).
- G3: D4 and D12 timing values not calibrated against hardware; calibration deferred.
- G4: effect of killing a client mid-session (U5) not validated.
- G5: sporadic `+1` handle count on Windows (`SteadyStateTests`) is an open, unresolved observation; the investigation was closed by operator decision, not by a root cause.
- G6: on Windows venv the worker may be reached through a launcher process (`intermediate_launcher` is a diagnostic token, not proof of containment); the D10 process limit is verified for the tested scenarios only.
- G7: no repository acceptance record for the hardware run (cf. `docs/DB-02_ACCEPTANCE_RECORD.md`); the E2E result above is operator-reported only.
- G8: one device, one firmware; no production readiness.

Closing DB-03 requires evidence for G1, G2, G7 and a decision on the rest. This roadmap does not create a new stage for them.

### Objective

Implement preview runtime evidence, stream lifecycle and frame freshness without creating Capture or Frame records.

### Scope

Preview source descriptors, stream lifecycle, runtime frame metadata, frame freshness, stale-frame detection, stream loss and recovery, simulator preview and Seestar RTSP validation.

### Contract requirement coverage

Primary: REQ-016 through REQ-018, REQ-022, REQ-030, REQ-031, REQ-058, REQ-059, REQ-063.

Boundary: REQ-032, REQ-033, REQ-052, REQ-064, REQ-067.

### Dependencies

DB-01 complete. DB-02 complete for real Seestar preview validation.

### Deliverables

Preview runtime model, Preview source descriptors, simulator preview provider, RTSP preview adapter, freshness/staleness classifier and stream loss/recovery handling.

### Deterministic acceptance tests

Preview reads are runtime evidence only. Preview reads do not submit Commands or alter physical Device state. Preview data does not automatically become Capture, Frame, Dataset, ProcessingRun, Observation or Session state. Stale frames are marked stale. Stream loss is reported without inventing command failure or success.

### Hardware validation requirements

Required for Seestar preview acceptance. Validate RTSP readiness, frame receipt, stale-frame detection, stream loss and recovery. Preview validation does not prove acquisition provenance or production readiness.

### Explicit non-goals

No Capture/Frame creation, Acquisition Execution, frontend implementation, HTTP/MCP exposure or physical movement.

### Exit criteria

Preview runtime is provider-neutral and simulator-testable, and real RTSP behavior is validated as runtime Preview evidence only.

## DB-04 — Safe Command Runtime

**Status:** PLANNED

### Objective

Implement provider-neutral command execution and safety gates using simulator-only validation.

### Scope

Command identity, authorization boundaries, request validation, one active nonterminal state-changing Command per Connection, command lifecycle and outcomes, deadline handling, timed_out versus unknown_result, transport-loss uncertainty, disconnect/cancellation races, device-level uncertainty across reconnect, restart safety requirements, command-kind-specific freshness predicates and deterministic rejection of unsafe operations.

### Contract requirement coverage

Primary: REQ-020 through REQ-029, REQ-039, REQ-040, REQ-044 through REQ-051, REQ-060 through REQ-062.

Boundary: REQ-032, REQ-052, REQ-064, REQ-067.

### Dependencies

DB-01 complete.

### Deliverables

Provider-neutral command executor, command state model, safety gate engine, deadline and transport-loss classifier, in-process unresolved physical uncertainty store, per-connection exclusivity enforcement and simulator command provider.

### Deterministic acceptance tests

Passive reads create no Commands. Invalid requests are rejected before provider submission. Safety-blocked requests never reach provider submission. Conflicting state-changing Commands are rejected or safety-blocked. Deadline before submission produces timed_out. Deadline after possible physical execution produces unknown_result. Transport loss preserves uncertainty when effect is unknown. Ordinary disconnect during active command is rejected unless controlled shutdown/cancellation is defined. Non-idempotent physical Commands are not retried after timeout, transport loss or unknown_result. Unresolved physical uncertainty survives reconnect within the running process. Process restart does not prove safety.

### Hardware validation requirements

None for DB-04 completion. DB-04 is simulator-only and must not issue physical hardware commands.

### Explicit non-goals

No Seestar physical commands, production hardware validation, persistent uncertainty storage, persistent command queue, global arbitration or frontend/API/MCP exposure.

### Exit criteria

Simulator proves command lifecycle, safety gates, uncertainty handling and exclusivity deterministically.

## DB-05 — Seestar Physical Command Integration

**Status:** PLANNED. Not started as a stage. The experimental E2E script described below is evidence toward this stage only; it does not complete DB-05 and does not substitute for DB-04.

### Experimental evidence toward DB-05

`tools/seestar_e2e_validate.py` (commit `32546a1`, merge `f7300cc`) is a standalone, experimental validation tool, separate from the read-only provider. It issues four fixed commands only (`scope_move_to_horizon`, `iscope_start_view` in scenery mode, `iscope_stop_view`, `scope_park`) behind operator opt-in (`--allow-physical-motion`, a TTY and typing `DEPLOY`) and a `PhysicalPermit`, with fail-closed state checks, bounded waits, verified cleanup and an unsafe/unknown final-state report. It does not use the DB-04 Safe Command Runtime (which does not exist yet), has no command ledger, uncertainty tracking or provider-neutral command model, and is not a DB-05 provider integration. The operator-reported hardware run is recorded under DB-03; it is not independently reproduced. DB-05 still requires DB-04, the DB-02 read-only integration and DB-03, and its own operator-gated hardware validation.

### Objective

Integrate selected Seestar physical commands behind explicit operator authorization and DSS-CTR-013 safety gates.

### Scope

Controlled Seestar command bindings, operator authorization, fresh precondition checks, provider acknowledgement separated from verified physical effect, post-command verification, timeout/transport-loss/unknown_result handling and hardware validation including failure and recovery.

### Contract requirement coverage

Primary: REQ-021, REQ-024 through REQ-029, REQ-039, REQ-040, REQ-044 through REQ-051, REQ-060 through REQ-062.

Seestar neutrality: REQ-054.

Boundary: REQ-032, REQ-052, REQ-064, REQ-067.

### Dependencies

DB-02 complete, DB-03 complete where preview or camera state is a precondition, DB-04 complete and operator-supervised hardware validation environment available.

### Deliverables

Seestar command provider for selected commands, command-kind safety policies, operator authorization records, hardware validation procedures and failure/recovery evidence.

### Deterministic acceptance tests

Commands are rejected when required state evidence is stale, unavailable, contradictory or unknown. Movement commands require known non-moving state and clearance/authorization. Preview/scenery commands require compatible state and verified readiness. Park/stow commands require stopped conflicting activity when required. Provider acknowledgement does not equal physical success. Verified effect requires fresh post-command evidence. Timeout or transport loss after possible physical execution produces unknown_result. Unresolved uncertainty blocks later safety-sensitive commands until recovery evidence or authorized operator clearance resolves the unsafe precondition.

### Hardware validation requirements

Mandatory. DB-05 cannot be accepted from simulator tests alone. Initial physical command support remains operator-gated and experimental. A single successful S30Lab experiment is evidence, not production readiness.

### Explicit non-goals

No production-readiness claim, automatic retries after uncertain outcomes, frontend/API/MCP exposure, Acquisition Execution, Capture/Frame creation or persistent Device registry.

### Exit criteria

Selected Seestar physical commands pass controlled hardware validation, including failure and timeout paths, and are documented as controlled backend capability rather than hardware certification.

## DB-06A — Acquisition Execution Contract and Design Gate

**Status:** PLANNED

### Objective

Create and independently accept a separate canonical Acquisition Execution contract before implementing acquisition execution.

### Scope

AcquisitionPlan execution boundary, Observation lifecycle interaction, provider-produced artifact handling, Capture and Frame provenance handoff, SessionContextFact and SessionEvent handoff where applicable, failure recovery, idempotency and duplicate protection.

### Contract requirement coverage

Primary DSS-CTR-013 boundary requirements: REQ-033, REQ-034, REQ-052, REQ-053, REQ-064, REQ-066, REQ-067.

DB-06A does not satisfy Acquisition Execution implementation. It defines the contract gate required before DB-06B.

### Dependencies

DB-01 through DB-05 evidence available for design and DSS-CTR-001 through DSS-CTR-013 ownership boundaries understood.

### Deliverables

Acquisition Execution contract draft, independent acceptance review and bounded implementation plan for DB-06B.

### Deterministic acceptance tests

No implementation tests are required in DB-06A. Acceptance is contract review and traceability.

### Hardware validation requirements

None required for contract authoring. Hardware evidence from DB-05 may inform the design but does not replace contract acceptance.

### Explicit non-goals

No acquisition implementation, Capture/Frame creation, Session/Observation mutation, Dataset/ProcessingRun creation or frontend/API/MCP exposure.

### Exit criteria

Acquisition Execution contract is independently accepted and DB-06B scope is bounded by accepted contract language.

## DB-06B — Acquisition Execution Implementation

**Status:** BLOCKED ON ACCEPTED ACQUISITION EXECUTION CONTRACT

### Objective

Implement the accepted Acquisition Execution contract after DB-06A is accepted.

### Scope

Explicit AcquisitionPlan execution workflow, provider-produced artifact handling, Capture and Frame provenance creation through explicit domain handoff, authorized Observation/SessionContextFact/SessionEvent handoff and idempotent failure recovery.

### Contract requirement coverage

Blocked on DB-06A. Expected DSS-CTR-013 boundary requirements include REQ-033, REQ-034, REQ-052, REQ-053, REQ-064, REQ-066 and REQ-067, plus the future Acquisition Execution contract requirements.

### Dependencies

DB-06A accepted. DB-05 controlled physical command validation complete where hardware acquisition is involved.

### Deliverables

Acquisition executor, provenance handoff implementation, Capture/Frame creation tests and failure recovery/idempotency tests.

### Deterministic acceptance tests

Acquisition Execution creates canonical records only through explicit contract-authorized handoff. Provider-produced files or preview frames do not silently become Capture or Frame records. Session, Observation, Capture, Frame, Dataset and ProcessingRun records are not silently created. Failure recovery is deterministic and idempotent. Duplicate provider artifacts do not create duplicate provenance records.

### Hardware validation requirements

Required for real-device acquisition support. Simulator acceptance alone is insufficient for production-readiness claims.

### Explicit non-goals

No implementation before DB-06A acceptance, silent domain writes, or frontend/API/MCP exposure unless separately planned later.

### Exit criteria

Accepted Acquisition Execution contract is implemented, provenance tests pass and hardware acquisition support is validated separately from simulator behavior.

## DSS-CTR-013 requirement traceability matrix

Every DSS-CTR-013 requirement has at least one planned verification stage. Simulator verification and hardware verification are deliberately separated.

| Requirements | Planned verification stage(s) | Verification type |
|---|---|---|
| REQ-001, REQ-002, REQ-004, REQ-005, REQ-035 | DB-01, DB-02 | Simulator first; hardware read-only for Seestar |
| REQ-003, REQ-032, REQ-052 | DB-01 through DB-06B | Boundary tests; no implicit domain writes |
| REQ-006, REQ-007, REQ-008, REQ-036, REQ-037, REQ-055 | DB-01, DB-02 | Simulator discovery; hardware read-only discovery |
| REQ-009, REQ-010, REQ-011, REQ-012, REQ-038, REQ-041, REQ-042, REQ-043 | DB-01, DB-02 | Simulator lifecycle; hardware read-only connection state |
| REQ-013, REQ-014, REQ-015, REQ-056, REQ-057 | DB-01, DB-02 | Capability model and freshness tests |
| REQ-016, REQ-017, REQ-018, REQ-019, REQ-058, REQ-059 | DB-01, DB-02, DB-03 | Runtime telemetry/preview tests; RTSP hardware validation |
| REQ-020, REQ-021, REQ-022, REQ-023, REQ-024, REQ-025, REQ-026, REQ-027 | DB-04, DB-05 | Simulator command runtime; controlled hardware commands |
| REQ-028, REQ-029, REQ-060, REQ-061, REQ-062 | DB-04, DB-05 | Simulator safety gates; operator-gated hardware validation |
| REQ-030, REQ-031, REQ-063 | DB-01, DB-03, DB-04 | Simulator conformance tests; in DB-01 REQ-030 and REQ-063 cover Provider, Device, Connection, Capability, Telemetry and Preview only, Command outcomes are DB-04 |
| REQ-033, REQ-066 | DB-01, DB-03, DB-06A, DB-06B | Boundary tests: preview and telemetry do not become Capture, Frame or SessionContextFact; future contract gate |
| REQ-034, REQ-064, REQ-067 | DB-03, DB-06A, DB-06B | Preview/acquisition boundary tests; future contract gate |
| REQ-039, REQ-040, REQ-044, REQ-045, REQ-046, REQ-047, REQ-048, REQ-049, REQ-050, REQ-051 | DB-04, DB-05 | Simulator command safety; controlled hardware validation |
| REQ-053 | DB-06A, DB-06B | Future SessionEvent handoff boundary |
| REQ-054 | DB-02, DB-03, DB-05 | Seestar integration stays outside provider-neutral core |
| REQ-065 | DB-01, DB-02, DB-06B | Target identity boundary tests |

Notes:

- Type-level preparation in DB-01 (REQ-020, REQ-039, REQ-040, REQ-044) is not verification of the DB-04/DB-05 rows above.
- REQ-057 is partial in both DB-01 and DB-02 (Capability Report model and fresh-evidence provenance). Enforcement against safety-sensitive Commands, and REQ-051 and REQ-060, are DB-04 and DB-05.
- REQ-016 is split: Telemetry in DB-01 and DB-02, Preview in DB-03 (DB-01 and DB-02 provide descriptors and availability evidence only).
- DSS-CTR-013 normative traceability uses the contract's own requirement IDs (`DSS-CTR-013-REQ-001` through `REQ-067`) and is separate from the legacy SHALL-anchor manifest `tests/contract_traceability.json`, which covers DSS-CTR-001 through DSS-CTR-012 only and does not include DSS-CTR-013.

## Hardware safety gates

Simulator conformance: DB-01 and DB-04 can complete with simulator tests only. Simulator acceptance does not imply physical-device safety.

Read-only hardware integration: DB-02 requires real Seestar read-only validation and must not move hardware or submit commands.

Preview validation: DB-03 requires RTSP validation for real preview support. Preview remains runtime evidence and does not create Capture or Frame records. Current state: one operator-reported E2E run delivered decoded frames from both cameras; loss and recovery and freshness validation are still outstanding, so DB-03 is not hardware-accepted.

Controlled physical execution: DB-05 requires operator-gated real hardware validation. The experimental E2E script (operator-reported run) is not that validation; it bypasses the DB-04 runtime. Provider acknowledgement is not physical success. Timeout or transport loss after possible physical execution creates uncertainty. Uncertainty blocks later safety-sensitive commands until recovery evidence or authorized operator clearance.

Acquisition provenance: DB-06A must produce an accepted Acquisition Execution contract. DB-06B remains blocked until DB-06A is accepted.

Production readiness: no stage may claim production readiness from simulator tests or a single successful experiment. Production readiness requires separate repeated hardware validation, failure-path validation and operator policy review.

## Open decisions

- Whether unresolved physical uncertainty should later persist across process restart.
- How operator clearance should be represented without implying physical proof.
- How credentials and key paths should be configured without leaking secrets.
- Whether provider telemetry should later become SessionContextFact.
- Whether provider command history should later become SessionEvent.
- Whether persistent Device identity is needed.
- Whether MCP/API exposure should be planned after backend maturity.
- Which physical movement safety policies apply to telescope and mount operations.
- What exact Acquisition Execution contract governs Capture, Frame, Observation and provenance handoff.

## Next action

Current state: DB-01 implemented (offline); DB-02 HARDWARE_VERIFIED (read-only scope); DB-03 implemented (offline) with partial hardware validation and open gaps G1-G8; DB-04 and DB-05 PLANNED; DB-06A PLANNED; DB-06B BLOCKED on an accepted Acquisition Execution contract.

NEXT: Implement DB-04 — Safe Command Runtime, as a provider-neutral, simulator-tested stage (it requires DB-01 only and no hardware). It is the next unstarted milestone on the dependency graph and a prerequisite of DB-05.

In parallel and independent of DB-04, DB-03 acceptance remains open: validate stream loss and recovery and frame freshness on hardware (G1, G2) and record the evidence in an acceptance record. No new hardware command beyond the existing operator-gated tools is implied.

Do not begin DB-05 provider integration before DB-04 is complete. Do not begin DB-06B before DB-06A produces an accepted Acquisition Execution contract.
