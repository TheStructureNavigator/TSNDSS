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
- `search_targets(query=None, limit=20)` searches the local TSN DSS target catalog only.
- `target_visibility_at(site_id, time_utc, target_id=None, target_query=None, target_ra_deg=None, target_dec_deg=None, min_target_altitude_deg=30)` evaluates deterministic point-in-time visibility.

`target_visibility_at.visible` uses this rule:

```text
above_geometric_horizon
AND above_minimum_altitude
AND (no Local Horizon profile OR above_local_horizon)
```

Weather/cloud cover is not part of this boolean, and the result does not mean astrophotography conditions are good.

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

- Target search uses the local TSN DSS database only; there is no external catalog lookup.
- `target_visibility_at` does not include weather forecasts.
- No write tools or actions are exposed.
- No planning execution, instrument control, Seestar or ASCOM integration is exposed.
