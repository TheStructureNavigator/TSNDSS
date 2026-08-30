# TSN DSS GUI Backend Bridge

`tsn_dss/gui/` is not the browser frontend itself.

At the current stage:

- `frontend/` contains the actual Vite + TypeScript GUI
- `tsn_dss/gui/` contains the local Python bridge that serves the frontend

The key file here is:

- `http_api.py` — local HTTP API for the TSN DSS web app

## Current Responsibility

This module connects the frontend to the Python engine and local project workspace.

It currently exposes API behavior around:

- health/runtime info
- projects
- capture browsing and thumbnails
- project runs, logs, outputs, and preview regeneration
- project sky target updates
- telescope state, planned pointing, simulator control, and adapter selection
- mosaic CRUD and panel generation/selection

## Why This Folder Exists

This keeps the application boundary explicit:

- engine logic stays in `tsn_dss/engine/`
- browser UI stays in `frontend/`
- local app-facing HTTP glue stays in `tsn_dss/gui/`

So despite the name, this folder currently acts more like:

- GUI backend
- local app API
- frontend bridge

than a standalone GUI toolkit.

## Run the Local API

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

Or use:

```bat
run_tsn_dss_gui.bat
```

## Notes for Future Cleanup

The current naming is acceptable, but slightly historical.

For now the recommended approach is:

- keep the package name stable
- document the boundary clearly
- postpone any rename until it gives real value
