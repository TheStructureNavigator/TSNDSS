# TSN Deep Space System

## DSS-CTR-007 --- Session Plan Contract

**Status:** Draft\
**Version:** 0.2\
**Authority:** TSN DSS\
**Scope:** Operational intent for one TSN DSS Session

------------------------------------------------------------------------

## 1. Purpose

This contract defines the canonical concept of a **Session Plan**.

A Session Plan describes intended Session activity before or during one Session.

All contracts, policies, procedures, software components, integrations, and operational processes that reference a Session Plan **SHALL** use the meaning established by this contract.

------------------------------------------------------------------------

## 2. Session Plan

A **Session Plan** is an ordered or unordered representation of intended activity for one Session.

A Session Plan belongs to exactly one Session.

A Session Plan answers:

> What do we intend to do in this Session?

Therefore:

> **SessionPlan != Session**

and:

> **SessionPlan != AcquisitionPlan**

and:

> **SessionPlan != MosaicPlan**

A planned item is not an executed Observation.

Session semantics are defined by:

> **DSS-CTR-001 --- Session Contract**

------------------------------------------------------------------------

## 3. Identity

A Session MAY have zero or one active Session Plan.

A Session Plan SHOULD have:

- `plan_id`
- `session_id`
- creation or revision metadata
- plan state

A Session Plan identity SHALL NOT depend solely on Target identity, AcquisitionPlan identity, MosaicPanel identity, Project identity, or runtime pointing state.

------------------------------------------------------------------------

## 4. Plan Contents

A Session Plan MAY reference:

- Targets,
- AcquisitionPlans,
- MosaicPanels,
- Project associations or hints where operationally useful,
- planned Observation intents,
- ordering or priority,
- timing constraints,
- environmental constraints,
- fallback activity,
- operator notes.

A Session Plan MAY change before or during a Session.

A Session Plan change SHALL NOT by itself prove that an Observation occurred.

------------------------------------------------------------------------

## 5. Target References

A Session Plan MAY reference one or more Targets.

A Target reference expresses intended observing subject, field, region, or target-like intent for the Session.

A Session Plan SHALL NOT own Target.

Target semantics are defined by:

> **DSS-CTR-004 --- Target Contract**

------------------------------------------------------------------------

## 6. AcquisitionPlan References

A Session Plan MAY reference one or more AcquisitionPlans.

An AcquisitionPlan reference expresses intended acquisition mechanics for planned Session activity.

A Session Plan SHALL NOT own AcquisitionPlan.

AcquisitionPlan semantics are defined by:

> **DSS-CTR-010 --- Acquisition Plan Contract**

AcquisitionPlan answers how target/data acquisition should be executed.

Session Plan answers what is intended for this Session.

------------------------------------------------------------------------

## 7. MosaicPanel References

A Session Plan MAY reference one or more MosaicPanels.

A MosaicPanel reference expresses which part of a Project-level mosaic campaign is intended to be attempted during the Session.

A Session Plan SHALL NOT own MosaicPlan.

A Session Plan SHALL NOT own MosaicPanel.

MosaicPlan and MosaicPanel semantics are defined by:

> **DSS-CTR-011 --- Mosaic Plan Contract**

MosaicPlan is campaign or Project sky-coverage intent.

Session Plan is Session-specific operational intent.

------------------------------------------------------------------------

## 8. Project References

A Session Plan MAY include Project associations or hints when operationally useful.

A Session Plan SHALL NOT own Project.

A Session Plan reference to Project SHALL NOT make Project the owner of the Session.

Project semantics are defined by:

> **DSS-CTR-008 --- Project Contract**

------------------------------------------------------------------------

## 9. PlannedPointing Boundary

PlannedPointing is short-lived runtime pointing intent.

A PlannedPointing MAY be derived from a Session Plan item, MosaicPanel, manual choice, or another operational source.

A Session Plan SHALL NOT mean PlannedPointing.

This contract does not create a PlannedPointing authority contract.

------------------------------------------------------------------------

## 10. Observation Boundary

A planned item is not an executed Observation.

An Observation MAY be created, started, completed, failed, or aborted in relation to a Session Plan item, but Observation semantics are defined by:

> **DSS-CTR-003 --- Observation Contract**

A Session Plan SHALL NOT redefine Observation lifecycle, Observation Target rules, Observation AcquisitionPlan references, or Observation Frame associations.

------------------------------------------------------------------------

## 11. Operator Authority and WZRD

The Session Operator retains authority over Session execution and may accept, reject, modify, or ignore a Session Plan according to Session authority.

WZRD MAY propose, optimize, rank, or modify a Session Plan when permitted by TSN DSS procedures or capabilities.

WZRD SHALL NOT redefine the canonical meaning of Session Plan.

WZRD SHALL NOT become the owner of Target, AcquisitionPlan, MosaicPlan, MosaicPanel, Project, Observation, Capture, Frame, Dataset, or ProcessingRun merely by proposing plan content.

------------------------------------------------------------------------

## 12. Contract Boundaries

This contract defines what a Session Plan is, Session Plan identity, Session-specific operational intent, Target references, AcquisitionPlan references, MosaicPanel references, Project hints, planned item boundaries, PlannedPointing boundary, Operator authority, and WZRD recommendation boundaries.

This contract does **NOT** define Session, Observation, Target, AcquisitionPlan, MosaicPlan, MosaicPanel, Project, Capture, Frame, Dataset, ProcessingRun, PlannedPointing as a standalone authority, scheduling algorithms, optimization algorithms, provider APIs, database tables, repository patterns, or user-interface behaviour.

------------------------------------------------------------------------

## 13. Compatibility Principle

Existing plan-like records MAY be adapted to this contract when they represent intended activity for one Session.

Compatibility mechanisms SHALL NOT collapse SessionPlan, AcquisitionPlan, MosaicPlan, or PlannedPointing into one concept.

Compatibility mechanisms SHALL NOT treat planned items as executed Observations.

------------------------------------------------------------------------

## 14. Contract Authority

Within the TSN DSS operating framework, this document is the authoritative definition of **Session Plan**.

Where a dependent contract, policy, procedure, integration, or implementation conflicts with the Session Plan semantics established here, **DSS-CTR-007 takes precedence for Session Plan semantics** unless superseded by a later approved revision.

------------------------------------------------------------------------

**END OF DSS-CTR-007**
