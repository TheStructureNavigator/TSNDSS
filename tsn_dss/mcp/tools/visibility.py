from __future__ import annotations

import sqlite3
from typing import Any

from ...domain.models import Site
from ...engine.astronomy import AstronomicalTargetContext
from ...engine.catalog_service import CatalogService
from ...engine.sqlite.planning import PlanningRepository
from ...engine.target_resolution import TargetResolutionRequest, resolve_astronomical_target_context
from ...engine.visibility import TargetVisibilityService, visibility_windows
from ..serializers import site_summary_to_dict


def target_visibility_at(
    connection: sqlite3.Connection,
    *,
    site_id: str,
    time_utc: str,
    target_id: str | None = None,
    target_query: str | None = None,
    target_ra_deg: float | None = None,
    target_dec_deg: float | None = None,
    min_target_altitude_deg: float = 30.0,
) -> dict[str, Any]:
    """Return deterministic TSN DSS target visibility at one UTC instant."""
    return {
        "visibility": TargetVisibilityService(connection).target_visibility_at(
            site_id=site_id,
            target_id=target_id,
            target_query=target_query,
            target_ra_deg=target_ra_deg,
            target_dec_deg=target_dec_deg,
            time_utc=time_utc,
            min_target_altitude_deg=float(min_target_altitude_deg),
        )
    }


def target_visibility_windows(
    connection: sqlite3.Connection,
    *,
    site_id: str,
    start_time_utc: str,
    end_time_utc: str,
    target_id: str | None = None,
    target_query: str | None = None,
    catalog_query: str | None = None,
    target_ra_deg: float | None = None,
    target_dec_deg: float | None = None,
    min_target_altitude_deg: float = 30.0,
) -> dict[str, Any]:
    """Return deterministic TSN DSS target visibility windows over a UTC interval."""
    planning = PlanningRepository(connection)
    site = _resolve_site(planning, site_id)
    target = _resolve_window_target(
        connection,
        planning,
        target_id=target_id,
        target_query=target_query,
        catalog_query=catalog_query,
        target_ra_deg=target_ra_deg,
        target_dec_deg=target_dec_deg,
    )
    if target is None:
        raise ValueError("Provide target_id, target_query, catalog_query, or explicit target coordinates.")

    result = visibility_windows(
        site,
        target,
        start_time_utc=start_time_utc,
        end_time_utc=end_time_utc,
        min_target_altitude_deg=float(min_target_altitude_deg),
    )
    payload = result.to_dict()
    payload.update(
        {
            "site": site_summary_to_dict(site),
            "target": target.to_dict(),
            "visible_rule": (
                "above_geometric_horizon AND above_minimum_altitude AND "
                "(no Local Horizon profile OR above_local_horizon)"
            ),
            "excluded_constraints": ["weather", "cloud_cover", "twilight", "moon", "ranking"],
            "diagnostic_semantics": "evaluation_grid_derived_not_continuous_proof",
        }
    )
    return {"visibility_windows": payload}


def _resolve_site(planning: PlanningRepository, site_identifier: str) -> Site:
    normalized = str(site_identifier).strip()
    if not normalized:
        raise ValueError("site_id is required.")

    site = planning.get_site(normalized)
    if site is not None:
        return site

    exact_name_matches = [item for item in planning.list_sites() if item.name.casefold() == normalized.casefold()]
    if len(exact_name_matches) == 1:
        return exact_name_matches[0]
    if len(exact_name_matches) > 1:
        raise ValueError(f"Site name is ambiguous: {site_identifier}")
    raise ValueError(f"Unknown site_id: {site_identifier}")


def _resolve_window_target(
    connection: sqlite3.Connection,
    planning: PlanningRepository,
    *,
    target_id: str | None,
    target_query: str | None,
    catalog_query: str | None,
    target_ra_deg: float | None,
    target_dec_deg: float | None,
) -> AstronomicalTargetContext | None:
    has_coordinates = target_ra_deg is not None or target_dec_deg is not None
    has_target = _has_text(target_id) or _has_text(target_query)
    has_catalog = _has_text(catalog_query)
    source_count = sum([has_coordinates, has_target, has_catalog])
    if source_count > 1:
        raise ValueError("Provide only one target source: target_id/target_query, catalog_query, or explicit coordinates.")

    if has_catalog:
        return _resolve_catalog_target(connection, str(catalog_query).strip())

    return resolve_astronomical_target_context(
        TargetResolutionRequest(
            target_id=target_id,
            target_name=target_query,
            target_ra_deg=target_ra_deg,
            target_dec_deg=target_dec_deg,
        ),
        planning_repository=planning,
    )


def _resolve_catalog_target(connection: sqlite3.Connection, query: str) -> AstronomicalTargetContext:
    resolution = CatalogService(connection).resolve_catalog_object(query)
    if resolution.status == "not_found":
        raise ValueError(f"CatalogObject not found for query: {query}")
    if resolution.status == "ambiguous":
        candidates = ", ".join(candidate.catalog_object.id for candidate in resolution.candidates)
        raise ValueError(f"CatalogObject query is ambiguous: {query} ({candidates})")
    if resolution.catalog_object is None:
        raise ValueError(f"CatalogObject not found for query: {query}")

    catalog_object = resolution.catalog_object.catalog_object
    return AstronomicalTargetContext(
        target_name=catalog_object.display_name,
        ra_deg=catalog_object.ra_deg,
        dec_deg=catalog_object.dec_deg,
        source_kind="catalog_object",
        source_id=catalog_object.id,
    )


def _has_text(value: str | None) -> bool:
    return value is not None and bool(str(value).strip())
