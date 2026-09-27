from __future__ import annotations

from ...domain.models import Site
from ...engine.sqlite.planning import PlanningRepository


def resolve_site(planning: PlanningRepository, site_identifier: str) -> Site:
    """Resolve a Site by stable ID, then by exact case-insensitive name. No fuzzy matching."""
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
