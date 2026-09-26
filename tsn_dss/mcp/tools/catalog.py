from __future__ import annotations

import sqlite3
from typing import Any

from ...engine.catalog_service import CatalogService, normalize_catalog_limit
from ..serializers import catalog_object_to_dict, catalog_resolution_to_dict


def search_catalog(
    connection: sqlite3.Connection,
    *,
    query: str | None = None,
    object_type: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Search registered canonical CatalogObjects only; no external lookup."""
    bounded_limit = normalize_catalog_limit(limit)
    normalized_query = str(query).strip() if query is not None and str(query).strip() else None
    normalized_type = str(object_type).strip() if object_type is not None and str(object_type).strip() else None
    objects = CatalogService(connection).search_catalog(
        query=normalized_query,
        object_type=normalized_type,
        limit=bounded_limit,
    )
    return {
        "query": normalized_query,
        "object_type": normalized_type,
        "limit": bounded_limit,
        "catalog_scope": "canonical_tsn_dss_catalog_objects",
        "objects": [catalog_object_to_dict(catalog_object) for catalog_object in objects],
    }


def resolve_catalog_object(connection: sqlite3.Connection, query: str) -> dict[str, Any]:
    """Resolve one registered canonical CatalogObject by deterministic exact alias."""
    normalized_query = str(query).strip()
    result = CatalogService(connection).resolve_catalog_object(normalized_query)
    return {
        "query": normalized_query,
        "catalog_scope": "canonical_tsn_dss_catalog_objects",
        "resolution": catalog_resolution_to_dict(result),
    }
