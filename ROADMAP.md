# TSN DSS — Contract-Driven Implementation Roadmap

## 1. Authority

DSS-CTR-001 through DSS-CTR-012 are the architectural and domain authority for TSN DSS.

This roadmap does not override the contracts. It defines the ordered implementation path toward conformance.

If this roadmap and a contract disagree, the contract wins.

Contract changes require adjudication before implementation changes.

Implementation agents must report `CONTRACT BLOCKED` when requested work contradicts a contract.

## 2. Current Baseline

Pass 6 established this baseline:

- 380 normative language hits
- 79 consolidated semantic requirement groups
- 24 CONFORMING
- 16 PARTIAL
- 18 MISSING
- 0 CONFLICT
- 5 LEGACY_COMPATIBILITY
- 12 POLICY_ONLY
- 4 NOT_YET_APPLICABLE
- 0 UNDETERMINED

The main architectural finding is that Project, Capture, Frame, AcquisitionPlan, Dataset, and ProcessingRun are mostly implemented.

The primary missing layer is operational Session:

```text
Session
SessionPlan
SessionContext
SessionEvent
SessionMember operational support
```

Observation exists but is not yet canonical because it lacks the required Session relationship.

## 3. Non-Negotiable Guardrails

Future TSN DSS implementation agents must follow these rules:

- Do not change canonical ownership to simplify persistence.
- Do not weaken canonical Observation -> exactly one Session.
- Do not fabricate Sessions for legacy/imported data.
- Do not fabricate Observations for imported Frames.
- Preserve Frame provenance under Project/Capture.
- Frame -> Observation is association, not ownership.
- Project and Session remain separate.
- SessionPlan, AcquisitionPlan, MosaicPlan and PlannedPointing remain distinct.
- Legacy incompatibility is migration/adjudication, not contract failure.
- Contract conflict => `CONTRACT BLOCKED`.
- Provider-specific semantics do not become DSS ownership.
- Mechanically enforced SHALL/SHALL NOT changes require conformance tests.

## 4. Implementation Dependency Spine

The core implementation dependency spine is:

```text
Session persistence
    ->
Session lifecycle repository/service
    ->
Observation.session_id migration
    ->
Observation creation validation
    ->
Project <-> Session association
    ->
SessionPlan / SessionContext / SessionEvent
    ->
operational API/service surface
    ->
full conformance suite
```

Independent or partially independent tracks:

```text
Target reconciliation
Mosaic legacy adjudication
Dataset/Processing conformance
Project/Capture/Frame conformance
```

## 5. Implementation Waves

### Wave 1 — Session Foundation

Goal:
Add canonical Session aggregate and persistence.

Contracts:
DSS-CTR-001 and related authority boundaries.

Expected work:

- Session domain model
- Session identity
- lifecycle
- persistence
- repository/service foundation
- initial Session conformance tests

Status:
COMPLETE

Completion note:
Wave 1 Session Foundation implementation passed its dedicated tests. Stale v5 schema-test expectations were corrected. Remaining local HTTP test failures are environmental WinError 10013; MCP/Pydantic verification stalls are outside Wave 1.

### Wave 2 — Canonical Observation -> Session

Goal:
Make canonical Observation belong to exactly one Session.

Contracts:
DSS-CTR-003, DSS-CTR-001.

Expected work:

- Observation session relationship
- persistence/schema support
- creation validation
- legacy Observation adjudication strategy
- preserve imported Frame provenance without fake Session/Observation
- conformance tests

Risk:
HIGH due legacy Observation compatibility.

Status:
COMPLETE

Completion note:
Wave 2 makes canonical Observation belong to exactly one Session. Schema v6 enforces required Observation -> Session membership, preserves legacy development Observations through the explicit non-historical compatibility Session, and leaves Capture/Frame provenance semantics unchanged. Lifecycle vocabulary was not broadly refactored; `paused` remains an ACTIVE implementation substate. Targeted acceptance verification passed, and stale current-schema migration tests were maintained.

### Wave 3 — Project <-> Session Association

Goal:
Implement explicit association without ownership.

Contracts:
DSS-CTR-001, DSS-CTR-008.

Status:
COMPLETE

Completion note:
Wave 3 delivered canonical optional many-to-many Project <-> Session association through additive schema v7, a pure `project_sessions(project_id, session_id)` relation, and a dedicated ProjectSessionRepository. The relation has no ownership in either direction, performs no migration backfill or fabricated association, and preserves Session ownership of Observations plus Project/Capture ownership of Capture/Frame provenance. Conformance and migration verification passed in the combined closure set: 156 tests passed.

### Wave 4 — SessionPlan

Goal:
Implement operational Session planning.

Contracts:
DSS-CTR-007, DSS-CTR-004, DSS-CTR-010, DSS-CTR-011.

Preserve:
SessionPlan != AcquisitionPlan != MosaicPlan != PlannedPointing.

Status:
READY

### Wave 5 — SessionContext

Goal:
Implement Session contextual association and Observation snapshot boundary.

Contracts:
DSS-CTR-005, DSS-CTR-003.

Status:
BLOCKED BY WAVE 1

### Wave 6 — SessionEvent

Goal:
Implement canonical SessionEvent and define legacy event compatibility.

Contracts:
DSS-CTR-006.

Status:
BLOCKED BY WAVE 1

### Wave 7 — Target Reconciliation

Goal:
Reconcile current catalog-like Target implementation with canonical DSS observing-intent Target.

Contracts:
DSS-CTR-004.

Status:
PLANNED / PARTIALLY INDEPENDENT

### Wave 8 — Mosaic Project Canonicalization

Goal:
Enforce canonical MosaicPlan -> exactly one Project while safely handling legacy unresolved project_slug records.

Contracts:
DSS-CTR-011, DSS-CTR-008.

Status:
PLANNED / LEGACY COMPATIBILITY

### Wave 9 — Contract Conformance Suite

Goal:
Establish explicit mechanical verification of DSS-CTR-001 through DSS-CTR-012.

Contracts:
ALL.

Expected work:

- contract-specific conformance tests
- requirement -> test traceability
- preserve existing behavioral tests
- distinguish behavioral coverage from contract coverage

Status:
ONGOING THROUGH WAVES, FINALIZED AFTER WAVES 1–8

Conformance tests should be added during each implementation wave where practical. Wave 9 is final consolidation, not permission to defer all tests until the end.

## 6. Human Adjudication Queue

These decisions are not permission for implementation agents to decide silently.

- HUMAN ADJUDICATION REQUIRED: existing Observation records without Session
- HUMAN ADJUDICATION REQUIRED: whether Site eventually needs its own authority contract
- HUMAN ADJUDICATION REQUIRED: whether CatalogObject eventually needs its own authority contract
- HUMAN ADJUDICATION REQUIRED: legacy events: migrate/archive/leave legacy
- HUMAN ADJUDICATION REQUIRED: unresolved MosaicPlan project_slug handling
- HUMAN ADJUDICATION REQUIRED: generated/stacked artifact semantics

## 7. Wave Execution Protocol

For every future implementation wave:

1. Identify governing DSS-CTR contracts.
2. Identify requirement IDs from Pass 6.
3. Inspect current implementation.
4. State intended files/areas.
5. State invariants that MUST remain unchanged.
6. Implement only wave scope.
7. Add/update conformance tests.
8. Run relevant existing tests.
9. Run contract conformance tests.
10. Report contract coverage gained.
11. Report legacy compatibility effects.
12. Stop with `CONTRACT BLOCKED` if implementation would require changing canonical semantics.
13. Do not modify contracts during an implementation wave.

## 8. Wave Status Table

| Wave | Objective | Contracts | Dependency | Status | Risk |
|---|---|---|---|---|---|
| 1 | Session Foundation | DSS-CTR-001 | None | COMPLETE | Medium |
| 2 | Canonical Observation -> Session | DSS-CTR-003, DSS-CTR-001 | Wave 1 | COMPLETE | HIGH |
| 3 | Project <-> Session Association | DSS-CTR-001, DSS-CTR-008 | Wave 2 | COMPLETE | Medium |
| 4 | SessionPlan | DSS-CTR-007, DSS-CTR-004, DSS-CTR-010, DSS-CTR-011 | Wave 1 | READY | Medium |
| 5 | SessionContext | DSS-CTR-005, DSS-CTR-003 | Wave 1 | BLOCKED BY WAVE 1 | Medium |
| 6 | SessionEvent | DSS-CTR-006 | Wave 1 | BLOCKED BY WAVE 1 | Medium |
| 7 | Target Reconciliation | DSS-CTR-004 | Partially independent | PLANNED / PARTIALLY INDEPENDENT | Medium |
| 8 | Mosaic Project Canonicalization | DSS-CTR-011, DSS-CTR-008 | Partially independent | PLANNED / LEGACY COMPATIBILITY | Medium |
| 9 | Contract Conformance Suite | DSS-CTR-001 through DSS-CTR-012 | Waves 1–8 for finalization | ONGOING THROUGH WAVES, FINALIZED AFTER WAVES 1–8 | Low |

## 9. Definition of Done

TSN DSS contract-driven implementation is complete when:

- all mechanically enforceable canonical SHALL / SHALL NOT requirements are implemented or explicitly classified as compatibility/policy exceptions,
- canonical Observation requires exactly one Session,
- imported provenance remains valid without fabricated Session/Observation,
- Project/Capture/Frame provenance ownership remains intact,
- operational Session layer exists,
- planning/context/event boundaries conform,
- MosaicPlan Project ownership is canonical,
- Target semantics are reconciled,
- Dataset/Processing semantics remain conforming,
- contract-specific conformance tests protect the architecture,
- every implementation wave can be traced to governing contract requirements.

## 10. Next Action

NEXT: Wave 4 — SessionPlan planning / implementation planning

Next work is planning and implementation planning for operational SessionPlan while preserving SessionPlan != AcquisitionPlan != MosaicPlan != PlannedPointing.

Do not begin Wave 4 implementation now.
