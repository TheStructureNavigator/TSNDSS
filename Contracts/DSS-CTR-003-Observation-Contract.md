# TSN Deep Space System

## DSS-CTR-003 --- Observation Contract

**Status:** Draft\
**Version:** 0.2\
**Authority:** TSN DSS\
**Scope:** Astronomical Observations within TSN DSS Sessions

------------------------------------------------------------------------

## 1. Purpose

This contract defines the canonical concept of an **Observation** within TSN Deep Space System.

All contracts, policies, procedures, software components, integrations, and operational processes that reference an Observation **SHALL** use the meaning established by this contract.

This contract reconciles Observation semantics with Session authority, Target authority, AcquisitionPlan authority, Capture and Frame provenance authority, and Dataset/Processing authority.

------------------------------------------------------------------------

## 2. Observation

An **Observation** is a bounded attempt, performed within exactly one Session, to observe, measure, image, record, or otherwise attend to exactly one Target.

Therefore:

> **Observation != Session**

Every canonical Observation **SHALL** belong to exactly one Session.

Every canonical Observation **SHALL** reference exactly one Target.

An Observation MAY reference zero or one AcquisitionPlan.

An Observation MAY associate with zero or more Frames.

An Observation MAY permanently produce zero Frames.

Examples of zero-Frame Observations include visual observations, failed observations, aborted observations, and measurements that produce no Frame artifact.

Session semantics are defined by:

> **DSS-CTR-001 --- Session Contract**

Target semantics are defined by:

> **DSS-CTR-004 --- Target Contract**

------------------------------------------------------------------------

## 3. Canonical Observation and Imported Provenance

Imported or legacy astronomical data without a known Session MAY exist as Project, Capture, Frame, Target metadata, Dataset membership, or other provenance without becoming a canonical Observation.

Imported data **SHALL NOT** require creation of an Observation.

Imported data **SHALL NOT** require fabrication of a Session.

Legacy TSN DSS Observation records that predate Session support are compatibility and migration concerns. They SHALL NOT redefine canonical Observation semantics.

Capture and Frame provenance semantics are defined by:

> **DSS-CTR-009 --- Capture and Frame Provenance Contract**

Project semantics are defined by:

> **DSS-CTR-008 --- Project Contract**

------------------------------------------------------------------------

## 4. Observation Identity

Every Observation **SHALL** have:

- `observation_id`
- `session_id`
- `target`
- `started_at`
- `state`

A terminated Observation **SHALL** additionally have:

- `ended_at`
- `final_state`

Observation identity belongs to TSN DSS and SHALL NOT depend solely on a Target name, Capture identifier, Frame identifier, Dataset identifier, ProcessingRun identifier, provider-specific identifier, or file path.

------------------------------------------------------------------------

## 5. Observation Lifecycle

The canonical Observation lifecycle is:

``` text
PLANNED
   |
   v
READY
   |
   v
ACTIVE
   |
   +------> FAILED
   |
   +------> ABORTED
   |
   v
COMPLETED
```

An Observation MAY fail or abort without producing Frames.

A completed Observation means that the intended observing attempt reached its intended operational end. It does not require that any particular processing output exists.

Observation lifecycle states SHALL NOT redefine Session lifecycle states.

------------------------------------------------------------------------

## 6. Target Relationship

Every Observation **SHALL** reference exactly one Target.

A Target may appear in a Session Plan without becoming an Observation.

A Target may appear in Project intent without becoming an Observation.

Target semantics are defined by:

> **DSS-CTR-004 --- Target Contract**

This contract does not redefine Target.

------------------------------------------------------------------------

## 7. AcquisitionPlan Relationship

An Observation MAY reference zero or one AcquisitionPlan.

An Observation reference to an AcquisitionPlan means that the Observation used, intended to use, or was planned according to that acquisition recipe.

An Observation does not own an AcquisitionPlan.

AcquisitionPlan semantics are defined by:

> **DSS-CTR-010 --- Acquisition Plan Contract**

This contract does not redefine AcquisitionPlan.

------------------------------------------------------------------------

## 8. Frame Association

An Observation MAY associate with zero or more Frames.

The semantic meaning of a linked Frame is:

> The Frame was produced by, or is accepted as evidence/output of, the Observation.

Observation-to-Frame association is not ownership.

An Observation **SHALL NOT** own Frame provenance.

Frame provenance remains governed by:

> **DSS-CTR-009 --- Capture and Frame Provenance Contract**

A Frame MAY be linked to an Observation after ingestion when evidence supports that association.

A Frame MAY exist without Observation association.

------------------------------------------------------------------------

## 9. Observation Modes and Outputs

An Observation MAY be visual, photographic, sensor-based, manual, automated, assisted, or otherwise recognized by TSN DSS.

An Observation MAY produce notes, measurements, references, Frames, derived artifacts, or no artifact.

Observation output semantics may include references to artifacts without owning their provenance.

Processing outputs and processing history are governed by:

> **DSS-CTR-012 --- Dataset and Processing Contract**

This contract does not redefine Dataset or ProcessingRun.

------------------------------------------------------------------------

## 10. Observation Context Snapshot

An Observation MAY preserve context relevant at observation time.

Such context MAY include weather, site conditions, equipment state, sky conditions, operator notes, Session Member notes, or other contextual details relevant to interpreting the Observation.

An Observation context snapshot or detail **SHALL NOT** redefine Session Context.

Observation context snapshots SHOULD preserve historically relevant conditions for the Observation.

Session Context semantics are defined by:

> **DSS-CTR-005 --- Session Context Contract**

This contract does not create a separate ObservationContextSnapshot authority.

------------------------------------------------------------------------

## 11. Completion and Failure

An Observation MAY complete successfully without producing a Frame.

An Observation MAY fail or abort before any Frame exists.

A failed or aborted Observation remains a valid historical Observation when it belongs to a Session and references a Target.

Processing success or failure SHALL NOT redefine Observation completion.

------------------------------------------------------------------------

## 12. Contract Boundaries

This contract defines:

- what an Observation is,
- canonical Session membership for Observation,
- Target relationship,
- optional AcquisitionPlan relationship,
- optional Frame association,
- zero-Frame compatibility,
- Observation lifecycle,
- imported-provenance boundary,
- Observation context snapshot boundary,
- Observation output boundaries.

This contract does **NOT** define:

- the canonical meaning of Session,
- the canonical meaning of Target,
- Capture or Frame provenance,
- AcquisitionPlan recipe semantics,
- Dataset or ProcessingRun semantics,
- MosaicPlan or MosaicPanel semantics,
- hardware APIs,
- processing pipelines,
- database tables,
- repository patterns,
- filesystem layout,
- user-interface behaviour.

------------------------------------------------------------------------

## 13. Compatibility Principle

Existing records that were historically called Observations MAY require migration or adjudication before they are canonical Observations under this contract.

Compatibility mechanisms SHALL NOT weaken the rule that every canonical Observation belongs to exactly one Session.

Compatibility mechanisms SHALL NOT fabricate Sessions merely to make legacy or imported data conform.

Imported or legacy data without known Session evidence SHOULD remain provenance until canonical Observation requirements can be satisfied.

------------------------------------------------------------------------

## 14. Contract Authority

Within the TSN DSS operating framework, this document is the authoritative definition of **Observation**.

Where a dependent contract, policy, procedure, integration, or implementation conflicts with the Observation semantics established here, **DSS-CTR-003 takes precedence for Observation semantics** unless superseded by a later approved revision.

------------------------------------------------------------------------

**END OF DSS-CTR-003**
