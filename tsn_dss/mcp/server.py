from __future__ import annotations

"""TSN DSS read-only MCP server over stdio."""

import argparse
import sys
from pathlib import Path
from typing import Any

from .bootstrap import McpBootstrapError, readonly_database
from .tools.catalog import resolve_catalog_object as resolve_catalog_object_impl
from .tools.catalog import search_catalog as search_catalog_impl
from .tools.sites import get_site as get_site_impl
from .tools.sites import get_sites as get_sites_impl
from .tools.targets import search_targets as search_targets_impl
from .tools.visibility import target_visibility_at as target_visibility_at_impl

try:
    from mcp.server import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ModuleNotFoundError as error:  # pragma: no cover - exercised only when dependency is missing
    MCPServer = None  # type: ignore[assignment]
    ToolError = RuntimeError  # type: ignore[assignment,misc]
    _IMPORT_ERROR = error
else:
    _IMPORT_ERROR = None


def create_mcp_server(
    *,
    projects_root: str | Path = "projects",
    database_path: str | Path | None = None,
) -> Any:
    if MCPServer is None:
        raise RuntimeError("The MCP Python SDK is not installed. Install dependencies from requirements.txt.") from _IMPORT_ERROR

    mcp = MCPServer("TSN DSS")

    @mcp.tool()
    def get_sites() -> dict[str, Any]:
        """List persisted TSN DSS observing Sites. Read-only."""
        return _call_tool(
            projects_root=projects_root,
            database_path=database_path,
            callback=get_sites_impl,
        )

    @mcp.tool()
    def get_site(site_id: str) -> dict[str, Any]:
        """Return one persisted TSN DSS observing Site, including Local Horizon profile."""
        return _call_tool(
            projects_root=projects_root,
            database_path=database_path,
            callback=lambda connection: get_site_impl(connection, site_id),
        )

    @mcp.tool()
    def search_targets(query: str | None = None, limit: int = 20) -> dict[str, Any]:
        """Search the local TSN DSS target catalog. No external catalog lookup is performed."""
        return _call_tool(
            projects_root=projects_root,
            database_path=database_path,
            callback=lambda connection: search_targets_impl(connection, query=query, limit=limit),
        )

    @mcp.tool()
    def search_catalog(
        query: str | None = None,
        object_type: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Search registered canonical astronomical CatalogObjects. Read-only and exact/no-fuzzy."""
        return _call_tool(
            projects_root=projects_root,
            database_path=database_path,
            callback=lambda connection: search_catalog_impl(
                connection,
                query=query,
                object_type=object_type,
                limit=limit,
            ),
        )

    @mcp.tool()
    def resolve_catalog_object(query: str) -> dict[str, Any]:
        """Resolve one registered canonical astronomical CatalogObject by exact alias."""
        return _call_tool(
            projects_root=projects_root,
            database_path=database_path,
            callback=lambda connection: resolve_catalog_object_impl(connection, query),
        )

    @mcp.tool()
    def target_visibility_at(
        site_id: str,
        time_utc: str,
        target_id: str | None = None,
        target_query: str | None = None,
        target_ra_deg: float | None = None,
        target_dec_deg: float | None = None,
        min_target_altitude_deg: float = 30.0,
    ) -> dict[str, Any]:
        """Evaluate deterministic TSN DSS target visibility at a UTC instant.

        The `visible` boolean is computed by TSN DSS, not by an LLM. It means the target is
        above the geometric horizon, above the minimum altitude, and not blocked by a configured
        Local Horizon. It does not include weather and does not mean astrophotography conditions
        are good.
        """
        return _call_tool(
            projects_root=projects_root,
            database_path=database_path,
            callback=lambda connection: target_visibility_at_impl(
                connection,
                site_id=site_id,
                target_id=target_id,
                target_query=target_query,
                target_ra_deg=target_ra_deg,
                target_dec_deg=target_dec_deg,
                time_utc=time_utc,
                min_target_altitude_deg=min_target_altitude_deg,
            ),
        )

    return mcp


def _call_tool(*, projects_root: str | Path, database_path: str | Path | None, callback: Any) -> Any:
    try:
        with readonly_database(projects_root=projects_root, database_path=database_path) as connection:
            return callback(connection)
    except (McpBootstrapError, ValueError) as error:
        raise ToolError(str(error)) from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the TSN DSS read-only MCP server over stdio.")
    parser.add_argument("--projects-root", default="projects")
    parser.add_argument("--database-path", default=None)
    args = parser.parse_args(argv)

    try:
        server = create_mcp_server(projects_root=args.projects_root, database_path=args.database_path)
    except Exception as error:
        print(f"TSN DSS MCP startup failed: {error}", file=sys.stderr)
        return 1
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
