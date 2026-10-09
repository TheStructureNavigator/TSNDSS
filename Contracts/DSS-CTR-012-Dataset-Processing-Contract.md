# TSN Deep Space System

## DSS-CTR-012 --- Dataset and Processing Contract

**Status:** Draft\
**Version:** 0.1\
**Authority:** TSN DSS\
**Scope:** Curated processing membership and processing execution history

------------------------------------------------------------------------

## 1. Purpose

This contract defines the canonical concepts of **Dataset** and
**ProcessingRun** within TSN Deep Space System.

All contracts, policies, procedures, software components, integrations,
and operational processes that reference a Dataset or ProcessingRun
**SHALL** use the meaning established by this contract.

This contract preserves the distinction between acquisition provenance,
operational history, curated processing membership, and processing
execution.

------------------------------------------------------------------------

## 2. Dataset

A **Dataset** is a curated processing or analysis membership over
Frames and, where useful, Observations.

A Dataset selects data for a processing, analysis, review, or
publication purpose.

A Dataset MAY contain data from multiple Observations.

A Dataset MAY span multiple Sessions.

A Dataset **SHALL NOT** be owned by Session.

Therefore:

> **Dataset != Session**

A Dataset is not a Capture.

Therefore:

> **Dataset != Capture**

Capture and Frame provenance semantics are defined by:

> **DSS-CTR-009 --- Capture and Frame Provenance Contract**

------------------------------------------------------------------------

## 3. ProcessingRun

A **ProcessingRun** is one execution or version of processing over
exactly one Dataset.

A ProcessingRun belongs to processing history.

A ProcessingRun MAY record:

- processing engine,
- processing version,
- parameters,
- status,
- start and completion times,
- output references,
- diagnostic information,
- notes or errors.

A ProcessingRun derives from exactly one Dataset.

Therefore:

> **ProcessingRun != Observation**

and:

> **ProcessingRun != Session**

------------------------------------------------------------------------

## 4. Identity

Every Dataset **SHALL** have a Dataset identity controlled by TSN DSS.

Every ProcessingRun **SHALL** have a ProcessingRun identity controlled
by TSN DSS.

Dataset identity **SHALL NOT** depend solely on:

- a Capture identifier,
- a Session identifier,
- an Observation identifier,
- a folder name,
- a provider-specific identifier.

ProcessingRun identity **SHALL NOT** depend solely on:

- a Dataset name,
- a ProcessingRun output path,
- a provider-specific identifier.

------------------------------------------------------------------------

## 5. Curated Membership

A Dataset **SHALL** preserve explicit curated membership.

A Dataset MAY associate with Frames.

A Dataset MAY associate with Observations.

A Dataset MAY contain Frames from multiple Observations when target
consistency is preserved.

A Dataset MAY contain Frames from multiple Sessions when target
consistency is preserved.

A Dataset MAY contain Frames that have no Observation association when
they are valid for the Dataset's purpose.

Dataset membership **SHALL NOT** replace Capture or Frame provenance.

Dataset membership **SHALL NOT** prove that a Session or Observation
occurred.

------------------------------------------------------------------------

## 6. Target Consistency

A Dataset **SHALL** preserve target-consistency semantics.

When a Dataset declares or derives a Target, associated Observations and
Frames SHOULD be consistent with that Target according to the available
provenance.

When Frame-to-Observation associations exist, Dataset membership SHOULD
preserve consistency between the Dataset Target, Observation Target, and
Frame provenance.

Target semantics are defined separately by:

> **DSS-CTR-004 --- Target Contract**

This contract does not redefine Target.

------------------------------------------------------------------------

## 7. Source Provenance Preservation

Dataset membership references source provenance.

Dataset membership **SHALL NOT** erase, replace, or weaken:

- Project provenance,
- Capture provenance,
- Frame provenance,
- Observation association,
- AcquisitionPlan sequence provenance,
- Session history.

Processing over a Dataset **SHALL NOT** make the Dataset the owner of
the source Frames.

------------------------------------------------------------------------

## 8. ProcessingRun to Dataset Relationship

A ProcessingRun **SHALL** derive from exactly one Dataset.

A Dataset MAY have zero or more ProcessingRuns.

ProcessingRun output MAY be associated with the ProcessingRun.

ProcessingRun output MAY be used by downstream procedures, but the
existence of an output **SHALL NOT** redefine the Dataset, Frames,
Captures, Observations, or Sessions from which it was derived.

------------------------------------------------------------------------

## 9. Processing History

Processing history SHOULD preserve enough information to distinguish
multiple executions over the same Dataset.

Processing history MAY include:

- engine name,
- engine version,
- processing parameters,
- timestamps,
- execution status,
- output references,
- logs or diagnostics,
- operator notes.

Processing history is not operational Session history.

Session Event semantics are defined by:

> **DSS-CTR-006 --- Session Event Contract**

This contract does not redefine SessionEvent.

------------------------------------------------------------------------

## 10. Relationship to Observation

A Dataset MAY associate with Observations.

A Dataset association with an Observation means that the Observation is
relevant to the curated membership or interpretation of the Dataset.

A Dataset association with an Observation **SHALL NOT** make the Dataset
owned by the Observation.

A ProcessingRun **SHALL NOT** mean Observation.

Observation semantics are defined by:

> **DSS-CTR-003 --- Observation Contract**

This contract does not redefine Observation.

------------------------------------------------------------------------

## 11. Relationship to Project

A Project MAY organize Datasets and ProcessingRuns.

Project semantics are defined by:

> **DSS-CTR-008 --- Project Contract**

Project organization of Datasets and ProcessingRuns **SHALL NOT**
replace Dataset membership or ProcessingRun identity.

------------------------------------------------------------------------

## 12. Boundaries and Non-Goals

This contract defines:

- what a Dataset is,
- what a ProcessingRun is,
- Dataset identity,
- ProcessingRun identity,
- curated membership semantics,
- Frame association,
- optional Observation association,
- multi-Observation membership,
- multi-Session membership,
- target consistency,
- source provenance preservation,
- ProcessingRun-to-Dataset relationship,
- processing history.

This contract does **NOT** define:

- the canonical meaning of Session,
- the canonical meaning of Observation,
- the canonical meaning of Capture,
- the canonical meaning of Frame,
- the canonical meaning of Project,
- the canonical meaning of Target,
- acquisition mechanics,
- specific processing engines,
- database tables,
- repository patterns,
- filesystem layouts,
- user-interface behaviour.

------------------------------------------------------------------------

## 13. Compatibility Principle

Existing dataset-like memberships and processing records MAY be adapted
to this contract when they preserve curated membership and processing
history.

Compatibility mechanisms SHALL preserve source provenance and SHALL NOT
invent Sessions, Observations, Captures, or Frames.

------------------------------------------------------------------------

## 14. Contract Authority

Within the TSN DSS operating framework, this document is the
authoritative definition of **Dataset** and **ProcessingRun**.

Where a dependent contract, policy, procedure, integration, or
implementation conflicts with the Dataset or ProcessingRun semantics
established here, **DSS-CTR-012 takes precedence for Dataset and
ProcessingRun semantics** unless superseded by a later approved
revision.

------------------------------------------------------------------------

**END OF DSS-CTR-012**
