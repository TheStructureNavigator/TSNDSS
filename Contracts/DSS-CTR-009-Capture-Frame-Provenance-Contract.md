# TSN Deep Space System

## DSS-CTR-009 --- Capture and Frame Provenance Contract

**Status:** Draft\
**Version:** 0.1\
**Authority:** TSN DSS\
**Scope:** Capture and Frame provenance

------------------------------------------------------------------------

## 1. Purpose

This contract defines the canonical concepts of **Capture** and
**Frame** provenance within TSN Deep Space System.

All contracts, policies, procedures, software components, integrations,
and operational processes that reference Capture or Frame provenance
**SHALL** use the meaning established by this contract.

This contract preserves the distinction between acquisition provenance,
operational history, curated processing membership, and processing
execution.

------------------------------------------------------------------------

## 2. Capture

A **Capture** is an acquisition or import provenance grouping.

A Capture groups data that arrived, was imported, was registered, or was
otherwise accepted together for provenance purposes.

A Capture MAY contain:

- live-acquired data,
- imported data,
- calibration data,
- legacy data,
- synthetic or test data,
- data with known Observation association,
- data without known Observation association.

A Capture belongs to exactly one Project.

Project semantics are defined separately by:

> **DSS-CTR-008 --- Project Contract**

Therefore:

> **Capture != Session**

and:

> **Capture != Observation**

Session semantics are defined by DSS-CTR-001.

Observation semantics are defined by DSS-CTR-003.

------------------------------------------------------------------------

## 3. Frame

A **Frame** is a durable record of one physical or synthetic
observational file or artifact.

A Frame MAY represent:

- a light frame,
- a dark frame,
- a flat frame,
- a bias frame,
- a calibration artifact,
- a device-produced stack,
- a master artifact,
- an imported file,
- a synthetic or test artifact,
- another observational artifact recognized by TSN DSS.

A Frame belongs to exactly one Capture.

Under the current TSN DSS provenance model, a Frame also belongs to
exactly one Project through its Capture and Project provenance.

------------------------------------------------------------------------

## 4. Provenance Purpose

Capture and Frame provenance exists to preserve where data came from,
how it entered TSN DSS, and how it remains traceable.

Provenance **SHALL NOT** be weakened merely to force an operational
relationship.

The absence of a known Session or Observation **SHALL NOT** invalidate a
Capture or Frame.

The existence of a Capture or Frame **SHALL NOT** imply that an
Observation exists.

------------------------------------------------------------------------

## 5. Identity

Every Capture **SHALL** have a Capture identity controlled by TSN DSS.

Every Frame **SHALL** have a Frame identity controlled by TSN DSS.

Capture identity **SHALL NOT** depend solely on:

- a folder name,
- an import label,
- a Session identifier,
- an Observation identifier,
- a provider-specific identifier.

Frame identity **SHALL NOT** depend solely on:

- a file name,
- a path,
- a content hash,
- a Session identifier,
- an Observation identifier,
- a provider-specific identifier.

Names, paths, hashes, and provider identifiers MAY be recorded as
provenance.

------------------------------------------------------------------------

## 6. Ownership

A Project **SHALL** own its Captures.

A Capture **SHALL** own zero or more Frames.

A Frame **SHALL** belong to exactly one Capture.

A Frame **SHALL** belong to exactly one Project under the current TSN
DSS provenance model.

Observation association is not ownership.

Session association is not ownership.

------------------------------------------------------------------------

## 7. Frame to Observation Association

A Frame MAY be associated with zero or one Observation.

The semantic meaning of a Frame-to-Observation association is:

> This Frame was produced by, or is accepted as evidence/output of, this
> Observation.

This association is provenance and evidentiary association, not
ownership.

An Observation MAY have zero Frames.

A visual Observation MAY permanently have zero Frames.

A failed or aborted Observation MAY have zero Frames.

A Frame MAY be linked to an Observation after ingestion when evidence
supports that association.

Observation semantics are defined by:

> **DSS-CTR-003 --- Observation Contract**

This contract does not redefine Observation.

------------------------------------------------------------------------

## 8. Imported, Legacy, Calibration, and Synthetic Data

Imported data MAY be represented as Captures and Frames without known
Session or Observation association.

Legacy data MAY be represented as Captures and Frames without known
Session or Observation association.

Calibration files MAY be represented as Frames without Observation
association.

Synthetic or test artifacts MAY be represented as Frames when clearly
identified according to applicable procedures.

No imported, legacy, calibration, synthetic, or test data SHALL require
fabrication of a Session or Observation merely to preserve provenance.

------------------------------------------------------------------------

## 9. Live Acquisition

Live acquisition MAY produce Captures and Frames.

When live acquisition occurs during an Observation, produced Frames MAY
be associated with that Observation.

When live acquisition occurs during a Session but no canonical
Observation is established, produced Frames remain Capture and Frame
provenance until an Observation association is established.

------------------------------------------------------------------------

## 10. Relationship to AcquisitionPlan

A Frame MAY carry or reference acquisition sequence provenance when the
Frame is produced under an AcquisitionPlan.

AcquisitionPlan semantics are defined separately by:

> **DSS-CTR-010 --- Acquisition Plan Contract**

This contract does not redefine AcquisitionPlan.

------------------------------------------------------------------------

## 11. Relationship to Dataset and Processing

A Dataset MAY associate with Frames.

A ProcessingRun MAY derive from a Dataset that contains Frames.

Dataset and ProcessingRun semantics are defined separately by:

> **DSS-CTR-012 --- Dataset and Processing Contract**

Dataset membership and processing execution **SHALL NOT** replace
Capture and Frame provenance.

------------------------------------------------------------------------

## 12. Boundaries and Non-Goals

This contract defines:

- what a Capture is,
- what a Frame is,
- provenance purpose,
- Capture identity,
- Frame identity,
- Project-to-Capture ownership,
- Capture-to-Frame ownership,
- Frame-to-Observation association semantics,
- imported data compatibility,
- calibration data compatibility,
- legacy data compatibility,
- synthetic and test data compatibility,
- late Observation linking.

This contract does **NOT** define:

- the canonical meaning of Session,
- the canonical meaning of Observation,
- the canonical meaning of Project,
- acquisition recipe semantics,
- Dataset membership rules,
- ProcessingRun execution rules,
- database tables,
- filesystem layout,
- repository patterns,
- user-interface behaviour.

------------------------------------------------------------------------

## 13. Compatibility Principle

Existing files, folders, imports, captures, and frame records MAY be
adapted to this contract when they preserve acquisition or import
provenance.

Compatibility mechanisms SHALL preserve available provenance and SHALL
NOT invent operational history.

------------------------------------------------------------------------

## 14. Contract Authority

Within the TSN DSS operating framework, this document is the
authoritative definition of **Capture** and **Frame** provenance.

Where a dependent contract, policy, procedure, integration, or
implementation conflicts with the Capture or Frame provenance semantics
established here, **DSS-CTR-009 takes precedence for Capture and Frame
provenance semantics** unless superseded by a later approved revision.

------------------------------------------------------------------------

**END OF DSS-CTR-009**
