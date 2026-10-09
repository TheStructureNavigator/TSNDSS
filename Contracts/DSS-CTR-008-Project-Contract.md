# TSN Deep Space System

## DSS-CTR-008 --- Project Contract

**Status:** Draft\
**Version:** 0.1\
**Authority:** TSN DSS\
**Scope:** Durable DSS campaigns and workspaces

------------------------------------------------------------------------

## 1. Purpose

This contract defines the canonical concept of a **Project** within TSN
Deep Space System.

All contracts, policies, procedures, software components, integrations,
and operational processes that reference a Project **SHALL** use the
meaning established by this contract.

This contract gives authority to the durable workspace and campaign
semantics used to organize astronomical work, provenance, files, plans,
datasets, and processing history.

------------------------------------------------------------------------

## 2. Project

A **Project** is a durable TSN DSS campaign or workspace that organizes
related astronomical work.

A Project MAY organize:

- Capture provenance,
- Frames,
- MosaicPlans,
- Datasets,
- ProcessingRuns,
- target or intent labels,
- references to Targets,
- notes and metadata,
- implementation-specific storage artifacts.

A Project normally has a lifetime longer than a single Session.

A Project MAY span multiple Sessions.

A Project MAY exist before any Session contributes data to it.

A Project MAY receive data after a contributing Session has completed.

Therefore:

> **Project != Session**

Session semantics are defined separately by:

> **DSS-CTR-001 --- Session Contract**

------------------------------------------------------------------------

## 3. Project Identity

Every Project **SHALL** have a Project identity controlled by TSN DSS.

A Project identity **SHALL NOT** depend solely on:

- a display name,
- a filesystem path,
- a directory name,
- a Target name,
- a Session identifier,
- a provider-specific identifier.

A Project MAY have human-readable labels or locators.

Human-readable labels and locators MAY change without changing the
canonical Project identity.

------------------------------------------------------------------------

## 4. Lifetime

A Project is a durable concept.

A Project MAY remain valid across:

- multiple observing nights,
- multiple Sessions,
- multiple Capture imports,
- multiple MosaicPlan revisions,
- multiple Datasets,
- multiple ProcessingRuns,
- changes in equipment,
- changes in storage layout,
- changes in user interface.

Project termination or archival **SHALL NOT** imply that Sessions,
Observations, Captures, Frames, Datasets, or ProcessingRuns are
semantically invalid.

------------------------------------------------------------------------

## 5. Target and Intent

A Project MAY have a primary Target.

A Project MAY have a primary target label, sky target label, campaign
intent, or other human-readable observing intent.

A Project **SHALL NOT** be restricted to exactly one Target.

A Project MAY organize related work involving multiple Targets.

Target semantics are defined separately by:

> **DSS-CTR-004 --- Target Contract**

This contract does not redefine Target.

------------------------------------------------------------------------

## 6. Capture Ownership

A Project **SHALL** own Capture provenance associated with the Project.

A Capture associated with a Project belongs to that Project for
provenance purposes.

Capture and Frame provenance semantics are defined separately by:

> **DSS-CTR-009 --- Capture and Frame Provenance Contract**

This contract does not redefine Capture or Frame.

------------------------------------------------------------------------

## 7. MosaicPlan Ownership

A Project **SHALL** own MosaicPlans associated with the Project.

A MosaicPlan is a Project-level sky coverage planning artifact.

MosaicPlan and MosaicPanel semantics are defined separately by:

> **DSS-CTR-011 --- Mosaic Plan Contract**

This contract does not redefine MosaicPlan or MosaicPanel.

------------------------------------------------------------------------

## 8. Dataset and Processing Relationship

A Project MAY organize Datasets and ProcessingRuns.

A Dataset or ProcessingRun associated with a Project **SHALL NOT**
replace the provenance of the Captures or Frames from which it is
derived.

Dataset and ProcessingRun semantics are defined separately by:

> **DSS-CTR-012 --- Dataset and Processing Contract**

This contract does not redefine Dataset or ProcessingRun.

------------------------------------------------------------------------

## 9. Project and Session Association

A Project MAY be associated with zero or more Sessions.

A Session MAY be associated with zero or more Projects.

A Session MAY contribute data, notes, plans, Observations, or other
operational history to multiple Projects.

Project-to-Session relationship is association, not ownership.

Therefore:

> **Project ASSOCIATES WITH Session**

and:

> **Project SHALL NOT own Session**

A Project MAY contain imported data for which no contributing Session is
known.

A Project MAY receive data after the contributing Session has completed.

A Project MAY exist without any Session association.

------------------------------------------------------------------------

## 10. Multi-Session Campaign Semantics

A Project MAY represent a campaign that accumulates work over many
Sessions.

The same Project MAY organize:

- early planning,
- live-acquired data,
- imported data,
- historical data,
- calibration data,
- synthetic or test data when explicitly identified as such,
- multiple Datasets,
- multiple ProcessingRuns.

The fact that a Project spans multiple Sessions **SHALL NOT** merge
those Sessions into a single Session.

The fact that a Session contributes to a Project **SHALL NOT** make the
Project the owner of the Session.

------------------------------------------------------------------------

## 11. Imported Data

A Project MAY contain imported data whose original Session is unknown.

Imported data **SHALL NOT** require fabrication of a Session merely to
belong to a Project.

Imported data **SHALL** preserve available provenance according to the
Capture and Frame provenance contract.

If an imported Frame is later accepted as evidence or output of an
Observation, that association SHALL be governed by:

> **DSS-CTR-009 --- Capture and Frame Provenance Contract**

and:

> **DSS-CTR-003 --- Observation Contract**

------------------------------------------------------------------------

## 12. Boundaries and Non-Goals

This contract defines:

- what a Project is,
- durable campaign and workspace semantics,
- Project identity principles,
- Project lifetime,
- Project relationship to Targets and intent labels,
- Project ownership of Captures,
- Project ownership of MosaicPlans,
- Project relationship to Datasets and ProcessingRuns,
- Project association with Sessions,
- imported-data compatibility principles.

This contract does **NOT** define:

- the canonical meaning of Session,
- the canonical meaning of Observation,
- the canonical meaning of Target,
- Capture or Frame provenance details,
- MosaicPlan or MosaicPanel details,
- Dataset membership details,
- ProcessingRun execution details,
- database tables,
- repository patterns,
- filesystem layouts,
- user-interface behaviour.

------------------------------------------------------------------------

## 13. Compatibility Principle

Existing Project-like records, folders, labels, or metadata MAY be
adapted to this contract when their meaning is consistent with durable
DSS campaign or workspace semantics.

Compatibility mechanisms SHALL preserve known provenance and SHALL NOT
invent Sessions, Observations, Targets, Captures, Frames, Datasets, or
ProcessingRuns merely to satisfy a Project representation.

------------------------------------------------------------------------

## 14. Contract Authority

Within the TSN DSS operating framework, this document is the
authoritative definition of **Project**.

Where a dependent contract, policy, procedure, integration, or
implementation conflicts with the Project semantics established here,
**DSS-CTR-008 takes precedence for Project semantics** unless superseded
by a later approved revision.

------------------------------------------------------------------------

**END OF DSS-CTR-008**
