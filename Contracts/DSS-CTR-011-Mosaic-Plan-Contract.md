# TSN Deep Space System

## DSS-CTR-011 --- Mosaic Plan Contract

**Status:** Draft\
**Version:** 0.1\
**Authority:** TSN DSS\
**Scope:** Project-level mosaic sky coverage planning

------------------------------------------------------------------------

## 1. Purpose

This contract defines the canonical concepts of **MosaicPlan** and
**MosaicPanel** within TSN Deep Space System.

All contracts, policies, procedures, software components, integrations,
and operational processes that reference a MosaicPlan or MosaicPanel
**SHALL** use the meaning established by this contract.

This contract distinguishes Project-level sky coverage planning from
Session-level operational planning and runtime pointing state.

------------------------------------------------------------------------

## 2. MosaicPlan

A **MosaicPlan** is a Project-level sky coverage planning artifact.

A MosaicPlan describes intended coverage of a region of sky through one
or more planned astronomical footprints.

A MosaicPlan belongs to exactly one Project.

Project semantics are defined by:

> **DSS-CTR-008 --- Project Contract**

A MosaicPlan MAY outlive many Sessions.

Therefore:

> **MosaicPlan != SessionPlan**

SessionPlan semantics are defined separately by:

> **DSS-CTR-007 --- Session Plan Contract**

------------------------------------------------------------------------

## 3. MosaicPanel

A **MosaicPanel** is one planned astronomical footprint or pointing
within a MosaicPlan.

A MosaicPanel belongs to exactly one MosaicPlan.

A MosaicPlan owns zero or more MosaicPanels.

A MosaicPanel MAY have:

- sky position,
- footprint geometry,
- ordering or panel index,
- status,
- target integration intent,
- acquired integration information,
- metadata required to preserve mosaic planning semantics.

A MosaicPanel is a planning artifact. It is not by itself a Session,
Observation, Frame, Dataset, or ProcessingRun.

------------------------------------------------------------------------

## 4. Identity

Every MosaicPlan **SHALL** have a MosaicPlan identity controlled by TSN
DSS.

Every MosaicPanel **SHALL** have a MosaicPanel identity controlled by
TSN DSS or a stable identity within its MosaicPlan.

MosaicPlan identity **SHALL NOT** depend solely on:

- a display name,
- a Session identifier,
- a selected panel,
- a provider-specific identifier.

MosaicPanel identity **SHALL NOT** depend solely on:

- screen position,
- UI cell position,
- runtime telescope position,
- provider-specific identifier.

------------------------------------------------------------------------

## 5. Ownership

A Project **SHALL** own MosaicPlans.

A MosaicPlan **SHALL** own MosaicPanels.

A SessionPlan MAY reference MosaicPanels.

A SessionPlan reference to a MosaicPanel **SHALL NOT** make the
MosaicPanel owned by the SessionPlan or Session.

An Observation MAY be planned or interpreted in relation to a
MosaicPanel when permitted by dependent contracts, but MosaicPanel
ownership remains with MosaicPlan.

------------------------------------------------------------------------

## 6. Sky Coverage and Footprint Semantics

A MosaicPlan describes sky coverage intent.

A MosaicPanel represents a planned footprint within that coverage.

Panel geometry SHOULD be expressed in astronomical terms appropriate to
the planning context.

Panel geometry **SHALL NOT** be reduced to a user-interface cell or
screen coordinate.

MosaicPlan-level intent such as observation type, filter intent, field
coverage, or integration goal MAY guide how MosaicPanels are generated,
selected, interpreted, or completed.

------------------------------------------------------------------------

## 7. Lifetime Across Sessions

A MosaicPlan MAY persist across multiple Sessions.

A MosaicPanel MAY be attempted, partially completed, completed, or
revisited across multiple Sessions.

Partial completion across Sessions **SHALL NOT** merge those Sessions.

Partial completion across Sessions **SHALL NOT** make the MosaicPlan a
SessionPlan.

------------------------------------------------------------------------

## 8. SessionPlan References

A SessionPlan MAY reference one or more MosaicPanels to express which
parts of a Project-level mosaic campaign are intended to be attempted
during a Session.

A SessionPlan MAY reference a MosaicPanel without owning it.

A SessionPlan MAY choose not to reference any MosaicPanel.

SessionPlan references to MosaicPanels are operational planning
references, not MosaicPlan ownership changes.

------------------------------------------------------------------------

## 9. PlannedPointing Boundary

Runtime pointing intent is a distinct concept from MosaicPlan and
MosaicPanel.

A runtime planned pointing MAY be derived from or related to a
MosaicPanel.

A runtime planned pointing **SHALL NOT** redefine MosaicPlan or
MosaicPanel semantics.

This contract does not create a PlannedPointing authority contract.

------------------------------------------------------------------------

## 10. Boundaries and Non-Goals

This contract defines:

- what a MosaicPlan is,
- what a MosaicPanel is,
- MosaicPlan identity,
- MosaicPanel identity,
- Project ownership of MosaicPlans,
- MosaicPlan ownership of MosaicPanels,
- sky coverage and footprint semantics,
- lifetime across Sessions,
- SessionPlan references to MosaicPanels,
- distinction from runtime planned pointing.

This contract does **NOT** define:

- the canonical meaning of Project,
- the canonical meaning of SessionPlan,
- the canonical meaning of Observation,
- the canonical meaning of Frame,
- acquisition mechanics,
- processing or stacking semantics,
- runtime telescope-control APIs,
- database tables,
- repository patterns,
- user-interface behaviour.

------------------------------------------------------------------------

## 11. Compatibility Principle

Existing mosaic planning records MAY be adapted to this contract when
they represent Project-level sky coverage planning.

Compatibility mechanisms SHOULD preserve existing panel identities,
footprints, completion state, and Project association where known.

------------------------------------------------------------------------

## 12. Contract Authority

Within the TSN DSS operating framework, this document is the
authoritative definition of **MosaicPlan** and **MosaicPanel**.

Where a dependent contract, policy, procedure, integration, or
implementation conflicts with the MosaicPlan or MosaicPanel semantics
established here, **DSS-CTR-011 takes precedence for MosaicPlan and
MosaicPanel semantics** unless superseded by a later approved revision.

------------------------------------------------------------------------

**END OF DSS-CTR-011**
