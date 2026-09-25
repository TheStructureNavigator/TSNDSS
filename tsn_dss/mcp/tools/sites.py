from __future__ import annotations

import sqlite3
from typing import Any

from ...engine.sqlite.planning import PlanningRepository
from ..serializers import site_detail_to_dict, site_summary_to_dict


def get_sites(connection: sqlite3.Connection) -> dict[str, Any]:
    """Return concise summaries of persisted TSN DSS observing Sites."""
    sites = PlanningRepository(connection).list_sites()
    return {"sites": [site_summary_to_dict(site) for site in sites]}


def get_site(connection: sqlite3.Connection, site_id: str) -> dict[str, Any]:
    """Return details for one persisted TSN DSS observing Site."""
    site = PlanningRepository(connection).get_site(str(site_id).strip())
    if site is None:
        raise ValueError(f"Unknown site_id: {site_id}")
    return {"site": site_detail_to_dict(site)}
