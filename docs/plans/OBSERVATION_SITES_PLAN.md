# TSN DSS Observation Sites Foundation Plan

Date: 2026-08-30

## Goal

Turn the existing `Site` domain model into a real application feature that can be used by the Sky workspace and later by observing conditions, sessions, and hardware workflows.

## Why now

TSN DSS already has:

- a `Site` model in the domain
- a `sites` table in SQLite
- observation records that can reference `site_id`
- telescope state snapshots that already carry normalized site coordinates

What is still missing is the application layer that makes sites editable, selectable, and visible in the live workflow.

## Scope for the first vertical slice

This milestone should deliver:

- SQLite repository support for listing sites
- local API CRUD for sites
- active observation-site selection for the Sky workspace
- telescope snapshots that reflect the currently active site
- a minimal Sky UI for choosing, creating, updating, and deleting sites
- automated tests for repository, API, and telescope-state behavior

## Explicitly out of scope

Not in this slice:

- weather providers
- astronomical darkness / Moon / Sun calculations
- altitude / airmass computation
- persistence of long-term planning windows
- automatic hardware geolocation sync
- observation-session orchestration

## Milestone breakdown

### S1 — Site API and service bridge

Goal:

Expose sites as a first-class API resource and allow one site to be active in the telescope-facing workflow.

Deliverables:

- `list_sites()` in the planning repository
- `GET /api/sites`
- `POST /api/sites`
- `POST /api/sites/{id}`
- `DELETE /api/sites/{id}`
- `POST /api/sites/active`
- `TelescopeStateService` support for an active site override

Testable outcome:

- the API can manage persistent sites
- the active site appears in the telescope snapshot

### S2 — Sky workspace site controls

Goal:

Manage observation sites directly where telescope state is already controlled.

Deliverables:

- site list in Sky → Telescope control
- select active site
- create site
- update active site
- delete active site

Testable outcome:

- a user can change the site in the Sky UI and immediately see it reflected in telescope state

### S3 — Foundation handoff for observing conditions

Goal:

Leave the repo ready for the next layer: visibility and conditions.

Deliverables:

- docs updated to mention `docs/app/` and site-aware roadmap
- stable API payload shape for active site metadata

Testable outcome:

- the next milestone can attach weather and visibility calculations without redesigning the site layer

## Recommended next milestone after this one

Observing Conditions MVP:

- Sun / Moon context
- darkness window
- target altitude
- airmass
- weather / atmosphere placeholders or provider integration
