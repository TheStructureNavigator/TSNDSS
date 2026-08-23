from __future__ import annotations

import json
import sqlite3

from ...domain.models import AcquisitionPlan, AcquisitionSequence, Equipment, Site, Target
from .db import transaction

ALLOWED_EQUIPMENT_TYPES = {
    "mount",
    "telescope",
    "camera",
    "guide_scope",
    "guide_camera",
    "focuser",
    "filter_wheel",
    "filter",
    "reducer_flattener",
    "barlow",
    "power",
    "computer",
    "other",
}

ALLOWED_PLAN_STATUSES = {"draft", "ready", "archived"}
ALLOWED_FRAME_TYPES = {"light", "dark", "flat", "bias", "dark_flat"}


class ValidationError(ValueError):
    pass


class PlanningRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def create_target(self, target: Target) -> Target:
        _validate_target(target)
        with transaction(self.connection):
            self.connection.execute(
                """
                INSERT INTO targets (
                    id,
                    catalog,
                    catalog_id,
                    name,
                    object_type,
                    ra_deg,
                    dec_deg,
                    angular_major_arcmin,
                    angular_minor_arcmin,
                    distance_ly,
                    constellation,
                    notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    target.id,
                    target.catalog,
                    target.catalog_id,
                    target.name,
                    target.object_type,
                    target.ra_deg,
                    target.dec_deg,
                    target.angular_major_arcmin,
                    target.angular_minor_arcmin,
                    target.distance_ly,
                    target.constellation,
                    target.notes,
                ),
            )
        return self.get_target(target.id)

    def get_target(self, target_id: str) -> Target | None:
        row = self.connection.execute(
            """
            SELECT
                id,
                catalog,
                catalog_id,
                name,
                ra_deg,
                dec_deg,
                object_type,
                angular_major_arcmin,
                angular_minor_arcmin,
                distance_ly,
                constellation,
                notes
            FROM targets
            WHERE id = ?;
            """,
            (target_id,),
        ).fetchone()
        return _row_to_target(row) if row else None

    def list_targets(self) -> list[Target]:
        rows = self.connection.execute(
            """
            SELECT
                id,
                catalog,
                catalog_id,
                name,
                ra_deg,
                dec_deg,
                object_type,
                angular_major_arcmin,
                angular_minor_arcmin,
                distance_ly,
                constellation,
                notes
            FROM targets
            ORDER BY catalog, catalog_id;
            """
        ).fetchall()
        return [_row_to_target(row) for row in rows]

    def update_target(self, target: Target) -> Target:
        _validate_target(target)
        with transaction(self.connection):
            cursor = self.connection.execute(
                """
                UPDATE targets
                SET
                    catalog = ?,
                    catalog_id = ?,
                    name = ?,
                    object_type = ?,
                    ra_deg = ?,
                    dec_deg = ?,
                    angular_major_arcmin = ?,
                    angular_minor_arcmin = ?,
                    distance_ly = ?,
                    constellation = ?,
                    notes = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?;
                """,
                (
                    target.catalog,
                    target.catalog_id,
                    target.name,
                    target.object_type,
                    target.ra_deg,
                    target.dec_deg,
                    target.angular_major_arcmin,
                    target.angular_minor_arcmin,
                    target.distance_ly,
                    target.constellation,
                    target.notes,
                    target.id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Target not found: {target.id}")
        return self.get_target(target.id)

    def delete_target(self, target_id: str) -> None:
        with transaction(self.connection):
            self.connection.execute("DELETE FROM targets WHERE id = ?;", (target_id,))

    def create_site(self, site: Site) -> Site:
        _validate_site(site)
        with transaction(self.connection):
            self.connection.execute(
                """
                INSERT INTO sites (
                    id,
                    name,
                    latitude_deg,
                    longitude_deg,
                    elevation_m,
                    sqm_mag_arcsec2,
                    bortle_class,
                    south_horizon_open,
                    notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    site.id,
                    site.name,
                    site.latitude_deg,
                    site.longitude_deg,
                    site.elevation_m,
                    site.sqm_mag_arcsec2,
                    site.bortle_class,
                    int(site.south_horizon_open),
                    site.notes,
                ),
            )
        return self.get_site(site.id)

    def get_site(self, site_id: str) -> Site | None:
        row = self.connection.execute(
            """
            SELECT
                id,
                name,
                latitude_deg,
                longitude_deg,
                elevation_m,
                sqm_mag_arcsec2,
                bortle_class,
                south_horizon_open,
                notes
            FROM sites
            WHERE id = ?;
            """,
            (site_id,),
        ).fetchone()
        return _row_to_site(row) if row else None

    def update_site(self, site: Site) -> Site:
        _validate_site(site)
        with transaction(self.connection):
            cursor = self.connection.execute(
                """
                UPDATE sites
                SET
                    name = ?,
                    latitude_deg = ?,
                    longitude_deg = ?,
                    elevation_m = ?,
                    sqm_mag_arcsec2 = ?,
                    bortle_class = ?,
                    south_horizon_open = ?,
                    notes = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?;
                """,
                (
                    site.name,
                    site.latitude_deg,
                    site.longitude_deg,
                    site.elevation_m,
                    site.sqm_mag_arcsec2,
                    site.bortle_class,
                    int(site.south_horizon_open),
                    site.notes,
                    site.id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Site not found: {site.id}")
        return self.get_site(site.id)

    def delete_site(self, site_id: str) -> None:
        with transaction(self.connection):
            self.connection.execute("DELETE FROM sites WHERE id = ?;", (site_id,))

    def create_equipment(self, equipment: Equipment) -> Equipment:
        _validate_equipment(equipment)
        with transaction(self.connection):
            self.connection.execute(
                """
                INSERT INTO equipment (
                    id,
                    equipment_type,
                    manufacturer,
                    model,
                    serial_number,
                    properties_json,
                    active,
                    notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    equipment.id,
                    equipment.equipment_type,
                    equipment.manufacturer,
                    equipment.model,
                    equipment.serial_number,
                    json.dumps(equipment.properties, sort_keys=True),
                    int(equipment.active),
                    equipment.notes,
                ),
            )
        return self.get_equipment(equipment.id)

    def get_equipment(self, equipment_id: str) -> Equipment | None:
        row = self.connection.execute(
            """
            SELECT
                id,
                equipment_type,
                manufacturer,
                model,
                serial_number,
                properties_json,
                active,
                notes
            FROM equipment
            WHERE id = ?;
            """,
            (equipment_id,),
        ).fetchone()
        return _row_to_equipment(row) if row else None

    def update_equipment(self, equipment: Equipment) -> Equipment:
        _validate_equipment(equipment)
        with transaction(self.connection):
            cursor = self.connection.execute(
                """
                UPDATE equipment
                SET
                    equipment_type = ?,
                    manufacturer = ?,
                    model = ?,
                    serial_number = ?,
                    properties_json = ?,
                    active = ?,
                    notes = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?;
                """,
                (
                    equipment.equipment_type,
                    equipment.manufacturer,
                    equipment.model,
                    equipment.serial_number,
                    json.dumps(equipment.properties, sort_keys=True),
                    int(equipment.active),
                    equipment.notes,
                    equipment.id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Equipment not found: {equipment.id}")
        return self.get_equipment(equipment.id)

    def delete_equipment(self, equipment_id: str) -> None:
        with transaction(self.connection):
            self.connection.execute("DELETE FROM equipment WHERE id = ?;", (equipment_id,))

    def save_acquisition_plan(self, plan: AcquisitionPlan) -> AcquisitionPlan:
        _validate_plan(plan)
        with transaction(self.connection):
            exists = self.connection.execute(
                "SELECT 1 FROM acquisition_plans WHERE id = ?;",
                (plan.id,),
            ).fetchone()

            if exists:
                self.connection.execute(
                    """
                    UPDATE acquisition_plans
                    SET
                        target_id = ?,
                        name = ?,
                        description = ?,
                        status = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?;
                    """,
                    (plan.target_id, plan.name, plan.description, plan.status, plan.id),
                )
                self.connection.execute(
                    "DELETE FROM acquisition_sequences WHERE plan_id = ?;",
                    (plan.id,),
                )
            else:
                self.connection.execute(
                    """
                    INSERT INTO acquisition_plans (
                        id,
                        target_id,
                        name,
                        description,
                        status
                    ) VALUES (?, ?, ?, ?, ?);
                    """,
                    (plan.id, plan.target_id, plan.name, plan.description, plan.status),
                )

            saved_sequences: list[AcquisitionSequence] = []
            for sequence in sorted(plan.sequences, key=lambda item: item.sequence_order):
                _validate_sequence(sequence)
                cursor = self.connection.execute(
                    """
                    INSERT INTO acquisition_sequences (
                        plan_id,
                        sequence_order,
                        name,
                        frame_type,
                        exposure_s,
                        frame_count,
                        iso,
                        gain,
                        offset_value,
                        binning_x,
                        binning_y,
                        filter_id,
                        notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        plan.id,
                        sequence.sequence_order,
                        sequence.name,
                        sequence.frame_type,
                        sequence.exposure_s,
                        sequence.frame_count,
                        sequence.iso,
                        sequence.gain,
                        sequence.offset_value,
                        sequence.binning_x,
                        sequence.binning_y,
                        sequence.filter_id,
                        sequence.notes,
                    ),
                )
                saved_sequences.append(
                    AcquisitionSequence(
                        id=int(cursor.lastrowid),
                        sequence_order=sequence.sequence_order,
                        name=sequence.name,
                        frame_type=sequence.frame_type,
                        exposure_s=sequence.exposure_s,
                        frame_count=sequence.frame_count,
                        iso=sequence.iso,
                        gain=sequence.gain,
                        offset_value=sequence.offset_value,
                        binning_x=sequence.binning_x,
                        binning_y=sequence.binning_y,
                        filter_id=sequence.filter_id,
                        notes=sequence.notes,
                    )
                )

        return self.get_acquisition_plan(plan.id)

    def get_acquisition_plan(self, plan_id: str) -> AcquisitionPlan | None:
        plan_row = self.connection.execute(
            """
            SELECT
                id,
                target_id,
                name,
                description,
                status
            FROM acquisition_plans
            WHERE id = ?;
            """,
            (plan_id,),
        ).fetchone()
        if not plan_row:
            return None

        sequence_rows = self.connection.execute(
            """
            SELECT
                id,
                sequence_order,
                name,
                frame_type,
                exposure_s,
                frame_count,
                iso,
                gain,
                offset_value,
                binning_x,
                binning_y,
                filter_id,
                notes
            FROM acquisition_sequences
            WHERE plan_id = ?
            ORDER BY sequence_order;
            """,
            (plan_id,),
        ).fetchall()

        return AcquisitionPlan(
            id=plan_row["id"],
            target_id=plan_row["target_id"],
            name=plan_row["name"],
            description=plan_row["description"],
            status=plan_row["status"],
            sequences=[_row_to_sequence(row) for row in sequence_rows],
        )

    def list_acquisition_plans(self, *, target_id: str | None = None) -> list[AcquisitionPlan]:
        if target_id is None:
            plan_rows = self.connection.execute(
                """
                SELECT id
                FROM acquisition_plans
                ORDER BY id;
                """
            ).fetchall()
        else:
            plan_rows = self.connection.execute(
                """
                SELECT id
                FROM acquisition_plans
                WHERE target_id = ?
                ORDER BY id;
                """,
                (target_id,),
            ).fetchall()

        return [self.get_acquisition_plan(row["id"]) for row in plan_rows]

    def delete_acquisition_plan(self, plan_id: str) -> None:
        with transaction(self.connection):
            self.connection.execute("DELETE FROM acquisition_plans WHERE id = ?;", (plan_id,))


def _validate_target(target: Target) -> None:
    if not target.id:
        raise ValidationError("Target id is required.")
    if not target.catalog or not target.catalog_id or not target.name:
        raise ValidationError("Target catalog, catalog_id and name are required.")
    if not 0 <= target.ra_deg < 360:
        raise ValidationError("Target ra_deg must be in [0, 360).")
    if not -90 <= target.dec_deg <= 90:
        raise ValidationError("Target dec_deg must be in [-90, 90].")


def _validate_site(site: Site) -> None:
    if not site.id or not site.name:
        raise ValidationError("Site id and name are required.")
    if site.latitude_deg is not None and not -90 <= site.latitude_deg <= 90:
        raise ValidationError("Site latitude_deg must be in [-90, 90].")
    if site.longitude_deg is not None and not -180 <= site.longitude_deg <= 180:
        raise ValidationError("Site longitude_deg must be in [-180, 180].")
    if site.bortle_class is not None and not 1 <= site.bortle_class <= 9:
        raise ValidationError("Site bortle_class must be in [1, 9].")


def _validate_equipment(equipment: Equipment) -> None:
    if not equipment.id:
        raise ValidationError("Equipment id is required.")
    if equipment.equipment_type not in ALLOWED_EQUIPMENT_TYPES:
        raise ValidationError(f"Unsupported equipment type: {equipment.equipment_type}")
    if not isinstance(equipment.properties, dict):
        raise ValidationError("Equipment properties must be a dictionary.")


def _validate_plan(plan: AcquisitionPlan) -> None:
    if not plan.id or not plan.target_id or not plan.name:
        raise ValidationError("Plan id, target_id and name are required.")
    if plan.status not in ALLOWED_PLAN_STATUSES:
        raise ValidationError(f"Unsupported plan status: {plan.status}")

    seen_orders: set[int] = set()
    for sequence in plan.sequences:
        _validate_sequence(sequence)
        if sequence.sequence_order in seen_orders:
            raise ValidationError(
                f"Duplicate sequence_order in acquisition plan: {sequence.sequence_order}"
            )
        seen_orders.add(sequence.sequence_order)


def _validate_sequence(sequence: AcquisitionSequence) -> None:
    if sequence.frame_type not in ALLOWED_FRAME_TYPES:
        raise ValidationError(f"Unsupported frame type: {sequence.frame_type}")
    if sequence.sequence_order < 0:
        raise ValidationError("Sequence order must be >= 0.")
    if sequence.frame_count <= 0:
        raise ValidationError("Sequence frame_count must be > 0.")
    if sequence.exposure_s is not None and sequence.exposure_s <= 0:
        raise ValidationError("Sequence exposure_s must be > 0 when provided.")
    if sequence.iso is not None and sequence.iso <= 0:
        raise ValidationError("Sequence iso must be > 0 when provided.")
    if sequence.binning_x <= 0 or sequence.binning_y <= 0:
        raise ValidationError("Sequence binning values must be > 0.")


def _row_to_target(row: sqlite3.Row) -> Target:
    return Target(
        id=row["id"],
        catalog=row["catalog"],
        catalog_id=row["catalog_id"],
        name=row["name"],
        ra_deg=row["ra_deg"],
        dec_deg=row["dec_deg"],
        object_type=row["object_type"],
        angular_major_arcmin=row["angular_major_arcmin"],
        angular_minor_arcmin=row["angular_minor_arcmin"],
        distance_ly=row["distance_ly"],
        constellation=row["constellation"],
        notes=row["notes"],
    )


def _row_to_site(row: sqlite3.Row) -> Site:
    return Site(
        id=row["id"],
        name=row["name"],
        latitude_deg=row["latitude_deg"],
        longitude_deg=row["longitude_deg"],
        elevation_m=row["elevation_m"],
        sqm_mag_arcsec2=row["sqm_mag_arcsec2"],
        bortle_class=row["bortle_class"],
        south_horizon_open=bool(row["south_horizon_open"]),
        notes=row["notes"],
    )


def _row_to_equipment(row: sqlite3.Row) -> Equipment:
    return Equipment(
        id=row["id"],
        equipment_type=row["equipment_type"],
        manufacturer=row["manufacturer"],
        model=row["model"],
        serial_number=row["serial_number"],
        properties=json.loads(row["properties_json"]),
        active=bool(row["active"]),
        notes=row["notes"],
    )


def _row_to_sequence(row: sqlite3.Row) -> AcquisitionSequence:
    return AcquisitionSequence(
        id=row["id"],
        sequence_order=row["sequence_order"],
        name=row["name"],
        frame_type=row["frame_type"],
        exposure_s=row["exposure_s"],
        frame_count=row["frame_count"],
        iso=row["iso"],
        gain=row["gain"],
        offset_value=row["offset_value"],
        binning_x=row["binning_x"],
        binning_y=row["binning_y"],
        filter_id=row["filter_id"],
        notes=row["notes"],
    )
