# TSN Deep Space System

## DSS-CTR-010 --- Acquisition Plan Contract

**Status:** Draft\
**Version:** 0.1\
**Authority:** TSN DSS\
**Scope:** Reusable acquisition recipes

------------------------------------------------------------------------

## 1. Purpose

This contract defines the canonical concept of an **AcquisitionPlan**
within TSN Deep Space System.

All contracts, policies, procedures, software components, integrations,
and operational processes that reference an AcquisitionPlan **SHALL**
use the meaning established by this contract.

This contract distinguishes reusable acquisition mechanics from
Session-level operational intent.

------------------------------------------------------------------------

## 2. AcquisitionPlan

An **AcquisitionPlan** is a reusable target-specific acquisition recipe.

An AcquisitionPlan describes how data acquisition for a Target or
target-like intent should be executed.

An AcquisitionPlan MAY describe:

- exposure duration,
- filters,
- frame counts,
- sequencing,
- frame types,
- gain or offset,
- binning,
- temperature or cooling intent,
- other acquisition parameters recognized by TSN DSS.

An AcquisitionPlan answers:

> How should this target or data acquisition be executed?

A SessionPlan answers:

> What do we intend to do in this Session?

Therefore:

> **AcquisitionPlan != SessionPlan**

SessionPlan semantics are defined separately by:

> **DSS-CTR-007 --- Session Plan Contract**

------------------------------------------------------------------------

## 3. Identity

Every AcquisitionPlan **SHALL** have an AcquisitionPlan identity
controlled by TSN DSS.

AcquisitionPlan identity **SHALL NOT** depend solely on:

- a display name,
- a Session identifier,
- an Observation identifier,
- a provider-specific identifier.

An AcquisitionPlan MAY have a human-readable name.

An AcquisitionPlan MAY have version-like metadata or revision history
when required by dependent procedures.

------------------------------------------------------------------------

## 4. Target Relationship

An AcquisitionPlan SHOULD be associated with a Target or target-specific
intent.

Target semantics are defined separately by:

> **DSS-CTR-004 --- Target Contract**

This contract does not redefine Target.

An AcquisitionPlan **SHALL NOT** replace Target identity.

An AcquisitionPlan **SHALL NOT** by itself prove that an Observation
occurred.

------------------------------------------------------------------------

## 5. Sequence Semantics

An AcquisitionPlan MAY contain one or more acquisition sequences.

An acquisition sequence MAY specify:

- frame type,
- frame count,
- exposure duration,
- filter,
- ordering,
- and other acquisition mechanics.

Sequence identity and sequence ordering SHOULD be stable enough to
preserve Frame provenance when Frames reference acquisition sequences.

Removing, replacing, or retyping a sequence that is already referenced
by Frames SHOULD preserve historical provenance.

------------------------------------------------------------------------

## 6. Reuse

An AcquisitionPlan MAY be reused across multiple Observations.

An AcquisitionPlan MAY be reused across multiple Sessions.

An AcquisitionPlan **SHALL NOT** be owned by Session merely because it
is used during a Session.

An AcquisitionPlan **SHALL NOT** be owned by Observation merely because
an Observation references it.

------------------------------------------------------------------------

## 7. Observation Reference

An Observation MAY reference an AcquisitionPlan.

Observation semantics are defined by:

> **DSS-CTR-003 --- Observation Contract**

This contract does not redefine Observation.

An Observation reference to an AcquisitionPlan means that the
Observation used, intended to use, or was planned according to that
acquisition recipe.

An Observation MAY exist without an AcquisitionPlan when permitted by
the Observation contract.

------------------------------------------------------------------------

## 8. SessionPlan Reference

A SessionPlan MAY reference an AcquisitionPlan to express intended
acquisition mechanics for a planned Session activity.

A SessionPlan reference to an AcquisitionPlan **SHALL NOT** make the
AcquisitionPlan part of the SessionPlan's identity.

A SessionPlan reference to an AcquisitionPlan **SHALL NOT** make the
AcquisitionPlan owned by the Session.

------------------------------------------------------------------------

## 9. Frame Provenance Relationship

A Frame MAY carry acquisition sequence provenance when produced under
an AcquisitionPlan.

Frame provenance semantics are defined by:

> **DSS-CTR-009 --- Capture and Frame Provenance Contract**

This contract does not redefine Frame.

Frame sequence provenance records how acquisition mechanics relate to
the resulting artifact. It does not by itself prove Session membership
or Observation completion.

------------------------------------------------------------------------

## 10. Intent-to-Observe vs Acquisition Mechanics

Intent to observe belongs to Session planning and Observation
semantics.

Acquisition mechanics belong to AcquisitionPlan.

Creating, editing, or storing an AcquisitionPlan **SHALL NOT** by itself
create:

- a Session,
- a SessionPlan,
- an Observation,
- a Capture,
- a Frame,
- a Dataset,
- a ProcessingRun.

------------------------------------------------------------------------

## 11. Boundaries and Non-Goals

This contract defines:

- what an AcquisitionPlan is,
- reusable recipe semantics,
- AcquisitionPlan identity,
- Target relationship,
- acquisition sequence semantics,
- reuse across Observations,
- reuse across Sessions,
- Observation references,
- SessionPlan references,
- Frame provenance relationship,
- the distinction between intent-to-observe and acquisition mechanics.

This contract does **NOT** define:

- the canonical meaning of SessionPlan,
- the canonical meaning of Observation,
- the canonical meaning of Target,
- the canonical meaning of Frame,
- hardware-specific acquisition APIs,
- database tables,
- repository patterns,
- user-interface behaviour.

------------------------------------------------------------------------

## 12. Compatibility Principle

Existing target-specific plans, capture recipes, and sequence records
MAY be adapted to this contract when they describe reusable acquisition
mechanics.

Compatibility mechanisms SHOULD preserve sequence provenance where
Frames already depend on it.

------------------------------------------------------------------------

## 13. Contract Authority

Within the TSN DSS operating framework, this document is the
authoritative definition of **AcquisitionPlan**.

Where a dependent contract, policy, procedure, integration, or
implementation conflicts with the AcquisitionPlan semantics established
here, **DSS-CTR-010 takes precedence for AcquisitionPlan semantics**
unless superseded by a later approved revision.

------------------------------------------------------------------------

**END OF DSS-CTR-010**
