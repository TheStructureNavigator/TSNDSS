# TSN Deep Space System

## DSS-CTR-004 --- Target Contract

**Status:** Draft\
**Version:** 0.2\
**Authority:** TSN DSS\
**Scope:** DSS observing intent and astronomical references

------------------------------------------------------------------------

## 1. Purpose

This contract defines the canonical concept of a **Target** within TSN Deep Space System.

All contracts, policies, procedures, software components, integrations, and operational processes that reference a Target **SHALL** use the meaning established by this contract.

------------------------------------------------------------------------

## 2. Target

A **Target** is a DSS observing intent or astronomical reference toward which planning, observing, acquisition, or interpretation may be directed.

A Target MAY represent:

- a catalog-backed object,
- fixed coordinates,
- a region or field,
- a mosaic field,
- a moving or dynamic target,
- a user-defined target,
- a historical imported target reference where meaningful.

Therefore:

> **Target != Observation**

A Target may be referenced by many Observations, Session Plans, Projects, AcquisitionPlans, Datasets, or other DSS concepts.

The existence of a Target does not prove that an Observation occurred.

------------------------------------------------------------------------

## 3. Target and CatalogObject

A Target is not the same concept as a catalog object.

Therefore:

> **Target != CatalogObject**

A catalog object is external or catalog astronomical knowledge.

A Target is the DSS observing intent or reference used by TSN DSS.

A Target MAY be backed by catalog data, resolved from catalog data, or associated with catalog metadata, but catalog metadata SHALL NOT replace the canonical Target identity.

This contract does not create a CatalogObject authority contract.

------------------------------------------------------------------------

## 4. Target Identity

Every Target **SHALL** have a Target identity controlled by TSN DSS.

Target identity SHALL NOT depend solely on:

- a display name,
- a catalog name,
- a provider identifier,
- a Project label,
- a Session identifier,
- an Observation identifier.

A Target MAY have aliases, catalog references, coordinates, type classification, or other metadata.

A Target name is not the sole identity of the Target.

------------------------------------------------------------------------

## 5. Target Classification

A Target SHOULD preserve canonical classification semantics equivalent to `target_type`.

Target classification MAY distinguish catalog-backed objects, fixed coordinates, regions, fields, mosaic fields, moving targets, user-defined targets, and historical imported references.

A Target classification SHALL NOT by itself prove that an Observation occurred.

------------------------------------------------------------------------

## 6. Relationship to Observation

Every canonical Observation **SHALL** reference exactly one Target.

Observation semantics are defined by:

> **DSS-CTR-003 --- Observation Contract**

This contract does not redefine Observation.

------------------------------------------------------------------------

## 7. Relationship to Session Plan

A Session Plan MAY reference one or more Targets to express intended Session activity.

A Target MAY appear in a Session Plan without becoming the subject of an executed Observation.

Session Plan semantics are defined by:

> **DSS-CTR-007 --- Session Plan Contract**

This contract does not redefine Session Plan.

------------------------------------------------------------------------

## 8. Relationship to Project

A Project MAY reference a primary Target, primary target label, or observing intent.

A Project SHALL NOT be constrained to exactly one Target merely because it has a primary Target or primary intent.

Project semantics are defined by:

> **DSS-CTR-008 --- Project Contract**

This contract does not redefine Project.

------------------------------------------------------------------------

## 9. Relationship to AcquisitionPlan and Dataset

An AcquisitionPlan SHOULD be associated with a Target or target-specific intent according to:

> **DSS-CTR-010 --- Acquisition Plan Contract**

A Dataset SHALL preserve target-consistency semantics according to:

> **DSS-CTR-012 --- Dataset and Processing Contract**

This contract does not redefine AcquisitionPlan, Dataset, or ProcessingRun.

------------------------------------------------------------------------

## 10. Dynamic Targets

A Target MAY represent a moving or dynamic astronomical subject.

For dynamic Targets, coordinates or ephemeris-derived positions MAY vary over time.

The identity of the Target SHALL remain distinct from any single coordinate sample.

------------------------------------------------------------------------

## 11. Contract Boundaries

This contract defines Target identity, Target classification, Target relationship to Observations, Target relationship to Session Plans, Target relationship to Projects, and the distinction between Target and CatalogObject.

This contract does **NOT** define Observation execution, Session planning mechanics, Project ownership, AcquisitionPlan sequence mechanics, Dataset processing membership, catalog provider APIs, database tables, repository patterns, or user-interface behaviour.

------------------------------------------------------------------------

## 12. Compatibility Principle

Existing target-like labels, catalog names, coordinates, or imported references MAY be adapted to Target semantics when they represent DSS observing intent or reference.

Compatibility mechanisms SHALL NOT collapse Target identity into a catalog object, Project label, Session, or Observation.

------------------------------------------------------------------------

## 13. Contract Authority

Within the TSN DSS operating framework, this document is the authoritative definition of **Target**.

Where a dependent contract, policy, procedure, integration, or implementation conflicts with the Target semantics established here, **DSS-CTR-004 takes precedence for Target semantics** unless superseded by a later approved revision.

------------------------------------------------------------------------

**END OF DSS-CTR-004**
