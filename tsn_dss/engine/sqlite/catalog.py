from __future__ import annotations

import sqlite3

from ...domain.models import CatalogObject, CatalogObjectAlias, CatalogResolutionResult
from ..catalog import (
    CatalogConflictError,
    CatalogRegistrationItem,
    CatalogRegistrationReport,
    normalize_catalog_alias,
)
from .db import transaction


class CatalogRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def get_catalog_object(self, object_id: str) -> CatalogObject | None:
        row = self.connection.execute(
            """
            SELECT
                id,
                canonical_designation,
                display_name,
                ra_deg,
                dec_deg,
                object_type,
                coordinate_frame,
                coordinate_epoch,
                angular_major_arcmin,
                angular_minor_arcmin,
                magnitude,
                source_provider,
                source_version,
                source_external_id,
                imported_at
            FROM catalog_objects
            WHERE id = ?;
            """,
            (object_id,),
        ).fetchone()
        return _row_to_catalog_object(row) if row else None

    def list_catalog_objects(self, *, query: str | None = None, limit: int = 50) -> list[CatalogObject]:
        bounded_limit = max(1, min(int(limit), 200))
        if query is None or not query.strip():
            rows = self.connection.execute(
                """
                SELECT
                    id, canonical_designation, display_name, ra_deg, dec_deg, object_type,
                    coordinate_frame, coordinate_epoch, angular_major_arcmin, angular_minor_arcmin,
                    magnitude, source_provider, source_version, source_external_id, imported_at
                FROM catalog_objects
                ORDER BY canonical_designation, id
                LIMIT ?;
                """,
                (bounded_limit,),
            ).fetchall()
        else:
            needle = f"%{query.strip().casefold()}%"
            rows = self.connection.execute(
                """
                SELECT DISTINCT
                    o.id, o.canonical_designation, o.display_name, o.ra_deg, o.dec_deg, o.object_type,
                    o.coordinate_frame, o.coordinate_epoch, o.angular_major_arcmin, o.angular_minor_arcmin,
                    o.magnitude, o.source_provider, o.source_version, o.source_external_id, o.imported_at
                FROM catalog_objects o
                LEFT JOIN catalog_object_aliases a ON a.catalog_object_id = o.id
                WHERE lower(o.canonical_designation) LIKE ?
                   OR lower(o.display_name) LIKE ?
                   OR a.normalized_alias LIKE ?
                ORDER BY o.canonical_designation, o.id
                LIMIT ?;
                """,
                (needle, needle, needle, bounded_limit),
            ).fetchall()
        return [_row_to_catalog_object(row) for row in rows]

    def resolve_alias(self, alias: str) -> CatalogResolutionResult:
        normalized = normalize_catalog_alias(alias)
        if not normalized:
            return CatalogResolutionResult(status="not_found")

        rows = self.connection.execute(
            """
            SELECT
                o.id, o.canonical_designation, o.display_name, o.ra_deg, o.dec_deg, o.object_type,
                o.coordinate_frame, o.coordinate_epoch, o.angular_major_arcmin, o.angular_minor_arcmin,
                o.magnitude, o.source_provider, o.source_version, o.source_external_id, o.imported_at
            FROM catalog_object_aliases a
            JOIN catalog_objects o ON o.id = a.catalog_object_id
            WHERE a.normalized_alias = ?
            ORDER BY o.id;
            """,
            (normalized,),
        ).fetchall()
        objects = [_row_to_catalog_object(row) for row in rows]
        if not objects:
            return CatalogResolutionResult(status="not_found")
        if len(objects) > 1:
            return CatalogResolutionResult(status="ambiguous", candidates=objects)
        return CatalogResolutionResult(status="resolved", catalog_object=objects[0], candidates=objects)

    def register_catalog_objects(self, items: list[CatalogRegistrationItem]) -> CatalogRegistrationReport:
        report = CatalogRegistrationReport()
        with transaction(self.connection):
            for item in items:
                self._register_one(item, report)
            if report.conflicts:
                raise CatalogConflictError("; ".join(report.conflicts))
        return report

    def _register_one(self, item: CatalogRegistrationItem, report: CatalogRegistrationReport) -> None:
        catalog_object = item.catalog_object
        _validate_catalog_object(catalog_object)
        existing = self.get_catalog_object(catalog_object.id)
        if existing is None:
            self.connection.execute(
                """
                INSERT INTO catalog_objects (
                    id, canonical_designation, display_name, ra_deg, dec_deg, object_type,
                    coordinate_frame, coordinate_epoch, angular_major_arcmin, angular_minor_arcmin,
                    magnitude, source_provider, source_version, source_external_id, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP));
                """,
                _catalog_object_values(catalog_object),
            )
            report.objects_created.append(catalog_object.id)
        elif _catalog_object_identity(existing) == _catalog_object_identity(catalog_object):
            report.objects_unchanged.append(catalog_object.id)
        else:
            report.conflicts.append(f"CatalogObject conflict for {catalog_object.id}")
            return

        aliases = item.aliases or [
            CatalogObjectAlias(
                catalog_object_id=catalog_object.id,
                alias=catalog_object.canonical_designation,
                normalized_alias=normalize_catalog_alias(catalog_object.canonical_designation),
                alias_kind="canonical_designation",
            )
        ]
        for alias in aliases:
            self._register_alias(catalog_object.id, alias, report)

    def _register_alias(
        self,
        catalog_object_id: str,
        alias: CatalogObjectAlias,
        report: CatalogRegistrationReport,
    ) -> None:
        normalized = normalize_catalog_alias(alias.alias)
        normalized = normalized or alias.normalized_alias
        provided = normalize_catalog_alias(alias.normalized_alias)
        if provided and provided != normalized:
            report.conflicts.append(f"Alias normalized form mismatch for {alias.alias!r}")
            return
        if not normalized:
            report.conflicts.append("CatalogObject alias cannot be blank")
            return
        existing = self.connection.execute(
            """
            SELECT catalog_object_id, alias, alias_kind
            FROM catalog_object_aliases
            WHERE normalized_alias = ?;
            """,
            (normalized,),
        ).fetchone()
        if existing is None:
            self.connection.execute(
                """
                INSERT INTO catalog_object_aliases (catalog_object_id, alias, normalized_alias, alias_kind)
                VALUES (?, ?, ?, ?);
                """,
                (catalog_object_id, alias.alias.strip(), normalized, alias.alias_kind),
            )
            report.aliases_created.append(normalized)
        elif existing["catalog_object_id"] == catalog_object_id:
            report.aliases_unchanged.append(normalized)
        else:
            report.conflicts.append(
                f"Alias {alias.alias!r} ({normalized}) already belongs to {existing['catalog_object_id']}"
            )


def _validate_catalog_object(catalog_object: CatalogObject) -> None:
    if not catalog_object.id:
        raise ValueError("CatalogObject id is required.")
    if not catalog_object.canonical_designation or not catalog_object.display_name:
        raise ValueError("CatalogObject canonical designation and display name are required.")
    if not 0 <= catalog_object.ra_deg < 360:
        raise ValueError("CatalogObject ra_deg must be in [0, 360).")
    if not -90 <= catalog_object.dec_deg <= 90:
        raise ValueError("CatalogObject dec_deg must be in [-90, 90].")
    if not catalog_object.object_type:
        raise ValueError("CatalogObject object_type is required.")
    if not catalog_object.coordinate_frame:
        raise ValueError("CatalogObject coordinate_frame is required.")
    if not catalog_object.source_provider or not catalog_object.source_version:
        raise ValueError("CatalogObject source provider and version are required.")


def _catalog_object_values(catalog_object: CatalogObject) -> tuple:
    return (
        catalog_object.id,
        catalog_object.canonical_designation,
        catalog_object.display_name,
        catalog_object.ra_deg,
        catalog_object.dec_deg,
        catalog_object.object_type,
        catalog_object.coordinate_frame,
        catalog_object.coordinate_epoch,
        catalog_object.angular_major_arcmin,
        catalog_object.angular_minor_arcmin,
        catalog_object.magnitude,
        catalog_object.source_provider,
        catalog_object.source_version,
        catalog_object.source_external_id,
        catalog_object.imported_at,
    )


def _catalog_object_identity(catalog_object: CatalogObject) -> tuple:
    return _catalog_object_values(catalog_object)[:-1]


def _row_to_catalog_object(row: sqlite3.Row) -> CatalogObject:
    return CatalogObject(
        id=row["id"],
        canonical_designation=row["canonical_designation"],
        display_name=row["display_name"],
        ra_deg=row["ra_deg"],
        dec_deg=row["dec_deg"],
        object_type=row["object_type"],
        coordinate_frame=row["coordinate_frame"],
        coordinate_epoch=row["coordinate_epoch"],
        angular_major_arcmin=row["angular_major_arcmin"],
        angular_minor_arcmin=row["angular_minor_arcmin"],
        magnitude=row["magnitude"],
        source_provider=row["source_provider"],
        source_version=row["source_version"],
        source_external_id=row["source_external_id"],
        imported_at=row["imported_at"],
    )
