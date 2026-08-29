# TSN DSS Telescope State and Sky Integration Plan

## Goal

Build a hardware-agnostic telescope state layer inside TSN DSS that can drive the Sky view in Aladin Lite and later support live capture workflows.

The core rule is:

- TSN DSS owns the telescope state model
- hardware integrations only adapt external device data into that model
- the frontend reads only TSN DSS state, never vendor-specific APIs directly

This allows the same architecture to support:

- Seestar S30 Pro
- future custom rigs
- simulator mode
- later ASCOM / INDI / other adapters

## Design Principles

1. Hardware independence

The system must not be designed around a single telescope vendor.

2. TSN DSS as the source of truth

The application owns the normalized telescope state and serves it to the frontend.

3. Simulator first

Before connecting real hardware, the architecture should work with a simulated adapter.

4. Pointing before live capture

First solve the question "where is the telescope pointing?".
Only later attach live capture, exposure progress, and preview workflows.

5. Separation of mount state and imaging profile

Where the telescope points is not the same as what field of view the current imaging setup covers.

## Current Status

### Completed

#### Phase A — Telescope State Foundation

Goal:

Create the neutral internal model for telescope state and a first simulator-backed implementation.

Completed:

- [x] define `TelescopeState`
- [x] define `ImagingProfile`
- [x] create simulator adapter
- [x] create backend state service
- [x] expose a basic API endpoint

#### Phase B — Sky Overlay Integration

Goal:

Show the current telescope pointing inside Aladin Lite.

Completed:

- [x] fetch telescope state in frontend
- [x] render pointing marker in Aladin
- [x] show simulated vs real source status if needed

#### Phase C — FOV and Footprint

Goal:

Show not only the telescope position, but also the current field of view.

Completed:

- [x] focal length / sensor / rotation model
- [x] FOV calculation
- [x] footprint overlay rendering

#### Cross-phase UI and behavior work already done

- [x] manual RA/Dec input in simulator controls
- [x] optional Alt/Az input
- [x] simple target label
- [x] `is_simulated = true`
- [x] `GET /api/telescope/state`
- [x] `POST /api/telescope/simulator/state`
- [x] frontend renders normalized telescope state only
- [x] simulator controls are available directly in the Sky workspace
- [x] telescope zoom no longer resets on `Update telescope`
- [x] Follow telescope toggle is available in Sky
- [x] default mode keeps the map fixed and moves only telescope marker / footprint
- [x] planned pointing state exists independently from live telescope state
- [x] selected mosaic panel can be stored as planned pointing
- [x] planned pointing marker and footprint are rendered in Sky
- [x] map click can select a mosaic panel directly from the sky view
- [x] generic `slew to planned target` action is available for the simulator-backed workflow

### Remaining

#### Phase D — Hardware Adapter Layer

Goal:

Attach real telescope sources without changing the frontend contract.

Remaining:

- [ ] define adapter interface
- [ ] define adapter lifecycle contract
- [ ] add adapter registration / selection
- [ ] add first real adapter when ready

Already done inside this phase:

- [x] polling / refresh model
- [x] planned target action shape can stay frontend-stable while adapters evolve

#### Phase E — Live Capture Integration

Goal:

Connect telescope state with future live capture workflows.

Remaining:

- [ ] bind telescope state to active capture session
- [ ] add optional live frame metadata
- [ ] add optional plate solve feedback loop
- [ ] add optional exposure / stacking progress integration

## Core Domain Model

### 1. TelescopeState

Minimal normalized state:

- `adapter_id`
- `source_kind` (`simulator`, `seestar`, `custom_rig`, etc.)
- `timestamp_utc`
- `connected`
- `status` (`idle`, `slewing`, `tracking`, `parked`, `error`)
- `is_simulated`
- `site_lat_deg`
- `site_lon_deg`
- `site_elevation_m` optional
- `ra_hours` optional
- `dec_deg` optional
- `alt_deg` optional
- `az_deg` optional
- `target_name` optional
- `position_quality` optional

Notes:

- RA/Dec is the most important representation for Aladin
- Alt/Az is still useful for diagnostics and future hardware support
- some hardware may expose one set first, then we derive the other if needed

### 2. ImagingProfile

Minimal normalized profile:

- `profile_id`
- `label`
- `focal_length_mm`
- `sensor_width_mm`
- `sensor_height_mm`
- `pixel_size_um` optional
- `rotation_deg` optional
- `binning` optional

Derived values:

- `fov_width_deg`
- `fov_height_deg`

Notes:

- this should stay independent from the mount state
- one mount may be used with different optical setups

### 3. TelescopeSnapshot

A practical API payload can combine both:

- `telescope_state`
- `imaging_profile`
- derived `footprint`

This allows the frontend to fetch one payload and render both marker and FOV overlay.

## Adapter Model

Each hardware integration should implement the same conceptual contract:

- connect
- disconnect
- refresh current state
- report health / availability
- emit normalized `TelescopeState`

Initial adapter list:

- `simulator`
- `seestar` later
- `custom_rig` later

Important rule:

Adapters do not shape frontend behavior.
Adapters only provide normalized state to TSN DSS.

## Simulator Strategy

Simulator should be the first working adapter.

It should support:

- [x] manual RA/Dec input
- [x] optional Alt/Az input
- [x] simple target label
- [x] generic slew to planned target through TSN DSS state
- [ ] optional imaging profile selection
- [x] `is_simulated = true`

Why this matters:

- enables full frontend and Aladin work before hardware
- supports demos and testing
- keeps the architecture honest and vendor-neutral

## Backend Responsibilities

TSN DSS backend should:

- hold the latest normalized telescope state
- know which adapter currently owns that state
- expose read APIs for frontend
- later accept write/control actions for simulator mode

Minimal first backend API:

- [x] `GET /api/telescope/state`

Later likely APIs:

- [x] `POST /api/telescope/simulator/state`
- [x] `POST /api/telescope/planned-pointing`
- [x] `DELETE /api/telescope/planned-pointing`
- [x] `POST /api/telescope/slew-to-planned`
- [ ] `GET /api/telescope/adapters`
- [ ] `POST /api/telescope/active-adapter`
- [ ] `GET /api/telescope/profile`
- [ ] `POST /api/telescope/profile`

## Frontend Responsibilities

Frontend should:

- [x] fetch normalized telescope state from TSN DSS
- [x] render telescope position in Aladin
- [x] render FOV footprint
- [x] indicate whether source is simulated or real
- [x] render planned target independently from live position
- [x] allow selecting mosaic panels directly from the sky map
- [x] trigger slew to planned target from the Sky workspace

Important UI rule:

The Sky view should never care whether the source is Seestar, simulator, or a custom rig.
It should care only about the normalized state payload.

## Recommended Milestones

### Milestone 1 — Telescope State Foundation

Deliverables:

- [x] internal telescope state model
- [x] simulator adapter
- [x] backend telescope state service
- [x] `GET /api/telescope/state`

Testable outcome:

- backend returns a valid simulated telescope state payload

### Milestone 2 — Sky Marker in Aladin

Deliverables:

- [x] frontend polling or refresh
- [x] Aladin marker at current RA/Dec

Testable outcome:

- changing simulator pointing changes marker location in the Sky view

### Milestone 3 — Imaging Profile and FOV

Deliverables:

- [x] imaging profile model
- [x] derived FOV values
- [x] footprint overlay in Aladin

Testable outcome:

- changing profile updates the visible footprint size

### Milestone 4 — Real Adapter Contract

Deliverables:

- stable adapter interface
- simulator remains supported
- first real hardware adapter can be plugged in later

Testable outcome:

- a second adapter can be introduced without changing frontend code

### Milestone 5 — Live Capture Readiness

Deliverables:

- telescope state linked to future live capture workflows
- extension points for preview, solve center, and session state

Testable outcome:

- current pointing and future live capture metadata can coexist in one workflow

## Recommended Next Step

Work next on the hardware-agnostic adapter contract and adapter registry, while keeping real hardware integration out of scope for now. After that, add optional imaging profile selection and start shaping the bridge from planned target into future acquisition workflows.

Still avoid:

- real hardware integration at this stage
- live capture work at this stage
- coupling the Sky view directly to Seestar
