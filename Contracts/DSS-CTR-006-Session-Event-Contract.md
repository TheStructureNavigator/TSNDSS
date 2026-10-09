# TSN Deep Space System

## DSS-CTR-006 --- Session Event Contract

**Status:** Draft\
**Version:** 0.2\
**Authority:** TSN DSS\
**Scope:** Historical events within TSN DSS Sessions

------------------------------------------------------------------------

## 1. Purpose

This contract defines the canonical concept of a **Session Event** within TSN Deep Space System.

All contracts, policies, procedures, software components, integrations, and operational processes that reference a Session Event **SHALL** use the meaning established by this contract.

Session Events preserve historical facts about a Session without requiring TSN DSS to use event sourcing internally.

------------------------------------------------------------------------

## 2. Session Event

A **Session Event** is an immutable historical fact associated with exactly one Session.

Every Session Event **SHALL** belong to exactly one Session.

Session semantics are defined by:

> **DSS-CTR-001 --- Session Contract**

A Session Event MAY reference a related Observation.

A Session Event MAY reference a related Session Member.

A Session Event is not the current state of the Session. It is a historical record.

------------------------------------------------------------------------

## 3. Event Identity

Every Session Event **SHALL** have:

- `event_id`
- `session_id`
- `event_type`
- `occurred_at`

A Session Event MAY additionally have:

- source or provenance,
- payload,
- related Observation,
- related Session Member,
- notes.

Session Event identity SHALL NOT depend solely on provider-specific identifiers, Observation identifiers, Frame identifiers, Dataset identifiers, or ProcessingRun identifiers.

------------------------------------------------------------------------

## 4. Historical Fact Semantics

A Session Event records that something relevant happened or was declared during or about a Session.

A Session Event SHOULD preserve enough information to understand its meaning, timing, and source when available.

A Session Event SHALL NOT redefine the canonical current state of Session, Observation, Project, Capture, Frame, Dataset, or ProcessingRun.

Historical events and current state are distinct concepts.

------------------------------------------------------------------------

## 5. Ordering and Time

A Session Event **SHALL** have `occurred_at`.

Session Events MAY be ordered by occurrence time.

If two Session Events have the same occurrence time, dependent procedures MAY define additional ordering rules without redefining Session Event semantics.

------------------------------------------------------------------------

## 6. Relationship to Observation

Observation-specific facts MAY be represented as Session Events with a related Observation reference when appropriate.

This contract does not create a separate ObservationEvent authority.

Observation semantics are defined by:

> **DSS-CTR-003 --- Observation Contract**

A Session Event reference to an Observation SHALL NOT make the Session Event the owner of the Observation.

------------------------------------------------------------------------

## 7. Relationship to Session Member

Session Member-specific facts MAY be represented as Session Events with a related Session Member reference when appropriate.

Session Member semantics are defined by:

> **DSS-CTR-002 --- Session Member Contract**

A Session Event reference to a Session Member SHALL NOT make the Session Event the owner of the Session Member.

------------------------------------------------------------------------

## 8. Event Types

Session Event types MAY include, but are not limited to:

- Session created,
- Session started,
- Session suspended,
- Session resumed,
- Session completed,
- Session aborted,
- Session Member admitted,
- Session Member removed,
- Observation planned,
- Observation started,
- Observation completed,
- Observation failed,
- Observation aborted,
- context changed,
- equipment state changed,
- safety condition declared,
- operator note recorded.

Dependent procedures MAY introduce additional event types without redefining Session Event.

------------------------------------------------------------------------

## 9. Event Sourcing Boundary

TSN DSS MAY use Session Events as an audit trail.

TSN DSS MAY use Session Events as part of state reconstruction when a dependent implementation chooses to do so.

This contract does **NOT** require event sourcing.

This contract does **NOT** require all state changes to be represented as Session Events.

------------------------------------------------------------------------

## 10. Legacy Event Compatibility

Legacy implementation event records are compatibility and migration concerns.

Legacy event records SHALL NOT redefine canonical Session Event semantics.

Legacy event records MAY be mapped to Session Events when a Session can be established and the mapping preserves historical meaning.

This contract does not define migration mechanics.

------------------------------------------------------------------------

## 11. Relationship to Dataset and Processing

Processing history belongs to Dataset and Processing semantics unless a processing-related fact is explicitly recorded as Session history.

Dataset and ProcessingRun semantics are defined by:

> **DSS-CTR-012 --- Dataset and Processing Contract**

A ProcessingRun SHALL NOT become a Session Event merely because it exists.

------------------------------------------------------------------------

## 12. Contract Boundaries

This contract defines Session Event identity, Session ownership, historical fact semantics, ordering, related Observation references, related Session Member references, optional payload/provenance, and event-sourcing boundaries.

This contract does **NOT** define Session, Observation, Session Member, Session Context, Dataset, ProcessingRun, Capture, Frame, provider APIs, database tables, repository patterns, or user-interface behaviour.

------------------------------------------------------------------------

## 13. Compatibility Principle

Existing event-like records MAY be adapted to this contract when they represent historical facts associated with a Session.

Compatibility mechanisms SHALL NOT weaken the rule that every canonical Session Event belongs to exactly one Session.

Compatibility mechanisms SHALL NOT create a separate ObservationEvent authority unless a future approved contract does so explicitly.

------------------------------------------------------------------------

## 14. Contract Authority

Within the TSN DSS operating framework, this document is the authoritative definition of **Session Event**.

Where a dependent contract, policy, procedure, integration, or implementation conflicts with the Session Event semantics established here, **DSS-CTR-006 takes precedence for Session Event semantics** unless superseded by a later approved revision.

------------------------------------------------------------------------

**END OF DSS-CTR-006**
