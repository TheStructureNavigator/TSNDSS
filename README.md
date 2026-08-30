# TSN Deep Space System

TSN DSS is a local deep-sky imaging workspace for managing projects, captures, processing runs, telescope state, and sky planning around a small SQLite-centered core.

Current foundation already covers the main v0.1 domain flow:

`Target -> AcquisitionPlan -> Observation -> Frames -> Dataset -> ProcessingRun -> Output`

## Repository Structure

- `tsn_dss/domain/` — normalized domain models
- `tsn_dss/engine/sqlite/` — SQLite repositories, validation, and database bootstrap
- `tsn_dss/engine/projects.py` — project, capture, and run folder layout
- `tsn_dss/engine/project_processing.py` — TSN-managed Siril run workflow
- `tsn_dss/engine/telescope.py` — telescope state service, simulator, adapter registry, Seestar adapter skeleton
- `tsn_dss/gui/http_api.py` — local HTTP API used by the frontend
- `frontend/` — Vite + TypeScript web GUI
- `docs/app/` — external app-facing content such as version, changelog, and TODO data
- `tests/` — automated `unittest` coverage
- `docs/plans/` — active planning notes for cleanup, telescope state, and mosaic work
- `sqlite/` — schema, seed data, ERD, example queries, and reference database snapshot
- `projects/` — local project workspace, imported captures, runs, logs, and artifacts

## What Already Works

- SQLite initialization with `PRAGMA foreign_keys = ON`
- schema version control with `PRAGMA user_version = 1`
- CRUD and validation for targets, sites, equipment, plans, observations, frames, datasets, processing runs, and mosaic plans
- dataset ownership of exact frame membership through `dataset_frames`
- project-local capture import and run workspace creation
- headless Siril execution through the official CLI/script workflow
- preview export and run artifact tracking
- local HTTP API for projects, captures, runs, mosaics, and telescope state
- web GUI for:
  - project creation and deletion
  - capture import and browsing
  - Siril run creation, logs, outputs, and preview review
  - sky view with Aladin Lite
  - telescope simulator controls
  - hardware-agnostic telescope state and adapter selection
  - mosaic planning and panel selection

## Runtime Pieces

TSN DSS currently has three practical layers:

1. Python domain/engine

- owns the normalized models and workflow logic
- stores durable state in SQLite
- manages project folders and Siril runs

2. Local HTTP API

- exposes the engine to the frontend
- acts as the app bridge, not as a public network service
- also serves editable app metadata such as Core changelog/TODO content

3. Web GUI

- runs locally in the browser
- renders Core, Projects, Processing, and Sky workspaces
- uses Aladin Lite for sky visualization

## How to Run Tests

```bash
py -m pip install -r requirements.txt
py -m unittest discover -s tests -v
```

## How to Run the Local App

Start the Python API:

```bash
py -m pip install -r requirements.txt
py -m tsn_dss.gui.http_api --projects-root projects
```

Start the frontend:

```bash
cd frontend
npm install
npm run dev
```

Or use the root helper:

```bat
run_tsn_dss_gui.bat
```

## Siril Path

If needed, set the local Siril CLI path in:

- [run_tsn_dss_gui.bat](/abs/path/C:/Users/treze/OneDrive/Desktop/TSN_DSS/run_tsn_dss_gui.bat)

Example:

```bat
set "TSN_DSS_SIRIL_EXECUTABLE=C:\PATH\TO\siril-cli.exe"
```

That value is then used as the default executable in the GUI run form.

## Main Reference Files

- [sqlite/README.md](/abs/path/C:/Users/treze/OneDrive/Desktop/TSN_DSS/sqlite/README.md)
- [sqlite/ERD.md](/abs/path/C:/Users/treze/OneDrive/Desktop/TSN_DSS/sqlite/ERD.md)
- [docs/plans/REPO_CLEANUP_AUDIT_PLAN.md](/abs/path/C:/Users/treze/OneDrive/Desktop/TSN_DSS/docs/plans/REPO_CLEANUP_AUDIT_PLAN.md)
- [docs/plans/TELESCOPE_STATE_PLAN.md](/abs/path/C:/Users/treze/OneDrive/Desktop/TSN_DSS/docs/plans/TELESCOPE_STATE_PLAN.md)
- [docs/plans/TSN_DSS_Mosaic_Planner_Plan.md](/abs/path/C:/Users/treze/OneDrive/Desktop/TSN_DSS/docs/plans/TSN_DSS_Mosaic_Planner_Plan.md)
- [docs/app/core-content.json](/abs/path/C:/Users/treze/OneDrive/Desktop/TSN_DSS/docs/app/core-content.json)
- [tsn_dss/domain/models.py](/abs/path/C:/Users/treze/OneDrive/Desktop/TSN_DSS/tsn_dss/domain/models.py)
- [tsn_dss/engine/telescope.py](/abs/path/C:/Users/treze/OneDrive/Desktop/TSN_DSS/tsn_dss/engine/telescope.py)
- [tsn_dss/gui/http_api.py](/abs/path/C:/Users/treze/OneDrive/Desktop/TSN_DSS/tsn_dss/gui/http_api.py)
- [tests/test_gui_api.py](/abs/path/C:/Users/treze/OneDrive/Desktop/TSN_DSS/tests/test_gui_api.py)
- [tests/test_telescope_state.py](/abs/path/C:/Users/treze/OneDrive/Desktop/TSN_DSS/tests/test_telescope_state.py)

## Current Direction

The next major direction is observation planning on top of the current project, sky, and telescope foundation.

That means TSN DSS should grow in three connected areas:

- observation sites with persistent coordinates and site metadata
- observing conditions such as weather, darkness windows, Moon/Sun context, altitude, and airmass
- hardware adapters that stay behind the normalized telescope-state contract, including the future real `seestarpy` implementation

The goal is to keep the frontend hardware-agnostic while gradually connecting:

`project target -> mosaic plan -> visibility window -> telescope state -> future observation session`
