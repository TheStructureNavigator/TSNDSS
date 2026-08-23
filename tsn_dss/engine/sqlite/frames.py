from __future__ import annotations

import json
import sqlite3

from ...domain.models import Frame
from .db import transaction
from .observation import ObservationRepository
from .planning import PlanningRepository, ValidationError

ALLOWED_FRAME_TYPES = {"light", "dark", "flat", "bias", "dark_flat"}


class FrameRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.observations = ObservationRepository(connection)
        self.planning = PlanningRepository(connection)

    def create_frame(self, frame: Frame) -> Frame:
        self._validate_frame_payload(frame)
        self._validate_frame_domain_consistency(frame)

        with transaction(self.connection):
            cursor = self.connection.execute(
                """
                INSERT INTO frames (
                    observation_id,
                    sequence_id,
                    frame_type,
                    file_path,
                    captured_at,
                    exposure_s,
                    iso,
                    gain,
                    offset_value,
                    filter_id,
                    accepted,
                    rejection_reason,
                    fwhm_px,
                    fwhm_arcsec,
                    eccentricity,
                    star_count,
                    background_median,
                    background_sigma,
                    snr_estimate,
                    mount_ra_deg,
                    mount_dec_deg,
                    guiding_rms_arcsec,
                    camera_temp_c,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    frame.observation_id,
                    frame.sequence_id,
                    frame.frame_type,
                    frame.file_path,
                    frame.captured_at,
                    frame.exposure_s,
                    frame.iso,
                    frame.gain,
                    frame.offset_value,
                    frame.filter_id,
                    _to_db_bool(frame.accepted),
                    frame.rejection_reason,
                    frame.fwhm_px,
                    frame.fwhm_arcsec,
                    frame.eccentricity,
                    frame.star_count,
                    frame.background_median,
                    frame.background_sigma,
                    frame.snr_estimate,
                    frame.mount_ra_deg,
                    frame.mount_dec_deg,
                    frame.guiding_rms_arcsec,
                    frame.camera_temp_c,
                    json.dumps(frame.metadata, sort_keys=True),
                ),
            )

        return self.get_frame(int(cursor.lastrowid))

    def get_frame(self, frame_id: int) -> Frame | None:
        row = self.connection.execute(
            """
            SELECT
                id,
                observation_id,
                sequence_id,
                frame_type,
                file_path,
                captured_at,
                exposure_s,
                iso,
                gain,
                offset_value,
                filter_id,
                accepted,
                rejection_reason,
                fwhm_px,
                fwhm_arcsec,
                eccentricity,
                star_count,
                background_median,
                background_sigma,
                snr_estimate,
                mount_ra_deg,
                mount_dec_deg,
                guiding_rms_arcsec,
                camera_temp_c,
                metadata_json
            FROM frames
            WHERE id = ?;
            """,
            (frame_id,),
        ).fetchone()
        return _row_to_frame(row) if row else None

    def list_frames(
        self,
        *,
        observation_id: str,
        frame_type: str | None = None,
        accepted: bool | None = None,
    ) -> list[Frame]:
        query = """
            SELECT
                id,
                observation_id,
                sequence_id,
                frame_type,
                file_path,
                captured_at,
                exposure_s,
                iso,
                gain,
                offset_value,
                filter_id,
                accepted,
                rejection_reason,
                fwhm_px,
                fwhm_arcsec,
                eccentricity,
                star_count,
                background_median,
                background_sigma,
                snr_estimate,
                mount_ra_deg,
                mount_dec_deg,
                guiding_rms_arcsec,
                camera_temp_c,
                metadata_json
            FROM frames
            WHERE observation_id = ?
        """
        params: list[object] = [observation_id]
        if frame_type is not None:
            query += " AND frame_type = ?"
            params.append(frame_type)
        if accepted is not None:
            query += " AND accepted = ?"
            params.append(_to_db_bool(accepted))
        query += " ORDER BY captured_at, id;"

        rows = self.connection.execute(query, params).fetchall()
        return [_row_to_frame(row) for row in rows]

    def update_frame(self, frame: Frame) -> Frame:
        if frame.id is None:
            raise ValidationError("Frame id is required for update.")
        self._validate_frame_payload(frame)
        self._validate_frame_domain_consistency(frame)

        with transaction(self.connection):
            cursor = self.connection.execute(
                """
                UPDATE frames
                SET
                    observation_id = ?,
                    sequence_id = ?,
                    frame_type = ?,
                    file_path = ?,
                    captured_at = ?,
                    exposure_s = ?,
                    iso = ?,
                    gain = ?,
                    offset_value = ?,
                    filter_id = ?,
                    accepted = ?,
                    rejection_reason = ?,
                    fwhm_px = ?,
                    fwhm_arcsec = ?,
                    eccentricity = ?,
                    star_count = ?,
                    background_median = ?,
                    background_sigma = ?,
                    snr_estimate = ?,
                    mount_ra_deg = ?,
                    mount_dec_deg = ?,
                    guiding_rms_arcsec = ?,
                    camera_temp_c = ?,
                    metadata_json = ?
                WHERE id = ?;
                """,
                (
                    frame.observation_id,
                    frame.sequence_id,
                    frame.frame_type,
                    frame.file_path,
                    frame.captured_at,
                    frame.exposure_s,
                    frame.iso,
                    frame.gain,
                    frame.offset_value,
                    frame.filter_id,
                    _to_db_bool(frame.accepted),
                    frame.rejection_reason,
                    frame.fwhm_px,
                    frame.fwhm_arcsec,
                    frame.eccentricity,
                    frame.star_count,
                    frame.background_median,
                    frame.background_sigma,
                    frame.snr_estimate,
                    frame.mount_ra_deg,
                    frame.mount_dec_deg,
                    frame.guiding_rms_arcsec,
                    frame.camera_temp_c,
                    json.dumps(frame.metadata, sort_keys=True),
                    frame.id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Frame not found: {frame.id}")

        return self.get_frame(frame.id)

    def review_frame(
        self,
        frame_id: int,
        *,
        accepted: bool,
        rejection_reason: str | None = None,
    ) -> Frame:
        frame = self.get_frame(frame_id)
        if frame is None:
            raise KeyError(f"Frame not found: {frame_id}")

        if accepted and rejection_reason is not None:
            raise ValidationError("Accepted frame cannot have a rejection_reason.")
        if not accepted and not rejection_reason:
            raise ValidationError("Rejected frame must have a rejection_reason.")

        frame.accepted = accepted
        frame.rejection_reason = None if accepted else rejection_reason
        return self.update_frame(frame)

    def delete_frame(self, frame_id: int) -> None:
        with transaction(self.connection):
            self.connection.execute("DELETE FROM frames WHERE id = ?;", (frame_id,))

    def _validate_frame_payload(self, frame: Frame) -> None:
        if not frame.observation_id:
            raise ValidationError("Frame observation_id is required.")
        if frame.frame_type not in ALLOWED_FRAME_TYPES:
            raise ValidationError(f"Unsupported frame type: {frame.frame_type}")
        if not frame.file_path:
            raise ValidationError("Frame file_path is required.")
        if frame.exposure_s is not None and frame.exposure_s <= 0:
            raise ValidationError("Frame exposure_s must be > 0 when provided.")
        if frame.iso is not None and frame.iso <= 0:
            raise ValidationError("Frame iso must be > 0 when provided.")
        if frame.accepted is True and frame.rejection_reason is not None:
            raise ValidationError("Accepted frame cannot have a rejection_reason.")
        if frame.accepted is False and not frame.rejection_reason:
            raise ValidationError("Rejected frame must have a rejection_reason.")
        if not isinstance(frame.metadata, dict):
            raise ValidationError("Frame metadata must be a dictionary.")

    def _validate_frame_domain_consistency(self, frame: Frame) -> None:
        observation = self.observations.get_observation(frame.observation_id)
        if observation is None:
            raise sqlite3.IntegrityError(f"Missing observation for frame: {frame.observation_id}")

        if frame.sequence_id is not None:
            sequence_row = self.connection.execute(
                """
                SELECT id, plan_id, frame_type, filter_id
                FROM acquisition_sequences
                WHERE id = ?;
                """,
                (frame.sequence_id,),
            ).fetchone()
            if sequence_row is None:
                raise sqlite3.IntegrityError(f"Missing acquisition sequence for frame: {frame.sequence_id}")

            if observation.acquisition_plan_id is None:
                raise ValidationError("Frame sequence_id requires Observation.acquisition_plan_id.")
            if sequence_row["plan_id"] != observation.acquisition_plan_id:
                raise ValidationError("Frame sequence must belong to the Observation acquisition plan.")
            if sequence_row["frame_type"] != frame.frame_type:
                raise ValidationError("Frame frame_type must match AcquisitionSequence frame_type.")
            if frame.filter_id is not None and sequence_row["filter_id"] is not None and sequence_row["filter_id"] != frame.filter_id:
                raise ValidationError("Frame filter_id must match AcquisitionSequence filter_id when sequence filter is defined.")

        if frame.filter_id is not None:
            equipment = self.planning.get_equipment(frame.filter_id)
            if equipment is None:
                raise sqlite3.IntegrityError(f"Missing filter equipment for frame: {frame.filter_id}")
            if equipment.equipment_type != "filter":
                raise ValidationError("Frame filter_id must reference equipment of type 'filter'.")


def _to_db_bool(value: bool | None) -> int | None:
    if value is None:
        return None
    return 1 if value else 0


def _row_to_frame(row: sqlite3.Row) -> Frame:
    return Frame(
        id=row["id"],
        observation_id=row["observation_id"],
        sequence_id=row["sequence_id"],
        frame_type=row["frame_type"],
        file_path=row["file_path"],
        captured_at=row["captured_at"],
        exposure_s=row["exposure_s"],
        iso=row["iso"],
        gain=row["gain"],
        offset_value=row["offset_value"],
        filter_id=row["filter_id"],
        accepted=None if row["accepted"] is None else bool(row["accepted"]),
        rejection_reason=row["rejection_reason"],
        fwhm_px=row["fwhm_px"],
        fwhm_arcsec=row["fwhm_arcsec"],
        eccentricity=row["eccentricity"],
        star_count=row["star_count"],
        background_median=row["background_median"],
        background_sigma=row["background_sigma"],
        snr_estimate=row["snr_estimate"],
        mount_ra_deg=row["mount_ra_deg"],
        mount_dec_deg=row["mount_dec_deg"],
        guiding_rms_arcsec=row["guiding_rms_arcsec"],
        camera_temp_c=row["camera_temp_c"],
        metadata=json.loads(row["metadata_json"]),
    )
