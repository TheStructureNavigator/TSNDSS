# TSN Deep Space System

## DSS-CTR-005 --- Session Context Contract

**Status:** Draft\
**Version:** 0.2\
**Authority:** TSN DSS\
**Scope:** Contextual information associated with TSN DSS Sessions

------------------------------------------------------------------------

## 1. Purpose

This contract defines the canonical concept of **Session Context** within TSN Deep Space System.

All contracts, policies, procedures, software components, integrations, and operational processes that reference Session Context **SHALL** use the meaning established by this contract.

Session Context provides contextual information associated with a Session without making external providers the owners of Session semantics.

------------------------------------------------------------------------

## 2. Session Context

**Session Context** is contextual information associated with a Session.

Session Context MAY include:

- weather,
- forecast conditions,
- observed environmental conditions,
- derived conditions,
- sky conditions,
- Site or location context,
- equipment state,
- power state,
- lighting state,
- music or ambience,
- human notes,
- safety-relevant context,
- other operationally relevant contextual information.

Session semantics are defined by:

> **DSS-CTR-001 --- Session Contract**

Session Context belongs to the Session as an association controlled by TSN DSS.

DSS owns the association of context with the Session.

Providers own their native domain semantics.

------------------------------------------------------------------------

## 3. Provider Independence

Session Context MAY be supplied by TSN DSS, human input, sensors, weather providers, astronomy services, music services, lighting systems, equipment adapters, orchestration systems, or other sources.

A context provider **SHALL NOT** own the Session.

A context provider **SHALL NOT** redefine Session, Observation, Session Plan, Session Event, Project, Capture, Frame, Dataset, or ProcessingRun semantics.

Failure or absence of an optional provider SHALL NOT prevent a Session from existing.

This contract does not define provider APIs.

------------------------------------------------------------------------

## 4. Context Categories

Session Context MAY be:

- **forecast**, representing expected or predicted conditions,
- **observed**, representing directly reported or measured conditions,
- **derived**, representing information computed from other context,
- **declared**, representing operator or member-provided statements,
- **referenced**, representing a pointer to contextual information owned elsewhere.

Forecast, observed, and derived context SHOULD remain distinguishable when that distinction is material to Session interpretation.

A derived context value SHOULD preserve enough provenance to understand its source category when available.

------------------------------------------------------------------------

## 5. Time-Varying Context

Session Context MAY evolve during a Session.

A Session MAY have multiple context states or context samples over time.

Time-varying context SHOULD preserve the time or interval for which the contextual information is relevant when that information is available.

Session Context changes MAY be recorded as Session Events when they are historically relevant according to:

> **DSS-CTR-006 --- Session Event Contract**

------------------------------------------------------------------------

## 6. Relationship to Observation

An Observation MAY preserve a context snapshot or contextual detail relevant to its execution.

Such an Observation snapshot or detail **SHALL NOT** redefine Session Context.

Such an Observation snapshot or detail SHOULD preserve historically relevant conditions for the Observation.

Observation semantics are defined by:

> **DSS-CTR-003 --- Observation Contract**

This contract does not create an ObservationContextSnapshot authority.

------------------------------------------------------------------------

## 7. Relationship to Session Members

Session Members MAY request, contribute, or receive Session Context when permitted by Session Member capabilities and Operator authority.

Session Member semantics are defined by:

> **DSS-CTR-002 --- Session Member Contract**

Session Member interaction with context does not transfer ownership of Session Context or provider systems.

------------------------------------------------------------------------

## 8. Relationship to Project and Provenance

Session Context MAY help interpret data associated with Projects, Captures, Frames, Datasets, or ProcessingRuns.

Session Context **SHALL NOT** own Project, Capture, Frame, Dataset, or ProcessingRun semantics.

Project semantics are defined by DSS-CTR-008.

Capture and Frame provenance semantics are defined by DSS-CTR-009.

Dataset and ProcessingRun semantics are defined by DSS-CTR-012.

------------------------------------------------------------------------

## 9. Contract Boundaries

This contract defines Session Context, provider-independence principles, forecast/observed/derived distinctions, time-varying context, relationship to Observation context snapshots, and context association ownership.

This contract does **NOT** define Session, Observation, Session Member, Session Event, Project, Capture, Frame, Dataset, ProcessingRun, provider APIs, database tables, repository patterns, user-interface behaviour, or storage mechanisms.

------------------------------------------------------------------------

## 10. Compatibility Principle

Existing telemetry, notes, weather records, provider states, or contextual records MAY be adapted to this contract when they are associated with a Session as contextual information.

Compatibility mechanisms SHALL preserve provider provenance when available and SHALL NOT make providers owners of DSS Session semantics.

------------------------------------------------------------------------

## 11. Contract Authority

Within the TSN DSS operating framework, this document is the authoritative definition of **Session Context**.

Where a dependent contract, policy, procedure, integration, or implementation conflicts with the Session Context semantics established here, **DSS-CTR-005 takes precedence for Session Context semantics** unless superseded by a later approved revision.

------------------------------------------------------------------------

**END OF DSS-CTR-005**
