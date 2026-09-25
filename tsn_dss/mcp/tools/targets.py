from __future__ import annotations

import sqlite3
from typing import Any

from ...engine.sqlite.planning import PlanningRepository
from ..serializers import target_to_dict

MAX_TARGET_LIMIT = 100


def normalize_limit(limit: int) -> int:
    value = int(limit)
    if value < 1:
        raise ValueError("limit must be >= 1.")
    return min(value, MAX_TARGET_LIMIT)


def search_targets(
    connection: sqlite3.Connection,
    *,
    query: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Search the local TSN DSS target catalog only; no external catalog lookup."""
    repository = PlanningRepository(connection)
    bounded_limit = normalize_limit(limit)
    normalized_query = str(query).strip() if query is not None else ""
    if normalized_query:
        target = repository.find_target_by_query(normalized_query)
        targets = [] if target is None else [target]
    else:
        targets = repository.list_targets()[:bounded_limit]
    return {
        "query": normalized_query or None,
        "limit": bounded_limit,
        "catalog_scope": "local_tsn_dss_database",
        "targets": [target_to_dict(target) for target in targets[:bounded_limit]],
    }
