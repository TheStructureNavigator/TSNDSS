from __future__ import annotations

import sqlite3
from typing import Any

from ...engine.visibility import TargetVisibilityService


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
