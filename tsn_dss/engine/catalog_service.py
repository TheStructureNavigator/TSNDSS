from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..domain.models import CatalogObject, CatalogObjectAlias, CatalogResolutionResult
from .sqlite.catalog import CatalogRepository

MAX_CATALOG_LIMIT = 100


@dataclass(frozen=True, slots=True)
class CatalogObjectWithAliases:
    catalog_object: CatalogObject
    aliases: tuple[CatalogObjectAlias, ...]


@dataclass(frozen=True, slots=True)
class CatalogResolutionWithAliases:
    status: str
    catalog_object: CatalogObjectWithAliases | None = None
    candidates: tuple[CatalogObjectWithAliases, ...] = ()


class CatalogService:
    """Read-side catalog service over canonical TSN DSS catalog state."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.repository = CatalogRepository(connection)

    def search_catalog(
        self,
        *,
        query: str | None = None,
        object_type: str | None = None,
        limit: int = 20,
    ) -> list[CatalogObject]:
        return self.repository.list_catalog_objects(
            query=_normalized_optional(query),
            object_type=_normalized_optional(object_type),
            limit=normalize_catalog_limit(limit),
        )

    def resolve_catalog_object(self, query: str) -> CatalogResolutionWithAliases:
        result = self.repository.resolve_alias(str(query).strip())
        if result.status == "resolved" and result.catalog_object is not None:
            return CatalogResolutionWithAliases(
                status="resolved",
                catalog_object=self._with_aliases(result.catalog_object),
                candidates=tuple(self._with_aliases(candidate) for candidate in result.candidates),
            )
        if result.status == "ambiguous":
            return CatalogResolutionWithAliases(
                status="ambiguous",
                candidates=tuple(self._with_aliases(candidate) for candidate in result.candidates),
            )
        return CatalogResolutionWithAliases(status="not_found")

    def _with_aliases(self, catalog_object: CatalogObject) -> CatalogObjectWithAliases:
        return CatalogObjectWithAliases(
            catalog_object=catalog_object,
            aliases=tuple(self.repository.list_aliases_for_object(catalog_object.id)),
        )


def normalize_catalog_limit(limit: int) -> int:
    value = int(limit)
    if value < 1:
        raise ValueError("limit must be >= 1.")
    return min(value, MAX_CATALOG_LIMIT)


def _normalized_optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None
