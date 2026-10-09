# TSN Deep Space System

## DSS-CTR-002 --- Session Member Contract

**Status:** Draft\
**Version:** 0.2\
**Authority:** TSN DSS\
**Parent Contract:** DSS-CTR-001 --- Session Contract\
**Scope:** Human participation in TSN DSS Sessions

------------------------------------------------------------------------

## 1. Purpose

This contract defines the canonical role, rights, responsibilities, and operational boundaries of a **Session Member** participating in a TSN Deep Space System Session.

This contract depends on and SHALL be interpreted according to:

> **DSS-CTR-001 --- Session Contract**

Terms defined by DSS-CTR-001 retain their canonical meaning here.

This contract does not redefine Session, Observation, Project, Capture, Frame, Dataset, ProcessingRun, or Session Operator authority.

------------------------------------------------------------------------

## 2. Session Member

A **Session Member** is a human participant admitted to a Session by the Session Operator.

A Session Member participates in the Session without assuming the operational authority of the Session Operator.

A Session MAY contain zero or more Session Members.

Session Member participation is scoped to the Session in which the Member is admitted.

Participation in one Session does not automatically grant participation rights in another Session.

Session Member participation begins when the Session Operator admits the participant to the Session and ends when:

- the Session is completed or aborted,
- the Session Operator ends the participant's participation,
- or the Session Member voluntarily leaves the Session.

------------------------------------------------------------------------

## 3. Session Operator Authority

The **Session Operator** retains final operational authority throughout the Session.

The Session Operator MAY:

- assign or revoke Session Member capabilities,
- restrict access to equipment,
- restrict actions that may affect an Observation,
- change the Session Plan,
- suspend Session activities,
- initiate safety procedures,
- remove a Session Member from operational participation,
- abort or close the Session.

Session Members SHALL respect operational instructions issued by the Session Operator when those instructions relate to safety, equipment, Observation integrity, or Session execution.

The Session Member Contract does not transfer ownership or control of TSN DSS to a Session Member.

------------------------------------------------------------------------

## 4. Membership and Domain Ownership

Session membership **SHALL NOT** imply ownership of any Project.

Session membership **SHALL NOT** imply ownership of Captures, Frames, Datasets, ProcessingRuns, MosaicPlans, MosaicPanels, AcquisitionPlans, or other provenance or planning artifacts.

Project semantics are defined by:

> **DSS-CTR-008 --- Project Contract**

Capture and Frame provenance semantics are defined by:

> **DSS-CTR-009 --- Capture and Frame Provenance Contract**

Dataset and ProcessingRun semantics are defined by:

> **DSS-CTR-012 --- Dataset and Processing Contract**

A Session Member MAY be allowed to request, view, annotate, or help operate domain concepts when granted appropriate capabilities, but such capabilities are operational permissions within the Session and not ownership transfer.

------------------------------------------------------------------------

## 5. Member Rights

A Session Member MAY:

- observe astronomical targets presented during the Session,
- receive information about current targets and Session conditions,
- participate in target selection when permitted,
- contribute notes or observations,
- request music, lighting, or other optional Session-context changes,
- use shared field infrastructure when permitted,
- ask the Session Operator or supporting systems for Session information,
- voluntarily end their participation at any time.

A Session Member SHOULD be able to participate meaningfully without understanding the technical implementation of TSN DSS.

No Session Member SHALL be required to understand provider architecture, telemetry schemas, MCP integrations, or other implementation details merely to look at the sky.

------------------------------------------------------------------------

## 6. Member Capabilities

Session Member permissions SHOULD be capability-based.

A Session Member SHALL NOT automatically receive control over all Session systems.

Example capabilities MAY include:

- `target.vote`
- `target.request`
- `music.request`
- `music.control`
- `lighting.request`
- `lighting.control`
- `session.note`
- `observation.view`
- `equipment.control`

Capabilities MAY be granted, limited, or revoked by the Session Operator.

The absence of a capability SHALL NOT prevent ordinary participation unless that capability is required for a specific action.

A Session Member without `equipment.control` SHALL NOT operate astronomical equipment merely because the equipment is physically accessible.

------------------------------------------------------------------------

## 7. Equipment Interaction

Session Members SHALL treat astronomical instruments, computers, sensors, power systems, and other Session equipment as operational equipment.

Unless explicitly authorized, Session Members SHALL NOT:

- move or reposition an active Observation instrument,
- disconnect power or communication links,
- modify acquisition settings,
- interrupt an active Observation,
- alter Session software configuration,
- handle equipment in a way that may compromise safety or data acquisition.

Physical proximity does not imply operational permission.

The Session Operator MAY explicitly authorize equipment interaction when appropriate.

------------------------------------------------------------------------

## 8. Dark Adaptation and Lighting

When a Session enters a dark-adapted operational state, Session Members SHOULD preserve dark adaptation.

Session Members SHOULD avoid unnecessary bright white light, including:

- phone flashlights,
- vehicle headlights,
- high-brightness displays,
- portable lamps,
- camera flashes.

Lighting MAY be controlled by the Session Operator or an authorized Session capability.

Unintentional light exposure SHALL be treated as an operational inconvenience, not as grounds for summary execution of the Session Member.

Repeated unauthorized white-light usage MAY result in revocation of `lighting.control`.

------------------------------------------------------------------------

## 9. Shared Field Infrastructure

Session Members MAY use shared field infrastructure made available by the Session Operator.

Shared infrastructure MAY include seating, blankets, tables, beverage areas, charging facilities, lighting, shelter, and other field equipment.

Beverages SHOULD remain outside any designated Laptop Safety Perimeter.

Session Members SHALL take reasonable care not to contaminate, damage, obstruct, or destabilize operational equipment.

The Field Table MAY be used for beverages, personal equipment, and approved Session preparation activities.

Detailed implementation of Session preparation activities is intentionally outside the scope of this contract.

------------------------------------------------------------------------

## 10. Session Context

Music, lighting, ambience, conversation, and other experiential elements MAY form part of Session Context.

Session Context semantics are defined by:

> **DSS-CTR-005 --- Session Context Contract**

Session Members MAY request changes to optional Session Context.

Such requests are non-binding unless the Session Member has been granted the corresponding control capability.

The Session Operator retains final authority over Session Context where it affects safety, dark adaptation, equipment operation, Observation integrity, or other Session Members.

A rejected Spotify request does not constitute a Session Incident.

------------------------------------------------------------------------

## 11. Junior Session Members

A Session Member MAY be designated as a **Junior Session Member**.

Junior Session Members SHOULD receive capabilities appropriate to their age, understanding, and the operational environment.

Junior Session Members SHALL NOT receive unrestricted equipment-control capabilities by default.

The Session Operator retains responsibility for defining appropriate participation boundaries.

Junior participation SHOULD prioritize safety, curiosity, target discovery, learning, meaningful involvement, and enjoyment of the Session.

A Junior Session Member MAY be granted target-selection or voting capabilities without receiving hardware-control capabilities.

------------------------------------------------------------------------

## 12. Safety Responsibilities

Every Session Member SHALL prioritize personal and group safety over Session objectives.

A Session Member SHOULD promptly report observed hazards including unexpected weather changes, rain, excessive wind, unstable equipment, unsafe terrain, suspicious environmental activity, wildlife, fire, electrical hazards, health or comfort concerns, or any condition reasonably believed to threaten people or equipment.

The statement:

> "chyba zaraz będzie padać"

SHALL be considered valid environmental input and MAY justify immediate reassessment by the Session Operator.

Detailed safety rules are governed separately by applicable TSN DSS safety policies.

------------------------------------------------------------------------

## 13. Emergency Behaviour

When an emergency, credible hazard, or Session abort condition is declared, Session Members SHALL prioritize movement to the designated safe location and follow reasonable safety instructions from the Session Operator.

Equipment recovery is secondary to human safety.

No Session Member is required to remain in an unsafe environment to protect, recover, shut down, or transport equipment.

Where a vehicle is designated as the Session safe fallback location, Session Members SHOULD proceed to that location when instructed.

------------------------------------------------------------------------

## 14. Observation Integrity

Session Members SHOULD avoid actions likely to interfere with an active Observation.

Potential interference includes physical contact with an instrument, vibration near sensitive equipment, obstruction of the optical path, unauthorized lighting, power interruption, network disruption, or configuration changes.

Accidental interference SHOULD be recorded when operationally relevant.

Session Members are participants, not sources of blame.

Observation semantics are defined by:

> **DSS-CTR-003 --- Observation Contract**

------------------------------------------------------------------------

## 15. Human Experience Principle

TSN DSS exists to support astronomical observation and the human experience surrounding it.

Operational structure SHOULD enhance the Session rather than make participation burdensome.

A Session Member SHOULD NOT need to behave like a trained technician in order to participate.

Contracts, procedures, automation, and technology SHOULD remain subordinate to safety, curiosity, shared experience, astronomical observation, and the simple act of looking at the sky.

------------------------------------------------------------------------

## 16. Contract Boundaries

This contract defines Session Member participation, Operator relationship, basic Member rights, capability-based permissions, equipment boundaries, dark-adaptation expectations, shared-infrastructure expectations, safety responsibilities, Junior Session Member principles, and the boundary between Session participation and ownership of Project, provenance, planning, dataset, or processing concepts.

This contract does **NOT** define Session, Observation, Project, Capture, Frame, Dataset, ProcessingRun, specific hardware APIs, user-account architecture, authentication architecture, emergency procedures, field setup, specific capability implementations, or legal responsibility outside the TSN DSS operating framework.

------------------------------------------------------------------------

## 17. Acceptance

Participation in a Session MAY include explicit acknowledgement of this contract.

A practical acknowledgement MAY take any form accepted by the Session Operator.

Participation in a Session after being informed of applicable Session Member expectations MAY constitute practical acknowledgement for TSN DSS operational purposes.

------------------------------------------------------------------------

## 18. Contract Authority

Within the TSN DSS operating framework, this document is the authoritative definition of **Session Member**.

Where a dependent contract, policy, procedure, integration, or implementation conflicts with the Session Member semantics established here, **DSS-CTR-002 takes precedence for Session Member semantics** unless superseded by a later approved revision.

------------------------------------------------------------------------

**END OF DSS-CTR-002**
