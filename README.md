# TSN Deep Space System (DSS)

TSN DSS is a local, single-user deep-sky imaging workbench: it manages capture folders and Siril processing runs, plans sky targets and mosaics, models observing sites, and assesses observing conditions. A Python backend serves a local HTTP API to a Vite + TypeScript web app, with SQLite for durable state.

Current version: **0.1.11** (see [docs/app/core-content.json](docs/app/core-content.json) for the changelog).

For how the system is structured, what owns what, and known caveats, read **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

## Current capabilities

Implemented:

- **Projects** — create/delete projects, import captures, browse captures with cached thumbnails.
- **Processing** — headless Siril `OSC_Preprocessing` runs per capture, with logs, output files and previews.
- **Sky** — Aladin Lite map, telescope marker and field-of-view footprint driven by a normalized telescope state, simulator controls, planned pointing.
- **Mosaic planner** — regular panel grids from an imaging profile and overlap, panel selection and status, plan-level observation type and filter.
- **Observation Center** — persistent observing Sites on a map, Current Device position, Candidate Site inspection, modeled Light Pollution per Site, manual Local Horizon Profile, and Conditions (weather, Sun/Moon/twilight, target Alt/Az, observing windows, overall assessment).

Scaffold only (structure exists, no working behavior):

- **Seestar adapter** — registered and selectable, but it does not connect to a device or slew. Only the simulator adapter works.

Present in code but not reachable from the app:

- The SQLite `Observation` / `Frame` / `Dataset` / `ProcessingRun` repositories are implemented and tested, but the local API does not expose them. The Projects and Processing workspaces run on the filesystem instead. See the "Two data worlds" section of the architecture doc.

## Requirements

- Python 3.12 (the version used in development) with `pip`
- Node.js with native TypeScript stripping (22.18 or newer; tested on 24.14) and npm
- [Siril](https://siril.org/) CLI for processing runs (optional for everything else)

## Quick start

Windows helper — starts the API and the frontend in two terminals:

```bat
run_tsn_dss_gui.bat
```

It also sets `TSN_DSS_SIRIL_EXECUTABLE` and `TSN_DSS_LIGHT_POLLUTION_RASTER`; edit the paths in that file for your machine.

Or start each part yourself.

Backend (listens on `http://127.0.0.1:8765`):

```bash
py -m pip install -r requirements.txt
py -m tsn_dss.gui.http_api --projects-root projects
```

Frontend:

```bash
cd frontend
npm install
npm run dev
```

## Configuration

Backend options: `--host`, `--port`, `--projects-root` (default `projects`), `--database-path` (default `<projects-root>/tsn_dss.db`), `--light-pollution-raster`.

### Siril

Processing runs need the Siril CLI. Set it in the environment (it becomes the default in the run form):

```bat
set "TSN_DSS_SIRIL_EXECUTABLE=C:\PATH\TO\siril-cli.exe"
```

The default script is `siril-1.4.4/scripts/OSC_Preprocessing.ssf` under the repository root. That directory is git-ignored, so place a Siril distribution there, or enter a script path in the run form.

### Light Pollution raster

The Sites map shows the public ArcGIS `ArtificialSkyBrightness` layer as a visual overlay only. Point values for a Candidate Site come from a local New World Atlas / Falchi 2016 GeoTIFF, which TSN DSS does not download. Put the file somewhere and point at it with either:

```bat
set TSN_DSS_LIGHT_POLLUTION_RASTER=C:\PATH\TO\World_Atlas_2015.tif
```

```bash
py -m tsn_dss.gui.http_api --projects-root projects --light-pollution-raster C:\PATH\TO\World_Atlas_2015.tif
```

Without the raster, Candidate Site reports `Light pollution dataset unavailable` and the overlay still works. Data license and citation requirements are in [data/light_pollution/README.txt](data/light_pollution/README.txt).

## Tests

```bash
py -m pip install -r requirements.txt
py -m unittest discover -s tests -v      # backend (python -m pytest tests also works)

cd frontend
npm test                                  # frontend logic tests, Node built-in runner
```

## Repository map

- `tsn_dss/domain/` — dataclass models and pure domain logic
- `tsn_dss/engine/` — services: projects, Siril, weather, astronomy, light pollution, telescope
- `tsn_dss/engine/sqlite/` — schema bootstrap and repositories
- `tsn_dss/gui/http_api.py` — local HTTP API used by the frontend
- `frontend/` — Vite + TypeScript web app (`src/main.ts`, `src/app/`, `tests/`)
- `sqlite/` — `schema.sql` and `seed.sql`
- `docs/ARCHITECTURE.md` — current architecture
- `docs/app/core-content.json` — version, changelog and TODO list shown in the app's Core page
- `tests/` — backend `unittest` suite
- `data/light_pollution/` — location for the local raster (the `.tif` is git-ignored)
- `projects/` — local workspace: projects, captures, runs and the SQLite file (git-ignored)
