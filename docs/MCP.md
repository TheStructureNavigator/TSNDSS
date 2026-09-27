# TSN DSS MCP

TSN DSS exposes a small read-only Model Context Protocol adapter so external MCP-capable clients can query TSN DSS data over stdio.

The LLM is not part of TSN DSS. TSN DSS remains the source of truth for persisted data, astronomy calculations, Local Horizon evaluation and deterministic visibility decisions.

## Launch

Install the Python dependencies, then run:

```bash
python -m tsn_dss.mcp.server --projects-root projects
```

Or point directly at a database:

```bash
python -m tsn_dss.mcp.server --database-path projects/tsn_dss.db
```

The default database path is `<projects-root>/tsn_dss.db`, matching the local HTTP API convention. The database must already exist and must already be at the supported schema version. The MCP bootstrap opens SQLite in read-only mode; it does not initialize, migrate, register filesystem projects, create directories or write data.

The server uses stdio. Stdout is reserved for MCP protocol traffic; diagnostics go to stderr.

## Tools

- `get_sites()` lists persisted observing Sites.
- `get_site(site_id)` returns Site details, including Local Horizon points.
- `search_catalog(query=None, object_type=None, limit=20)` searches canonical astronomical `CatalogObject` rows already registered in the TSN DSS database.
- `resolve_catalog_object(query)` resolves one canonical astronomical `CatalogObject` by deterministic exact alias.
- `search_targets(query=None, limit=20)` searches the local TSN DSS target catalog only.
- `target_visibility_at(site_id, time_utc, target_id=None, target_query=None, target_ra_deg=None, target_dec_deg=None, min_target_altitude_deg=30)` evaluates deterministic point-in-time visibility.
- `target_visibility_windows(site_id, start_time_utc, end_time_utc, target_id=None, target_query=None, catalog_query=None, target_ra_deg=None, target_dec_deg=None, min_target_altitude_deg=30)` evaluates deterministic visibility windows over a UTC interval.

`CatalogObject` and `Target` are intentionally different concepts:

- `CatalogObject` is an astronomical catalog fact, such as a registered OpenNGC object.
- `Target` is a user/workflow observing target stored in TSN DSS planning data.

Use `search_catalog` for astronomical catalog discovery. Use `search_targets` for the user's workflow Targets.

Catalog MCP tools read only canonical database state. They do not parse bundled catalog files, download data, register OpenNGC, initialize a database, migrate a database or create Targets. If no `CatalogObject` rows have been explicitly registered beforehand, `search_catalog` returns an empty result and `resolve_catalog_object` returns `not_found`.

Catalog resolution is exact and deterministic after TSN DSS alias normalization. There is no fuzzy matching, no online lookup and no model-knowledge fallback. Ambiguous aliases are not silently assigned to an object; the MCP result reflects the canonical resolver state.

`target_visibility_at.visible` uses this rule:

```text
above_geometric_horizon
AND above_minimum_altitude
AND (no Local Horizon profile OR above_local_horizon)
```

Weather/cloud cover is not part of this boolean, and the result does not mean astrophotography conditions are good.

`target_visibility_windows` uses the same deterministic visibility predicate over a closed UTC interval and returns zero or more clipped windows. It can use exactly one target source: a persisted workflow `Target`, a resolved canonical `CatalogObject` through `catalog_query`, or explicit RA/Dec coordinates. Window diagnostics are evaluation-grid-derived observations, not continuous proof that an opposite condition never occurred between evaluated instants. Weather, twilight, Moon constraints and ranking are excluded.

## Example Client Configuration

Provider-neutral MCP clients generally need a command and args:

```json
{
  "command": "python",
  "args": [
    "-m",
    "tsn_dss.mcp.server",
    "--projects-root",
    "C:/Users/treze/OneDrive/Desktop/TSN_DSS/projects"
  ]
}
```

## Current Limitations

- Catalog and Target search use the local TSN DSS database only; there is no external catalog lookup.
- `target_visibility_at` does not include weather forecasts.
- `target_visibility_windows` does not include weather forecasts.
- No write tools or actions are exposed.
- No planning execution, instrument control, Seestar or ASCOM integration is exposed.
