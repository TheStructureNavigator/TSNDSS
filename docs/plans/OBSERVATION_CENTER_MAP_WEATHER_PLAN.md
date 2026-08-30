# TSN DSS Observation Center Map & Weather Plan

Date: 2026-08-30

## Goal

Build the next Observation Center slice around three stable, hardware-agnostic capabilities:

- observation sites managed inside TSN DSS
- map visualization for sites and telescope context
- weather and location lookup services that stay independent from any specific telescope vendor

This should prepare TSN DSS for future adapters such as Seestar, while keeping the frontend unaware of hardware-specific details.

## Provider choice

Recommended external foundations for v0.1.x:

- Map rendering: OpenStreetMap tiles
- Map UI layer: Leaflet
- Site search / geocoding: Open-Meteo Geocoding API
- Weather forecast: Open-Meteo Forecast API

Why this split:

- OpenStreetMap gives us a simple public basemap for Observation Center.
- Leaflet is a small, stable frontend layer and fits the current lightweight Vite setup.
- Open-Meteo gives us both geocoding and forecast data without forcing a paid dependency in the first slice.
- We avoid relying on public Nominatim for interactive search because its public usage policy is much stricter.

## Product direction

Observation Center should become the place where we manage:

- observing sites
- site map and map context
- current telescope position context
- basic observing conditions
- future mount / telescope adapter state

The Sky workspace should stay focused on sky interaction, target framing, telescope state, and mosaic planning.

## Architecture rules

### 1. Hardware agnostic state

The app should operate on normalized telescope/site state:

- site coordinates
- telescope RA / Dec
- optional Alt / Az
- status / movement state
- field of view

Adapters such as `SimulatedTelescopeAdapter` or future `SeestarAdapter` should translate vendor-specific APIs into that shared model.

### 2. Backend owns external API calls

TSN DSS should not call Open-Meteo directly from the browser for core workflows.

Instead:

- frontend talks only to local TSN DSS API
- backend calls Open-Meteo
- backend normalizes payloads
- backend can add caching, rate limiting, and fallback behavior

For the map itself, browser tile loading is acceptable, but provider-specific configuration should still be centralized in frontend map utilities.

### 3. Site remains the anchor entity

Observation Center should continue to treat `Site` as the stable planning object.

Derived data should hang off that:

- selected active site
- geocoding suggestions
- forecast snapshot
- later: darkness windows, Moon, seeing/transparency hints, horizon profile

## Milestone breakdown

### OCM1 — Observation Center map foundation

Goal:

Show sites on a real map inside Observation Center without changing telescope adapter behavior.

Deliverables:

- add Leaflet to frontend
- render Observation Center map with OpenStreetMap tiles
- show all saved sites as markers
- highlight the active site
- clicking a marker selects that site in the Observation Center UI

Testable outcome:

- a saved site is visible on the map
- changing active site updates selected marker state

### OCM2 — Geocoding-backed site creation

Goal:

Make site creation easier than manual coordinate entry.

Deliverables:

- local API endpoint for site search, e.g. `GET /api/site-search?q=...`
- backend integration with Open-Meteo Geocoding
- Observation Center search UI
- create new site from search result
- still allow manual coordinate entry/editing

Testable outcome:

- entering a place name returns location suggestions
- a suggestion can be saved as a TSN DSS site

### OCM3 — Weather snapshot for active site

Goal:

Attach first real conditions data to a selected site.

Deliverables:

- local API endpoint for forecast, e.g. `GET /api/site-forecast?site_id=...`
- backend integration with Open-Meteo Forecast API
- normalized weather payload for frontend
- Observation Center weather panel for the active site

Suggested first fields:

- temperature
- cloud cover
- wind speed
- humidity
- precipitation probability
- forecast timestamp

Testable outcome:

- selecting a site loads a weather snapshot for that site
- UI handles missing provider data gracefully

### OCM4 — Telescope context overlay

Goal:

Visually connect site state and telescope state.

Deliverables:

- show current telescope-linked site on Observation Center map
- show telescope status summary near map
- optional map indicator for site-relative telescope context
- keep this independent from whether data comes from simulator or future hardware adapter

Testable outcome:

- updating telescope/site state is reflected in Observation Center without changing frontend contracts

### OCM5 — Service hardening

Goal:

Make the provider integration stable enough for everyday local use.

Deliverables:

- response caching for geocoding and forecast
- provider error handling and friendly fallback messages
- attribution and provider notes in UI
- config surface for provider tuning

Testable outcome:

- repeated queries do not spam upstream providers
- provider failures do not break the Observation Center view

## Data/API additions

Recommended local endpoints:

- `GET /api/sites`
- `POST /api/sites`
- `POST /api/sites/{id}`
- `DELETE /api/sites/{id}`
- `POST /api/sites/active`
- `GET /api/site-search?q=...`
- `GET /api/site-forecast?site_id=...`

Potential normalized DTOs:

### SiteSearchResult

- `name`
- `country`
- `admin1`
- `latitude`
- `longitude`
- `elevation_m` optional
- `timezone` optional

### SiteForecastSnapshot

- `site_id`
- `generated_at`
- `current`
- `hourly`
- `provider`

## UX direction

Observation Center should be split into two main tabs over one consistent layout:

- `Sites`
- `Conditions`

Suggested flow:

1. choose or create site
2. inspect location on map
3. review current/near-term conditions
4. mark site as active
5. use the same active site across Sky and future capture workflows

## Explicitly out of scope for this slice

Not yet:

- live Seestar connection
- INDI / ASCOM integration
- automatic GPS sync from hardware
- astronomical darkness calculations
- Moon phase / Moon separation logic
- horizon mask import
- weather history storage
- push notifications

## Recommended implementation order

1. OCM1 — map foundation
2. OCM2 — geocoding search
3. OCM3 — weather snapshot
4. OCM5 — service hardening
5. OCM4 — telescope context overlay polish

Reasoning:

- map plus sites gives immediate user value
- geocoding removes friction from data entry
- weather becomes useful once sites are easy to manage
- hardening should happen before broader daily use
- telescope overlay can then reuse stable site/map infrastructure

## Definition of done for this planning thread

We can consider this plan ready for implementation when:

- Observation Center owns site management
- map provider choice is fixed for the slice
- weather/geocoding go through local TSN DSS API
- telescope adapters remain hardware agnostic
- the first implementation milestone can start without revisiting core architecture
