from __future__ import annotations

import json
import sqlite3

from ...domain.local_horizon import normalize_local_horizon_profile
from ...domain.models import AcquisitionPlan, AcquisitionSequence, Equipment, LocalHorizonPoint, Site, Target
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

    def find_target_by_query(self, query: str) -> Target | None:
        normalized_query = str(query).strip()
        if not normalized_query:
            return None

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
            WHERE lower(name) = lower(?)
               OR lower(catalog_id) = lower(?)
               OR lower(catalog || ' ' || catalog_id) = lower(?)
            ORDER BY
                CASE
                    WHEN lower(name) = lower(?) THEN 0
                    WHEN lower(catalog_id) = lower(?) THEN 1
                    ELSE 2
                END,
                name,
                id
            LIMIT 1;
            """,
            (
                normalized_query,
                normalized_query,
                normalized_query,
                normalized_query,
                normalized_query,
            ),
        ).fetchone()
        return _row_to_target(row) if row else None

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
                    lp_artificial_brightness_mcd_m2,
                    lp_natural_sky_ratio,
                    lp_estimated_total_brightness_mcd_m2,
                    lp_estimated_sqm_mag_arcsec2,
                    lp_estimated_bortle_class,
                    lp_dataset_name,
                    lp_provider_name,
                    lp_source,
                    lp_source_unit,
                    lp_data_kind,
                    lp_updated_at,
                    south_horizon_open,
                    notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    site.id,
                    site.name,
                    site.latitude_deg,
                    site.longitude_deg,
                    site.elevation_m,
                    site.sqm_mag_arcsec2,
                    site.bortle_class,
                    site.lp_artificial_brightness_mcd_m2,
                    site.lp_natural_sky_ratio,
                    site.lp_estimated_total_brightness_mcd_m2,
                    site.lp_estimated_sqm_mag_arcsec2,
                    site.lp_estimated_bortle_class,
                    site.lp_dataset_name,
                    site.lp_provider_name,
                    site.lp_source,
                    site.lp_source_unit,
                    site.lp_data_kind,
                    site.lp_updated_at,
                    int(site.south_horizon_open),
                    site.notes,
                ),
            )
            _replace_site_horizon_profile(self.connection, site.id, site.horizon_profile)
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
                lp_artificial_brightness_mcd_m2,
                lp_natural_sky_ratio,
                lp_estimated_total_brightness_mcd_m2,
                lp_estimated_sqm_mag_arcsec2,
                lp_estimated_bortle_class,
                lp_dataset_name,
                lp_provider_name,
                lp_source,
                lp_source_unit,
                lp_data_kind,
                lp_updated_at,
                south_horizon_open,
                notes
            FROM sites
            WHERE id = ?;
            """,
            (site_id,),
        ).fetchone()
        if not row:
            return None
        site = _row_to_site(row)
        site.horizon_profile = _load_site_horizon_profile(self.connection, site.id)
        return site

    def list_sites(self) -> list[Site]:
        rows = self.connection.execute(
            """
            SELECT
                id,
                name,
                latitude_deg,
                longitude_deg,
                elevation_m,
                sqm_mag_arcsec2,
                bortle_class,
                lp_artificial_brightness_mcd_m2,
                lp_natural_sky_ratio,
                lp_estimated_total_brightness_mcd_m2,
                lp_estimated_sqm_mag_arcsec2,
                lp_estimated_bortle_class,
                lp_dataset_name,
                lp_provider_name,
                lp_source,
                lp_source_unit,
                lp_data_kind,
                lp_updated_at,
                south_horizon_open,
                notes
            FROM sites
            ORDER BY name, id;
            """
        ).fetchall()
        sites = [_row_to_site(row) for row in rows]
        for site in sites:
            site.horizon_profile = _load_site_horizon_profile(self.connection, site.id)
        return sites

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
                    lp_artificial_brightness_mcd_m2 = ?,
                    lp_natural_sky_ratio = ?,
                    lp_estimated_total_brightness_mcd_m2 = ?,
                    lp_estimated_sqm_mag_arcsec2 = ?,
                    lp_estimated_bortle_class = ?,
                    lp_dataset_name = ?,
                    lp_provider_name = ?,
                    lp_source = ?,
                    lp_source_unit = ?,
                    lp_data_kind = ?,
                    lp_updated_at = ?,
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
                    site.lp_artificial_brightness_mcd_m2,
                    site.lp_natural_sky_ratio,
                    site.lp_estimated_total_brightness_mcd_m2,
                    site.lp_estimated_sqm_mag_arcsec2,
                    site.lp_estimated_bortle_class,
                    site.lp_dataset_name,
                    site.lp_provider_name,
                    site.lp_source,
                    site.lp_source_unit,
                    site.lp_data_kind,
                    site.lp_updated_at,
                    int(site.south_horizon_open),
                    site.notes,
                    site.id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Site not found: {site.id}")
            _replace_site_horizon_profile(self.connection, site.id, site.horizon_profile)
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
                # Update sequences in place: their ids are referenced by Frames (provenance), so a
                # routine plan edit must keep them. Only genuinely new sequences are inserted.
                to_insert = self._reconcile_existing_sequences(plan)
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
                to_insert = sorted(plan.sequences, key=lambda item: item.sequence_order)

            for sequence in to_insert:
                _validate_sequence(sequence)
                self._insert_sequence(plan.id, sequence)

        return self.get_acquisition_plan(plan.id)

    def _insert_sequence(self, plan_id: str, sequence: AcquisitionSequence) -> None:
        self.connection.execute(
            """
            INSERT INTO acquisition_sequences (
                plan_id, sequence_order, name, frame_type, exposure_s, frame_count,
                iso, gain, offset_value, binning_x, binning_y, filter_id, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                plan_id,
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

    def _reconcile_existing_sequences(self, plan: AcquisitionPlan) -> list[AcquisitionSequence]:
        """Bring a saved plan's sequences in line with ``plan`` without changing surviving ids.

        A sequence is matched to an existing one by its ``id`` when given, otherwise by
        ``sequence_order``. Matched sequences are updated in place, so Frames that reference them
        keep their provenance. Returns the sequences that are genuinely new and must be inserted.
        Refuses (``ValidationError``) to remove a sequence that Frames reference, or to change the
        frame_type of one. Must run inside the caller's transaction.
        """
        existing = {
            row["id"]: row
            for row in self.connection.execute(
                "SELECT id, sequence_order, frame_type FROM acquisition_sequences WHERE plan_id = ?;",
                (plan.id,),
            ).fetchall()
        }
        by_order = {row["sequence_order"]: sequence_id for sequence_id, row in existing.items()}

        incoming = sorted(plan.sequences, key=lambda item: item.sequence_order)
        for sequence in incoming:
            _validate_sequence(sequence)

        claimed: dict[int, AcquisitionSequence] = {}
        for sequence in incoming:  # explicit ids first
            if sequence.id is None:
                continue
            if sequence.id not in existing:
                raise ValidationError(f"Acquisition sequence {sequence.id} does not belong to plan {plan.id}.")
            if sequence.id in claimed:
                raise ValidationError(f"Acquisition sequence {sequence.id} appears twice in plan {plan.id}.")
            claimed[sequence.id] = sequence

        new_sequences: list[AcquisitionSequence] = []
        for sequence in incoming:  # then match id-less sequences by order
            if sequence.id is not None:
                continue
            matched = by_order.get(sequence.sequence_order)
            if matched is not None and matched not in claimed:
                claimed[matched] = sequence
            else:
                new_sequences.append(sequence)

        for sequence_id, row in existing.items():
            references = int(
                self.connection.execute("SELECT COUNT(*) FROM frames WHERE sequence_id = ?;", (sequence_id,)).fetchone()[0]
            )
            if not references:
                continue
            kept = claimed.get(sequence_id)
            if kept is None:
                raise ValidationError(
                    f"Acquisition sequence {row['sequence_order']} is used by {references} frame(s) and cannot be removed."
                )
            if kept.frame_type != row["frame_type"]:
                raise ValidationError(
                    f"Acquisition sequence {row['sequence_order']} is used by {references} frame(s); "
                    f"its frame_type cannot change."
                )

        # Two phases so a reordering can never collide with UNIQUE (plan_id, sequence_order).
        self.connection.execute(
            "UPDATE acquisition_sequences SET sequence_order = sequence_order + 1000000 WHERE plan_id = ?;",
            (plan.id,),
        )
        for sequence_id, sequence in claimed.items():
            self.connection.execute(
                """
                UPDATE acquisition_sequences
                SET sequence_order = ?, name = ?, frame_type = ?, exposure_s = ?, frame_count = ?,
                    iso = ?, gain = ?, offset_value = ?, binning_x = ?, binning_y = ?, filter_id = ?, notes = ?
                WHERE id = ?;
                """,
                (
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
                    sequence_id,
                ),
            )
        for sequence_id in existing:
            if sequence_id not in claimed:
                self.connection.execute("DELETE FROM acquisition_sequences WHERE id = ?;", (sequence_id,))
        return new_sequences

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
    if site.lp_artificial_brightness_mcd_m2 is not None and site.lp_artificial_brightness_mcd_m2 < 0:
        raise ValidationError("Site lp_artificial_brightness_mcd_m2 must be >= 0.")
    if site.lp_natural_sky_ratio is not None and site.lp_natural_sky_ratio < 0:
        raise ValidationError("Site lp_natural_sky_ratio must be >= 0.")
    if site.lp_estimated_total_brightness_mcd_m2 is not None and site.lp_estimated_total_brightness_mcd_m2 < 0:
        raise ValidationError("Site lp_estimated_total_brightness_mcd_m2 must be >= 0.")
    if site.lp_estimated_bortle_class is not None and not 1 <= site.lp_estimated_bortle_class <= 9:
        raise ValidationError("Site lp_estimated_bortle_class must be in [1, 9].")
    if site.lp_data_kind is not None and site.lp_data_kind not in {"modeled", "estimated"}:
        raise ValidationError("Site lp_data_kind must be 'modeled' or 'estimated'.")
    try:
        normalize_local_horizon_profile(site.horizon_profile)
    except ValueError as error:
        raise ValidationError(str(error)) from error


def _load_site_horizon_profile(connection: sqlite3.Connection, site_id: str) -> list[LocalHorizonPoint]:
    rows = connection.execute(
        """
        SELECT azimuth_deg, min_altitude_deg
        FROM site_horizon_profile_points
        WHERE site_id = ?
        ORDER BY azimuth_deg;
        """,
        (site_id,),
    ).fetchall()
    return [
        LocalHorizonPoint(
            azimuth_deg=row["azimuth_deg"],
            min_altitude_deg=row["min_altitude_deg"],
        )
        for row in rows
    ]


def _replace_site_horizon_profile(
    connection: sqlite3.Connection,
    site_id: str,
    points: list[LocalHorizonPoint],
) -> None:
    normalized_points = normalize_local_horizon_profile(points)
    connection.execute("DELETE FROM site_horizon_profile_points WHERE site_id = ?;", (site_id,))
    connection.executemany(
        """
        INSERT INTO site_horizon_profile_points (
            site_id,
            azimuth_deg,
            min_altitude_deg
        ) VALUES (?, ?, ?);
        """,
        [
            (site_id, point.azimuth_deg, point.min_altitude_deg)
            for point in normalized_points
        ],
    )


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
        lp_artificial_brightness_mcd_m2=row["lp_artificial_brightness_mcd_m2"],
        lp_natural_sky_ratio=row["lp_natural_sky_ratio"],
        lp_estimated_total_brightness_mcd_m2=row["lp_estimated_total_brightness_mcd_m2"],
        lp_estimated_sqm_mag_arcsec2=row["lp_estimated_sqm_mag_arcsec2"],
        lp_estimated_bortle_class=row["lp_estimated_bortle_class"],
        lp_dataset_name=row["lp_dataset_name"],
        lp_provider_name=row["lp_provider_name"],
        lp_source=row["lp_source"],
        lp_source_unit=row["lp_source_unit"],
        lp_data_kind=row["lp_data_kind"],
        lp_updated_at=row["lp_updated_at"],
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
