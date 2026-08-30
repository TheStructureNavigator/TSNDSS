# TSN DSS Repo Cleanup Audit Plan

Date: 2026-08-30

## Goal

Prepare the repository for the next integration phase, especially real hardware adapter work, without changing the current behavior of TSN DSS.

This cleanup pass should:

- improve readability
- reduce stale or misleading documentation
- remove obsolete repo leftovers
- make core classes and functions easier to understand
- keep the app buildable and testable after every small step

## Current Repo Snapshot

The repository already has a solid functional base:

- normalized domain model in `tsn_dss/domain/models.py`
- SQLite-backed repositories in `tsn_dss/engine/sqlite/`
- project/capture/run storage in `tsn_dss/engine/projects.py`
- Siril workflow integration in `tsn_dss/engine/project_processing.py` and `tsn_dss/engine/siril.py`
- local HTTP API in `tsn_dss/gui/http_api.py`
- web GUI in `frontend/`
- telescope state + mosaic planning foundations already in place
- automated tests covering database, processing, GUI API, mosaic, and telescope state

This means the repo does not need an architectural rewrite before `seestarpy`.
It mainly needs cleanup, documentation, and boundary sharpening.

## Audit Findings

### 1. Documentation drift exists

Confirmed issues:

- `README.md` is partially outdated and internally inconsistent.
- `tsn_dss/gui/README.md` still describes a much smaller API surface than the code now exposes.
- root-level plans exist, but they are not yet clearly organized as active documentation vs historical planning notes.

Examples:

- the root README still says there is no GUI in one place, while the repo clearly has a working web GUI in `frontend/`
- the GUI README says `http_api.py` mainly exposes `GET /api/health` and `GET /api/projects`, while the file now serves a much broader API

### 2. Code readability is now more important than feature count

The core modules already carry real behavior and are no longer “throwaway prototypes”:

- `tsn_dss/engine/telescope.py`
- `tsn_dss/gui/http_api.py`
- `tsn_dss/engine/project_processing.py`
- `tsn_dss/engine/projects.py`
- `frontend/src/main.ts`
- `frontend/src/app/shell.ts`

These files now deserve:

- module docstrings
- short class docstrings
- short function docstrings for non-obvious behavior
- clearer separation between public API helpers and internal helpers

### 3. Public API surfaces may be broader than needed

Both:

- `tsn_dss/__init__.py`
- `tsn_dss/engine/__init__.py`

re-export a large number of symbols.

This is not necessarily wrong, but it increases maintenance cost and makes it harder to see what is intentionally public.

Cleanup task:

- review exports
- keep only symbols that are actually part of the intended package surface
- avoid breaking tests or internal imports

### 4. `tsn_dss/gui/` naming is now slightly misleading

At this point:

- `frontend/` is the actual GUI
- `tsn_dss/gui/` is mostly the backend bridge for the GUI

This is not urgent enough to justify a rename right now, but it should be documented more clearly.

Recommendation:

- clarify this in docs first
- postpone any module rename until after hardware work, unless it becomes painful

### 5. One obvious obsolete file path is already being removed

Current git status shows:

- `D tools/generate_favicon.py`

This strongly suggests it is already obsolete in the current workflow.

Cleanup task:

- confirm no remaining references
- keep it deleted if unused

### 6. Root-level documentation is getting crowded

Current root contains active planning documents such as:

- `TELESCOPE_STATE_PLAN.md`
- `TSN_DSS_Mosaic_Planner_Plan.md`
- this new cleanup plan

That is still manageable, but if we continue adding plans, the root will become noisy.

Recommendation:

- keep this cleanup pass focused first
- then consider moving long-lived plans into a dedicated `docs/` directory

### 7. Frontend structure is workable but now worth documenting

The frontend is still intentionally simple:

- `main.ts` orchestrates state and bindings
- `shell.ts` renders views
- `sky.ts` owns Aladin integration
- `api.ts` owns HTTP calls

This is a good minimal structure.

The main risk is not the structure itself, but lack of comments around ownership boundaries.

Cleanup task:

- document file responsibilities
- keep structure flat unless pain is clearly visible

### 8. No strong evidence yet for large dead-code removal

From the audit so far, the repo looks more “under-documented” than “full of dead code”.

So the cleanup should focus on:

- stale docs
- naming clarity
- docstrings
- intentional exports
- obsolete helper/script leftovers

rather than aggressive deletion or refactoring.

## Cleanup Principles

During the cleanup pass:

1. do not change domain behavior unless necessary
2. do not rewrite architecture for aesthetics
3. prefer small, testable commits
4. keep every step buildable
5. if a cleanup changes public naming, do it only with strong justification

## Proposed Cleanup Milestones

### Milestone C1 — Documentation Reality Check

Status: completed on 2026-08-30

Goal:

Make the docs describe the repo as it actually exists today.

Tasks:

- [x] refresh `README.md`
- [x] refresh `tsn_dss/gui/README.md`
- [x] clearly describe current runtime pieces:
  - domain
  - SQLite engine
  - project/run processing
  - GUI backend API
  - frontend GUI
  - sky / telescope / mosaic work
- [x] remove stale statements about missing GUI or overly small API scope

Testable outcome:

- a new contributor can read the docs and correctly understand how the repo is structured and launched

### Milestone C2 — Python Module and API Documentation

Status: completed on 2026-08-30

Goal:

Make the Python side self-explanatory enough for future adapter work.

Primary files:

- `tsn_dss/domain/models.py`
- `tsn_dss/engine/projects.py`
- `tsn_dss/engine/project_processing.py`
- `tsn_dss/engine/siril.py`
- `tsn_dss/engine/telescope.py`
- `tsn_dss/gui/http_api.py`

Tasks:

- [x] add module docstrings
- [x] add class docstrings
- [x] add targeted function docstrings for non-obvious flows
- [x] document important invariants and responsibilities

Testable outcome:

- core files are readable without reverse-engineering every function call

### Milestone C3 — Frontend Responsibility Cleanup

Status: completed on 2026-08-30

Goal:

Document and lightly tidy frontend ownership boundaries.

Primary files:

- `frontend/src/main.ts`
- `frontend/src/app/shell.ts`
- `frontend/src/app/sky.ts`
- `frontend/src/app/api.ts`

Tasks:

- add comments for state ownership and rendering responsibilities
- tighten naming where obviously confusing
- remove any tiny stale leftovers if found during the pass

Testable outcome:

- frontend flow is easier to extend without guessing where new behavior belongs

### Milestone C4 — Export Surface and Obsolete File Review

Status: completed on 2026-08-30

Goal:

Reduce confusion around what is public and what is legacy.

Primary files:

- `tsn_dss/__init__.py`
- `tsn_dss/engine/__init__.py`
- obsolete helper/script paths

Tasks:

- [x] review re-exports
- [x] remove only clearly unnecessary exports
- [x] verify deleted helpers are not referenced
- [x] keep changes conservative

Testable outcome:

- package entrypoints look intentional
- obsolete file leftovers are gone

### Milestone C5 — Repo Layout Follow-up

Status: completed on 2026-08-30

Goal:

Leave the repository in a cleaner long-term shape without changing behavior.

Tasks:

- [x] decide whether to introduce a `docs/` directory for active plans
- [x] move planning documents only if the value is clear
- [x] keep root minimal and practical

Testable outcome:

- root repo view is easier to scan

## Recommended Execution Order

1. C1 — docs reality check — completed
2. C2 — Python docstrings — completed
3. C3 — frontend responsibility cleanup — completed
4. C4 — exports and obsolete leftovers — completed
5. C5 — optional repo layout pass — completed

## What Should Stay Out of Scope for This Cleanup

Not part of this pass:

- real `seestarpy` implementation
- hardware networking
- major router refactor of `http_api.py`
- deep frontend architecture rewrite
- migration from current GUI/backend structure
- large package renames

## Definition of Done

This cleanup pass is successful if:

- docs match the real state of the repo
- key Python and frontend files explain themselves clearly
- obvious obsolete leftovers are removed
- tests still pass
- the repo feels ready for real hardware adapter work

## Recommended Next Step

Cleanup pass complete. The repo is ready for the next hardware-facing phase.
