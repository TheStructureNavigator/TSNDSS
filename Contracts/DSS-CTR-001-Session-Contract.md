# TSN Deep Space System

## DSS-CTR-001 --- Session Contract

**Status:** Draft\
**Version:** 0.2\
**Authority:** TSN DSS\
**Scope:** Field and observational sessions

------------------------------------------------------------------------

## 1. Purpose

This contract defines the canonical concept of a **Session** within TSN Deep Space System.

All contracts, policies, procedures, software components, integrations, and operational processes that reference a Session **SHALL** use the meaning established by this contract.

This contract is intentionally independent of specific astronomical hardware, software providers, user interfaces, and field equipment.

------------------------------------------------------------------------

## 2. Session

A **Session** is a bounded operational period during which a **Session Operator** uses TSN DSS capabilities to plan, execute, observe, record, or experience astronomical observations within a defined operational context.

A Session MAY include one or more Session Members.

A Session MAY exist without an astronomical Observation being completed.

A Session MAY exist without any Observation at all.

Therefore:

> **Session != Observation**

An Observation occurs within exactly one Session according to:

> **DSS-CTR-003 --- Observation Contract**

A Session remains valid when an intended Observation cannot be completed because of weather, equipment failure, operational decisions, safety considerations, or other circumstances.

------------------------------------------------------------------------

## 3. Session Identity

Every Session **SHALL** have:

- `session_id`
- `started_at`

A completed or otherwise terminated Session **SHALL** additionally have:

- `ended_at`
- `final_state`

A Session MAY contain or reference:

- `location`
- `title`
- `notes`
- Session Members
- Session Context associations
- Observations
- Session Events
- a Session Plan
- associated Projects

The Session identity belongs to TSN DSS and SHALL NOT depend on the identity model of any individual provider, Project, Capture, Frame, Dataset, ProcessingRun, or external integration.

------------------------------------------------------------------------

## 4. Session Roles

Every active Session **SHALL** have exactly one **Session Operator**.

A Session MAY have zero or more **Session Members**.

The Session Operator holds operational authority for the Session and is responsible for initiating, controlling, suspending, aborting, and closing it.

Participation of Session Members is governed separately by:

> **DSS-CTR-002 --- Session Member Contract**

Additional roles MAY be introduced by dependent contracts without changing the fundamental Operator/Member relationship defined here.

------------------------------------------------------------------------

## 5. Session Lifecycle

The canonical Session lifecycle is:

``` text
PLANNED
   |
   v
PREPARING
   |
   v
ACTIVE
   |
   +------> ABORTED
   |
   v
CLOSING
   |
   v
COMPLETED
```

A Session MAY terminate without completing an Observation.

Session termination SHALL NOT invalidate information already recorded during the Session.

Dependent procedures MAY define additional operational transitions, provided that they do not redefine the canonical meaning of the lifecycle states established by this contract.

------------------------------------------------------------------------

## 6. Session Ownership and Associations

A Session **SHALL** own:

- zero or one active Session Plan,
- zero or more Session Events,
- zero or more Session Context associations,
- zero or more Observations.

A Session MAY reference a Site or observing location.

A Session MAY associate with zero or more Projects.

Project semantics are defined by:

> **DSS-CTR-008 --- Project Contract**

Project-to-Session relationship is association, not ownership.

Therefore:

> **Project SHALL NOT own Session**

and:

> **Session SHALL NOT own Project**

A Session **SHALL NOT** own Capture provenance.

A Session **SHALL NOT** own Frame provenance.

A Session **SHALL NOT** own Datasets or ProcessingRuns.

Capture and Frame provenance semantics are defined by:

> **DSS-CTR-009 --- Capture and Frame Provenance Contract**

Dataset and ProcessingRun semantics are defined by:

> **DSS-CTR-012 --- Dataset and Processing Contract**

------------------------------------------------------------------------

## 7. Observations

A Session MAY contain zero or more Observations.

Every canonical Observation associated with TSN DSS **SHALL** belong to exactly one Session.

The existence of a Session does not require the successful completion of an Observation.

The existence of imported Capture or Frame provenance does not require creation of a Session or Observation.

Observation semantics, Target relationships, AcquisitionPlan references, Frame associations, outputs, and completion criteria are outside the scope of this contract and SHALL be defined by:

> **DSS-CTR-003 --- Observation Contract**

------------------------------------------------------------------------

## 8. Session Plan

A Session MAY have zero or one active Session Plan.

A Session Plan describes operational intent for one Session.

Session Plan semantics are defined by:

> **DSS-CTR-007 --- Session Plan Contract**

A Session Plan SHALL NOT redefine Session, Observation, Target, AcquisitionPlan, MosaicPlan, MosaicPanel, Project, Capture, Frame, Dataset, or ProcessingRun semantics.

------------------------------------------------------------------------

## 9. Operational Context

A Session MAY be associated with contextual information including, but not limited to:

- location,
- weather forecast,
- measured environmental conditions,
- sky conditions,
- equipment state,
- power state,
- lighting,
- music,
- human notes.

Session Context semantics are defined by:

> **DSS-CTR-005 --- Session Context Contract**

Context providers are optional unless explicitly required by another applicable contract or procedure.

Absence or failure of an optional context provider SHALL NOT prevent a Session from existing.

Operational context MAY evolve during a Session and MAY be recorded as state, telemetry, events, snapshots, or derived information according to applicable authority contracts.

------------------------------------------------------------------------

## 10. Events

A Session MAY produce an ordered sequence of Session Events.

Session Event semantics are defined by:

> **DSS-CTR-006 --- Session Event Contract**

Events MAY originate from the Session Operator, Session Members, TSN DSS components, capability providers, sensors, or orchestration systems.

Events SHALL NOT redefine the canonical meaning of Session.

------------------------------------------------------------------------

## 11. Equipment and Capabilities

This contract SHALL NOT require a specific telescope, camera, mount, sensor, computer, vehicle, lighting system, music service, environmental station, sky-quality instrument, or other implementation.

Equipment and external systems participate in a Session through capabilities.

Specific equipment integrations SHALL NOT redefine the canonical meaning of a Session.

Replacement of one hardware or software provider with another SHOULD NOT require modification of this contract when equivalent Session semantics are preserved.

This contract does not assign capability-provider authority to any numbered contract.

------------------------------------------------------------------------

## 12. Safety and Abort Authority

The Session Operator MAY suspend or terminate a Session at any time.

Safety of Session Members SHALL take precedence over:

- Observation completion,
- equipment operation,
- data acquisition,
- Session objectives,
- preservation of planned procedure.

An aborted Session remains a valid Session and SHOULD preserve all information recorded before termination.

Detailed safety rules are outside the scope of this contract and SHALL be governed by applicable TSN DSS safety policies.

------------------------------------------------------------------------

## 13. Contract Boundaries

This contract defines:

- what a Session is,
- minimum Session identity,
- Session lifecycle,
- fundamental Session roles,
- Session ownership of Observations, Session Plan, Session Events, and Session Context associations,
- the relationship between Session and Project,
- the relationship between Session and Observation,
- operational-context boundaries,
- equipment-independence principles,
- extensibility boundaries.

This contract does **NOT** define:

- Project semantics,
- Observation semantics,
- Target semantics,
- Capture or Frame provenance,
- AcquisitionPlan semantics,
- MosaicPlan or MosaicPanel semantics,
- Dataset or ProcessingRun semantics,
- specific astronomical hardware,
- observation acquisition parameters,
- individual provider APIs,
- detailed safety procedures,
- field setup procedures,
- Session Member permissions,
- user-interface behaviour,
- physical field layout,
- implementation details of optional Session context.

------------------------------------------------------------------------

## 14. Dependent Documents

### Contracts

- **DSS-CTR-002 --- Session Member Contract**
- **DSS-CTR-003 --- Observation Contract**
- **DSS-CTR-005 --- Session Context Contract**
- **DSS-CTR-006 --- Session Event Contract**
- **DSS-CTR-007 --- Session Plan Contract**
- **DSS-CTR-008 --- Project Contract**
- **DSS-CTR-009 --- Capture and Frame Provenance Contract**
- **DSS-CTR-012 --- Dataset and Processing Contract**

Dependent documents SHALL NOT redefine the canonical meaning of Session established by DSS-CTR-001.

------------------------------------------------------------------------

## 15. Compatibility Principle

Future versions of TSN DSS SHOULD preserve the semantics defined by this contract independently of changes to hardware, capability providers, orchestration systems, storage mechanisms, or user interfaces.

Imported or legacy data without a known Session MAY be preserved as Project, Capture, Frame, Target metadata, Dataset membership, or other provenance according to the appropriate authority contracts.

Compatibility mechanisms SHALL NOT fabricate Sessions merely to make legacy data conform.

A change that alters the canonical meaning of Session SHOULD require an explicit revision of this contract.

------------------------------------------------------------------------

## 16. Contract Authority

Within the TSN DSS operating framework, this document is the authoritative definition of **Session**.

Where a dependent contract, policy, procedure, integration, or implementation conflicts with the Session semantics established here, **DSS-CTR-001 takes precedence for Session semantics** unless superseded by a later approved revision.

------------------------------------------------------------------------

**END OF DSS-CTR-001**
