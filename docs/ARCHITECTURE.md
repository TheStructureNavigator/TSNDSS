# TSN DSS Architecture

Snapshot of the system as implemented at **v0.1.11**. Code, `sqlite/schema.sql` and the tests are the source of truth; if this document disagrees with them, fix this document.

Statements below are marked **implemented**, **scaffold** (structure exists, behavior does not), or **not present**. Nothing here is a roadmap.

## System purpose

TSN DSS is a local, single-user deep-sky imaging workbench. It manages capture folders and Siril processing runs, plans sky targets and mosaics, models observing sites, and assesses observing conditions. It runs entirely on the user's machine; the only network calls are to public map/weather/sky services (see [External integrations](#external-integrations)).

## Runtime architecture

```
Browser (Vite + TypeScript, no UI framework)
   │  fetch JSON  ── frontend/src/app/api.ts
   ▼
Local HTTP API   tsn_dss/gui/http_api.py   (stdlib ThreadingHTTPServer, 127.0.0.1:8765)
   │
   ├── tsn_dss/engine/*            services: projects, Siril runs, weather, astronomy,
   │                               light-pollution lookup, telescope state
   ├── tsn_dss/engine/sqlite/*     repositories over one SQLite file
   └── tsn_dss/domain/*            dataclasses + pure domain logic
```

- **Backend** is Python. `tsn_dss/gui/` is the HTTP bridge, not a GUI toolkit; the GUI is `frontend/`. The API is hand-routed by path prefix inside `_build_handler`, with no framework and no authentication (it binds to localhost by default).
- **Frontend** is a Vite/TypeScript app. `main.ts` owns one mutable `state` object, the event bindings and the API calls, and re-renders. `app/shell.ts` renders HTML strings for every workspace. `app/sky.ts` owns Aladin Lite, and `app/observation_center_map.ts` owns the Leaflet map. `app/api.ts` is the only HTTP client and defines the frontend copies of the API payload types.
- **SQLite** file defaults to `<projects-root>/tsn_dss.db` (`--database-path` overrides). Connections are opened per request.
- **Filesystem** under `--projects-root` (default `projects/`, git-ignored) holds project folders, imported captures, Siril workspaces, logs and outputs.
- Workspaces (`ViewName` in `shell.ts`): `core`, `projects`, `processing`, `sky`, `observationcenter`. Core shows version/changelog served from `docs/app/core-content.json` via `GET /api/core-content`.

## Two data worlds

This is the most important structural fact about the current codebase. There are two separate models of "a capture and its processing", and they are **not connected to each other**.

### A. SQLite observation domain (implemented and tested, not exposed by the HTTP app)

```
Target → AcquisitionPlan → Observation → Frames → Dataset → ProcessingRun → Output
```

- Models: `tsn_dss/domain/models.py`. Schema: `sqlite/schema.sql`. Repositories: `ObservationRepository`, `FrameRepository`, `DatasetRepository`, `ProcessingRunRepository` in `tsn_dss/engine/sqlite/`. A `Dataset` owns its exact frame membership through `dataset_frames`.
- `SirilProcessingService` (`engine/siril.py`) runs Siril for a `ProcessingRun` whose `Dataset` frames come from these repositories.
- Covered by `tests/test_*_repository.py` and `tests/test_siril_integration.py`.
- `http_api.py` never imports or calls these four repositories. Nothing in the frontend reaches them. No code path in the running app creates an `Observation`, `Frame`, `Dataset` or `ProcessingRun` row.

### B. Filesystem Project workflow (what the user-facing app uses)

```
Project → Capture → Run
```

- `engine/projects.py` (`ProjectStorage`) lays out `projects/<slug>/…`: captures, thumbnails, run workspaces, and `sky_target` metadata.
- `engine/project_processing.py` (`ProjectRunManager`) executes the stock Siril `OSC_Preprocessing` script headless against a capture, tracks run status/logs/artifacts on disk, and generates previews.
- The Projects and Processing workspaces are built on this. Run state lives in run folders, not in `processing_runs`.

### Where the two worlds touch

- The SQLite `PlanningRepository` and `MosaicRepository` (sites, targets, mosaics) **are** used by the app.
- `mosaic_plans.project_slug` is a plain string referencing a filesystem Project. It is not a foreign key and not a `Target`.
- Neither model is marked deprecated in code. Treat this as the current implementation boundary: new work should say explicitly which world it belongs to.

## Sky and telescope state

- **Sky workspace** (`app/sky.ts`): one Aladin Lite map with two tabs, *Telescope* and *Mosaic*. Project `sky_target` names are resolved to coordinates in the browser through Aladin's Sesame lookup.
- **Normalized state.** `TelescopeState`, `ImagingProfile` and `PlannedPointing` (`domain/models.py`) are the only telescope data the frontend consumes. `TelescopeSnapshot` (`engine/telescope.py`) bundles state, imaging profile, planned pointing and the active Site into one payload from `GET /api/telescope/state`.
- **Adapter abstraction** (implemented): `TelescopeAdapter` protocol, `TelescopeAdapterRegistry`, and `TelescopeStateService`, which owns the active adapter, planned pointing and the active Site. Adapters are swapped with `POST /api/telescope/active-adapter`; the frontend contract does not change.
- **Simulator adapter** (implemented): manual RA/Dec/Alt/Az updates, simulated slew to the planned pointing, `is_simulated = true`. Sky footprint and marker rendering are driven from its state.
- **Seestar adapter** (**scaffold**): registered and selectable. `connect()`/`disconnect()` only flip a local boolean, and state fields stay empty. `slew_to_coordinates()` raises `NotImplementedError`. There is no device discovery, network I/O, `seestarpy` dependency, preview, stacking or plan execution.
- **Known caveat:** the Seestar adapter and its registry descriptor advertise `can_slew_to_coordinates`, `can_park`, `can_set_tracking`, `can_stream_preview`, `can_start_stack` and `can_run_observation_plans` as `True`. These flags describe intended scope, not working behavior. Do not read them as capabilities. `POST /api/telescope/slew-to-planned` catches only `ValueError`, so slewing with the Seestar adapter active lets the `NotImplementedError` escape the request handler (traceback on the server console, no JSON error response to the client).
- **Planned pointing** is independent of live telescope state and is held in memory in `TelescopeStateService`.
- Both the simulator and the Seestar skeleton use the same hardcoded imaging profile (Seestar S30 Pro tele: 160 mm, 11.2 × 6.3 mm sensor). The `imaging_profile` is not user-editable through the API.

## Observation Center

Workspace with two tabs, **Sites** (Leaflet map plus site details) and **Conditions**. It uses the *active Site* selected in a top site bar.

### Sites and their lifecycle

| Concept | What it is | Where it lives |
|---|---|---|
| **Current Device** | The browser's geolocation fix, shown as a marker with accuracy circle | Ephemeral UI state (`current_device_position.ts`, module state in `observation_center_map.ts`). Never sent to the backend or stored. |
| **Candidate Site** | A map-clicked (or "Inspect this location") point being inspected, with a Light Pollution lookup result | Temporary UI state in `observation_center_map.ts`. Discarded on clear or next click. |
| **Site** | A persisted observing location | SQLite `sites` (+ `site_horizon_profile_points`) via `PlanningRepository` |
| **Active Site** | The Site currently driving Conditions, Sky and the telescope snapshot | In-memory in `TelescopeStateService._active_site`. **Not persisted**: a freshly started API has no active Site, the frontend adopts whatever the API reports, and the user must select a Site again. |

Flow: *Current Device* (optional) → *Inspect this location* → *Candidate Site* (fetches Light Pollution) → *Create Site* (persists, and the frontend then sets it active) → *Site Details* → *Conditions*. Existing Sites can refresh their Light Pollution values with "Update light pollution data". Site create/update is `POST /api/sites`, `POST /api/sites/{id}`; activation is `POST /api/sites/active`.

### Site data: three different kinds of "sky quality"

A `Site` carries these separately; they must not be merged or relabelled:

- **Location**: `latitude_deg`, `longitude_deg`, `elevation_m`. Coordinates are required for Conditions.
- **Manual / measured sky values**: `sqm_mag_arcsec2`, `bortle_class`. Entered by the user; TSN DSS never writes them from a model.
- **Modeled Light Pollution**: `lp_*` fields (`lp_artificial_brightness_mcd_m2`, `lp_natural_sky_ratio`, `lp_estimated_total_brightness_mcd_m2`, `lp_estimated_sqm_mag_arcsec2`, `lp_estimated_bortle_class`) plus provenance: `lp_dataset_name`, `lp_provider_name`, `lp_source`, `lp_source_unit`, `lp_data_kind`, `lp_updated_at`. `lp_data_kind` is constrained to `modeled` or `estimated` in the schema and the frontend always writes `modeled`. Estimated SQM/Bortle are derived values, not measurements.
- `south_horizon_open` is a separate legacy boolean flag on `Site`. It is unrelated to the Local Horizon Profile.

### Light Pollution: deliberate provider split

| Purpose | Provider | Where |
|---|---|---|
| **Visual map overlay only** | ArcGIS `ArtificialSkyBrightness` tile layer | `ARTIFICIAL_SKY_BRIGHTNESS_LAYER` in `frontend/src/app/light_pollution.ts`, rendered as a Leaflet tile layer. Its pixels are never read as data. |
| **Point lookup / modeled Site data** | Local New World Atlas (Falchi et al. 2016) GeoTIFF | `LocalRasterLightPollutionProvider` in `engine/light_pollution.py`, exposed as `GET /api/light-pollution?lat=&lon=` |

- The raster is not bundled or downloaded (git-ignored, license text in `data/light_pollution/README.txt`). It is configured with `--light-pollution-raster` or `TSN_DSS_LIGHT_POLLUTION_RASTER`.
- `lookup()` reads a single cell through `rasterio` without loading the raster, and returns a normalized `LightPollutionPointResult` with `status` one of `available`, `dataset unavailable`, `out of bounds`, `no data`. The overlay works even when the raster is missing.
- Backend conversion `estimate_light_pollution_from_artificial_brightness` derives natural-sky ratio, total brightness, SQM and Bortle from artificial brightness (mcd/m²).
- The frontend stores the returned values on the Site with `lp_data_kind = 'modeled'` (`site_light_pollution.ts`). The backend does not compute Light Pollution on Site save; the frontend supplies the payload.

### Local Horizon Profile

- **Data**: a list of `(azimuth_deg ∈ [0,360), min_altitude_deg ∈ [0,90])` points per Site in `site_horizon_profile_points`. Validated and sorted by `domain/local_horizon.py` (`normalize_local_horizon_profile`, duplicate azimuths rejected). Persisted and returned with the Site as `horizon_profile`.
- **Interpolation and use**: implemented **in the frontend**. `app/local_horizon.ts` interpolates altitude cyclically over azimuth; `app/local_horizon_conditions.ts` computes clearance (`target altitude − local horizon altitude`) and `clear` / `blocked` / `not_configured` / `unknown`, and applies it to observing windows. `shell.ts` uses it for the hourly table, the Horizon Compass and limiting factors.
- The backend stores the profile, and `domain/local_horizon.py` has an interpolation function (`get_local_horizon_altitude`) that only the tests call. **`engine/astronomy.py` does not read the profile.** Local Horizon is not a backend Conditions input today.
- An empty profile means no obstruction (`not_configured`), which does not block anything.
- Editing is manual (editor modal, template generation, clearing). There is no horizon import.

### Conditions

See the next section for the pipeline and the exact backend/frontend split.

## Conditions pipeline

```
Site (id → lat/lon/elevation) + reference time + target + min altitude + forecast range
   │
   ├─ GET /api/site-forecast           weather   (Open-Meteo, normalized in weather.py)
   └─ GET /api/astronomical-conditions astronomy (astropy/astroplan, engine/astronomy.py)
           Sun/twilight/night, Moon, hourly target Alt/Az, airmass, Moon–target separation
   │
   ▼  frontend joins the two hourly series by time
   Observing Window        (frontend, shell.ts + local_horizon_conditions.ts)
   Local Horizon clearance (frontend)
   Condition Assessment    (frontend, conditions_assessment.ts) → Overall GOOD / MODERATE / POOR
```

### Ownership

**Backend owns**
- Weather retrieval from Open-Meteo, normalization to `SiteForecastSnapshot`, and derived dew margin and dew risk (`weather.py`). The forecast range is 1–16 days.
- Astronomy: sky state (day/civil/nautical/astronomical twilight/night), twilight and astronomical-night boundaries, sunrise/sunset, Moon altitude/azimuth/illumination/phase, moonrise/moonset, and for a target: hourly altitude, azimuth, airmass, Moon–target separation, transit, and the above-horizon window.
- The configured minimum target altitude: `min_target_altitude_deg` query parameter (default 30), producing `target_above_observation_threshold` per hour (`altitude ≥ min`). The frontend currently always sends 30.
- Target resolution for a request: explicit `target_ra_deg`/`target_dec_deg`, else a `target_name` matching a stored Target, else `mosaic_panel_id`, `target_id`, or `use_planned_pointing`.

**Frontend owns** (nothing below is a backend domain concern)
- Local Horizon interpolation, clearance, and CLEAR/BLOCKED semantics.
- Observing Window definition: an hour qualifies if the sky is `astronomical_night`, target altitude ≥ the minimum, cloud cover ≤ 35 % (`OBSERVING_WINDOW_MAX_CLOUD_COVER_PCT` in `shell.ts`), and the target is clear of the Local Horizon. Consecutive qualifying hours form a window.
- Context-aware Condition Assessment: factors are Moon illumination, Moon altitude, Moon–target separation, dew risk, wind and gusts. Moon thresholds depend on an intent profile (`neutral`, `broadband`, `dual_band`, `narrowband`) inferred from the mosaic plan's `observation_type` and `filter`.
- Overall Assessment (GOOD / MODERATE / POOR), weighted aggregation, and limiting-factor text.
- Selection of which target feeds the request. The frontend sends, in priority order, the selected mosaic panel, then planned pointing, then the selected project's `sky_target` (resolved through Aladin/Sesame), then no target.

### Three separate "horizon" concepts

| Concept | Meaning | Defined in |
|---|---|---|
| **Geometric horizon** | Target altitude > 0° (`target_above_horizon`) | Backend, `astronomy.py` |
| **Minimum target altitude** | User/system threshold, default 30° (`target_above_observation_threshold`) | Backend, `astronomy.py` |
| **Local Horizon obstruction** | Site-specific terrain/obstacle altitude per azimuth | Frontend, applied on top of both |

These are independent constraints. A target can be above the minimum altitude and still be `blocked` by the Local Horizon, and the reverse.

## Mosaic planning

- **Model**: `MosaicPlan` → `MosaicPanel[]`, tables `mosaic_plans` / `mosaic_panels`, `MosaicRepository`. Endpoints under `/api/mosaics`, `/api/mosaic-panels`. Plans belong to a filesystem Project through `project_slug`.
- **Panels are sky footprints, not UI cells.** A panel's persistent address is `center_ra_deg` / `center_dec_deg`. Alt/Az is never stored on a panel; it is computed at request time by the astronomy service for the active Site and time.
- **FOV comes from the imaging profile at plan creation.** `fov_width_deg`, `fov_height_deg`, `imaging_profile_id` and `imaging_profile_label` are copied onto the plan (defaulting from the telescope snapshot's profile). `imaging_profile_id` is a plain string, not a foreign key, and later profile changes do not alter existing plans.
- **Generation** (`_generate_regular_mosaic_panels`): a regular grid, step = FOV × (1 − overlap), rows and columns centered on the plan center, RA offsets divided by cos(Dec), RA wrapped to [0, 360). Limited to 256 panels (`MAX_GENERATED_PANELS`). Validation: `0 ≤ overlap < 100`, FOV > 0, region > 0, RA/Dec ranges. The plan's `rotation_deg` is copied onto each panel; **the grid layout itself is axis-aligned in RA/Dec** and is not rotated.
- **Plan-level intent, panel-level geometry.** `observation_type` and `filter` are on `MosaicPlan`, not `MosaicPanel`. Panel geometry does not depend on the filter, and the same footprint can be observed with different filters. These fields feed the Condition Assessment intent profile.
- **Panel status** (`not_started` / `in_progress` / `complete`) and `target_integration_seconds` / `acquired_integration_seconds` are set manually. They are not derived from captures, runs or `Observation` rows.
- **Selecting a panel is two separate steps.** `POST /api/mosaics/{id}/select-panel` only records `selected_panel_id` on the plan. Making it the planned pointing is a separate frontend call to `POST /api/telescope/planned-pointing` (`source_kind = 'mosaic_panel'`).
- **Not present**: manual panel add/move/delete, region extension, per-panel visibility scheduling, stitching, or any automatic acquisition.

## Persistence

- One SQLite database. `sqlite/schema.sql` is applied only when the database has no user tables, then `PRAGMA user_version = 1` is asserted. `EXPECTED_USER_VERSION = 1` in `engine/sqlite/db.py`; a mismatch raises `SchemaVersionError`.
- **There is no migration framework.** Schema evolution is done by additive, idempotent code that runs against existing databases, and the same definitions are repeated in `schema.sql`:
  - `db.py::_ensure_backward_compatible_columns` adds the `lp_*` columns to `sites` and creates `site_horizon_profile_points`.
  - `MosaicRepository.__init__` calls `ensure_mosaic_schema`, which creates `mosaic_plans` / `mosaic_panels` and adds optional columns.
- A schema change therefore needs edits in `schema.sql` **and** an additive upgrade path; bumping `user_version` alone has no upgrade mechanism.
- **Repository ownership**: `PlanningRepository` (targets, sites, horizon points, equipment, plans/sequences), `MosaicRepository` (mosaics), and the four World-A repositories listed above. Each takes a connection; `transaction()` does not nest.
- Filesystem state (projects, captures, runs, thumbnails, logs) is outside SQLite. The active Site, planned pointing, active adapter and simulator state are in process memory only.
- Tables `surveys`, `survey_targets`, `events`, `telemetry` and two views exist in the schema. No Python repository or API uses them.

## External integrations

| Integration | Called from | Used for | Notes |
|---|---|---|---|
| **Open-Meteo Forecast** | Backend (`weather.py`) | Hourly weather, 1–16 days | Only forecast is used. No geocoding endpoint exists in the code. |
| **ArcGIS `ArtificialSkyBrightness` tiles** | Browser (Leaflet) | Visual overlay only | Never used as data. |
| **New World Atlas / Falchi 2016 GeoTIFF** | Backend (`rasterio`) | Modeled point values | Local file, user-supplied. |
| **OpenStreetMap tiles** | Browser (Leaflet) | Base map | |
| **Aladin Lite** (`aladin-lite` npm) | Browser | Sky map, footprints, Sesame name resolution | Sky survey tiles and Sesame are remote services fetched by Aladin. |
| **Siril CLI** | Backend subprocess | `OSC_Preprocessing` runs | Executable from `TSN_DSS_SIRIL_EXECUTABLE` or the run form. Script defaults to `siril-1.4.4/scripts/OSC_Preprocessing.ssf` (`DEFAULT_OSC_SCRIPT_PATH`); that directory is git-ignored and must be supplied locally, or a script path given in the run form. |
| **astropy / astroplan** | Backend | Ephemeris and Alt/Az. IERS auto-download is disabled and the built-in solar-system ephemeris is used. | |
| **Pillow** | Backend | Capture thumbnails and previews | Optional import. |
| **Seestar (`seestarpy`)** | — | — | Not integrated (see scaffold above). |

## Architectural rules

Each rule below is supported by current code.

1. **The frontend never consumes provider structures.** Open-Meteo, astropy and raster responses are normalized in the backend (`SiteForecastSnapshot`, `AstronomicalConditionsSnapshot`, `LightPollutionPointResult`). The frontend uses only the local API payloads. (Leaflet/Aladin tile and Sesame calls are direct browser-to-provider calls for map rendering.)
2. **Backend owns external data calls for domain data** (weather, astronomy, raster lookup). Map tiles are the exception.
3. **Modeled Light Pollution is never measured data.** It lives in `lp_*` fields with provenance and `lp_data_kind`, separate from `sqm_mag_arcsec2` / `bortle_class`.
4. **The ArcGIS overlay is visual only;** point data comes from the local raster.
5. **Current Device is ephemeral UI state; Candidate Site is temporary inspection state; Site is persistent; Active Site is runtime selection held in memory.**
6. **Local Horizon is Site-specific physical obstruction data,** stored per Site. It is separate from the minimum target altitude and from the geometric horizon.
7. **Local Horizon and Condition Assessment are frontend logic today.** New backend code must not assume it already applies horizon obstruction.
8. **Telescope UI consumes normalized `TelescopeState` / `TelescopeSnapshot` only.** Vendor specifics belong inside an adapter, behind the `TelescopeAdapter` protocol.
9. **Adapter capability flags are not proof of behavior.** Check the implementation.
10. **Site is the anchor entity for planning.** Weather, astronomy and Local Horizon all hang off a Site.
11. **Panels are stored as RA/Dec.** Alt/Az is always derived for a Site and a time.
12. **Filter and observation intent are plan-level metadata,** not panel geometry.
13. **The SQLite observation domain and the filesystem Project workflow are separate.** State which one a change targets; do not assume data written in one is visible in the other.
14. **Schema changes need both `schema.sql` and an additive upgrade path** (see Persistence).

## Testing

- Python: `py -m unittest discover -s tests -v` (`pytest` also collects these).
- Frontend: `cd frontend && npm test` runs the Node built-in test runner on `frontend/tests/*.test.ts`. It covers Local Horizon, Local Horizon Conditions, Condition Assessment, Light Pollution and Current Device Position logic. Rendering (`shell.ts`, `main.ts`, `sky.ts`, `observation_center_map.ts`) and the HTTP client have no frontend tests.
- API behavior is tested from Python in `tests/test_gui_api.py`.
