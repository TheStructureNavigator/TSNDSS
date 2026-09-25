from __future__ import annotations

"""Canonical Frame records: one physical observational file plus its metadata and provenance.

Metadata only. The bytes stay on disk at ``<project root>/<rel_path>``.
"""

import json
import re
import sqlite3
from datetime import datetime, timezone

from ...domain.models import Frame
from ..projects import validate_project_relative_path
from .captures import CaptureRepository
from .db import transaction
from .observation import ObservationRepository
from .planning import PlanningRepository, ValidationError

ALLOWED_FRAME_TYPES = {"light", "dark", "flat", "bias", "dark_flat"}
ALLOWED_FRAME_ORIGINS = {"raw", "device_stack", "master", "unknown"}
ALLOWED_CAPTURED_AT_SOURCES = {"fits_header", "exif", "device", "filename", "user"}

_SHA256 = re.compile(r"^[0-9a-f]{64}$")

# Stored columns in a fixed order. ``id`` and ``created_at`` are managed by the database.
_FRAME_COLUMNS = (
    "project_id", "capture_id", "observation_id", "sequence_id", "rel_path",
    "frame_type", "origin", "stack_count", "file_format",
    "size_bytes", "content_sha256", "hashed_at", "width_px", "height_px", "instrument_name",
    "captured_at", "captured_at_source",
    "exposure_s", "gain", "offset_value", "iso", "binning_x", "binning_y", "camera_temp_c",
    "filter_id", "filter_name",
    "mount_ra_deg", "mount_dec_deg", "guiding_rms_arcsec",
    "pointing_source_kind", "pointing_source_id", "pointing_label", "planned_ra_deg", "planned_dec_deg",
    "accepted", "rejection_reason",
    "fwhm_px", "fwhm_arcsec", "eccentricity", "star_count",
    "background_median", "background_sigma", "snr_estimate",
    "metadata_json",
)
_SELECT_COLUMNS = "id, " + ", ".join(_FRAME_COLUMNS)


class FrameRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.observations = ObservationRepository(connection)
        self.planning = PlanningRepository(connection)
        self.captures = CaptureRepository(connection)

    def create_frame(self, frame: Frame) -> Frame:
        self._validate_frame_payload(frame)
        self._validate_frame_domain_consistency(frame)

        placeholders = ", ".join("?" for _ in _FRAME_COLUMNS)
        try:
            with transaction(self.connection):
                cursor = self.connection.execute(
                    f"INSERT INTO frames ({', '.join(_FRAME_COLUMNS)}) VALUES ({placeholders});",
                    _frame_values(frame),
                )
        except sqlite3.IntegrityError as error:
            if "UNIQUE" in str(error).upper():
                raise ValidationError(
                    f"A frame is already registered at {frame.rel_path!r} in this project."
                ) from error
            raise
        return self.get_frame(int(cursor.lastrowid))

    def get_frame(self, frame_id: int) -> Frame | None:
        row = self.connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM frames WHERE id = ?;",
            (frame_id,),
        ).fetchone()
        return _row_to_frame(row) if row else None

    def get_frame_by_path(self, project_id: str, rel_path: str) -> Frame | None:
        row = self.connection.execute(
            f"SELECT {_SELECT_COLUMNS} FROM frames WHERE project_id = ? AND rel_path = ?;",
            (project_id, rel_path),
        ).fetchone()
        return _row_to_frame(row) if row else None

    def list_frames(
        self,
        *,
        observation_id: str | None = None,
        capture_id: str | None = None,
        project_id: str | None = None,
        frame_type: str | None = None,
        accepted: bool | None = None,
    ) -> list[Frame]:
        clauses: list[str] = []
        params: list[object] = []
        for column, value in (
            ("observation_id", observation_id),
            ("capture_id", capture_id),
            ("project_id", project_id),
            ("frame_type", frame_type),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if accepted is not None:
            clauses.append("accepted = ?")
            params.append(_to_db_bool(accepted))

        query = f"SELECT {_SELECT_COLUMNS} FROM frames"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY captured_at, id;"
        return [_row_to_frame(row) for row in self.connection.execute(query, params).fetchall()]

    def update_frame(self, frame: Frame) -> Frame:
        if frame.id is None:
            raise ValidationError("Frame id is required for update.")
        current = self.get_frame(frame.id)
        if current is None:
            raise KeyError(f"Frame not found: {frame.id}")
        _require_recorded_hash_unchanged(current, frame)
        self._validate_frame_payload(frame)
        self._validate_frame_domain_consistency(frame)

        assignments = ", ".join(f"{column} = ?" for column in _FRAME_COLUMNS)
        try:
            with transaction(self.connection):
                self.connection.execute(
                    f"UPDATE frames SET {assignments} WHERE id = ?;",
                    (*_frame_values(frame), frame.id),
                )
        except sqlite3.IntegrityError as error:
            if "UNIQUE" in str(error).upper():
                raise ValidationError(
                    f"A frame is already registered at {frame.rel_path!r} in this project."
                ) from error
            raise
        return self.get_frame(frame.id)

    def record_content_hash(
        self,
        frame_id: int,
        *,
        content_sha256: str,
        size_bytes: int,
        hashed_at: str | None = None,
    ) -> Frame:
        """Record the hash of a frame that has none yet. A recorded hash can never change."""
        current = self.get_frame(frame_id)
        if current is None:
            raise KeyError(f"Frame not found: {frame_id}")
        if not _SHA256.match(content_sha256 or ""):
            raise ValidationError("content_sha256 must be 64 lowercase hexadecimal characters.")
        if size_bytes is None or size_bytes < 0:
            raise ValidationError("size_bytes must be >= 0 when a content hash is recorded.")
        if current.content_sha256 is not None:
            if (current.content_sha256, current.size_bytes) == (content_sha256, size_bytes):
                return current
            raise ValidationError("A recorded frame content hash is immutable.")

        with transaction(self.connection):
            self.connection.execute(
                """
                UPDATE frames
                SET content_sha256 = ?, size_bytes = ?, hashed_at = ?
                WHERE id = ? AND content_sha256 IS NULL;
                """,
                (content_sha256, size_bytes, hashed_at or _utc_now(), frame_id),
            )
        return self.get_frame(frame_id)

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

    def list_duplicate_hashes(self, project_id: str) -> dict[str, list[str]]:
        """Content hashes shared by more than one frame path in a project. Duplicates are allowed."""
        rows = self.connection.execute(
            """
            SELECT content_sha256, rel_path
            FROM frames
            WHERE project_id = ?
              AND content_sha256 IN (
                  SELECT content_sha256 FROM frames
                  WHERE project_id = ? AND content_sha256 IS NOT NULL
                  GROUP BY content_sha256 HAVING COUNT(*) > 1
              )
            ORDER BY content_sha256, rel_path;
            """,
            (project_id, project_id),
        ).fetchall()
        duplicates: dict[str, list[str]] = {}
        for row in rows:
            duplicates.setdefault(row["content_sha256"], []).append(row["rel_path"])
        return duplicates

    # -- validation -----------------------------------------------------------------------------

    def _validate_frame_payload(self, frame: Frame) -> None:
        if not frame.project_id or not frame.capture_id:
            raise ValidationError("Frame project_id and capture_id are required.")
        try:
            validate_project_relative_path(frame.rel_path)
        except ValueError as error:
            raise ValidationError(str(error)) from error
        if frame.frame_type is not None and frame.frame_type not in ALLOWED_FRAME_TYPES:
            raise ValidationError(f"Unsupported frame type: {frame.frame_type}")
        if frame.origin not in ALLOWED_FRAME_ORIGINS:
            raise ValidationError(f"Unsupported frame origin: {frame.origin}")
        if frame.stack_count is not None and frame.stack_count <= 0:
            raise ValidationError("Frame stack_count must be > 0 when provided.")
        if frame.size_bytes is not None and frame.size_bytes < 0:
            raise ValidationError("Frame size_bytes must be >= 0 when provided.")
        if frame.content_sha256 is not None:
            if not _SHA256.match(frame.content_sha256):
                raise ValidationError("Frame content_sha256 must be 64 lowercase hexadecimal characters.")
            if frame.size_bytes is None:
                raise ValidationError("Frame size_bytes is required when content_sha256 is recorded.")
        if frame.captured_at_source is not None and frame.captured_at_source not in ALLOWED_CAPTURED_AT_SOURCES:
            raise ValidationError(f"Unsupported captured_at_source: {frame.captured_at_source}")
        if frame.exposure_s is not None and frame.exposure_s < 0:
            raise ValidationError("Frame exposure_s must be >= 0 when provided.")
        if frame.iso is not None and frame.iso <= 0:
            raise ValidationError("Frame iso must be > 0 when provided.")
        for name in ("binning_x", "binning_y", "width_px", "height_px"):
            value = getattr(frame, name)
            if value is not None and value <= 0:
                raise ValidationError(f"Frame {name} must be > 0 when provided.")
        if frame.planned_ra_deg is not None and not 0 <= frame.planned_ra_deg < 360:
            raise ValidationError("Frame planned_ra_deg must be in [0, 360).")
        if frame.planned_dec_deg is not None and not -90 <= frame.planned_dec_deg <= 90:
            raise ValidationError("Frame planned_dec_deg must be in [-90, 90].")
        if frame.accepted is True and frame.rejection_reason is not None:
            raise ValidationError("Accepted frame cannot have a rejection_reason.")
        if frame.accepted is False and not frame.rejection_reason:
            raise ValidationError("Rejected frame must have a rejection_reason.")
        if not isinstance(frame.metadata, dict):
            raise ValidationError("Frame metadata must be a dictionary.")

    def _validate_frame_domain_consistency(self, frame: Frame) -> None:
        capture = self.captures.get_capture(frame.capture_id)
        if capture is None:
            raise sqlite3.IntegrityError(f"Missing capture for frame: {frame.capture_id}")
        if capture.project_id != frame.project_id:
            raise ValidationError("Frame project_id must match its Capture's project_id.")
        if not frame.rel_path.startswith(capture.rel_path + "/"):
            raise ValidationError(f"Frame rel_path must lie inside its Capture directory {capture.rel_path!r}.")

        observation = None
        if frame.observation_id is not None:
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

            if observation is None:
                raise ValidationError("Frame sequence_id requires an Observation.")
            if observation.acquisition_plan_id is None:
                raise ValidationError("Frame sequence_id requires Observation.acquisition_plan_id.")
            if sequence_row["plan_id"] != observation.acquisition_plan_id:
                raise ValidationError("Frame sequence must belong to the Observation acquisition plan.")
            if sequence_row["frame_type"] != frame.frame_type:
                raise ValidationError("Frame frame_type must match AcquisitionSequence frame_type.")
            if (
                frame.filter_id is not None
                and sequence_row["filter_id"] is not None
                and sequence_row["filter_id"] != frame.filter_id
            ):
                raise ValidationError("Frame filter_id must match AcquisitionSequence filter_id when sequence filter is defined.")

        if frame.filter_id is not None:
            equipment = self.planning.get_equipment(frame.filter_id)
            if equipment is None:
                raise sqlite3.IntegrityError(f"Missing filter equipment for frame: {frame.filter_id}")
            if equipment.equipment_type != "filter":
                raise ValidationError("Frame filter_id must reference equipment of type 'filter'.")


def _require_recorded_hash_unchanged(current: Frame, updated: Frame) -> None:
    if current.content_sha256 is None:
        return
    if (
        updated.content_sha256 != current.content_sha256
        or updated.size_bytes != current.size_bytes
        or updated.hashed_at != current.hashed_at
    ):
        raise ValidationError("A recorded frame content hash is immutable.")


def _frame_values(frame: Frame) -> tuple:
    values = []
    for column in _FRAME_COLUMNS:
        if column == "metadata_json":
            values.append(json.dumps(frame.metadata, sort_keys=True))
        elif column == "accepted":
            values.append(_to_db_bool(frame.accepted))
        else:
            values.append(getattr(frame, column))
    return tuple(values)


def _to_db_bool(value: bool | None) -> int | None:
    if value is None:
        return None
    return 1 if value else 0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_frame(row: sqlite3.Row) -> Frame:
    values = {column: row[column] for column in _FRAME_COLUMNS if column not in {"accepted", "metadata_json"}}
    return Frame(
        id=row["id"],
        accepted=None if row["accepted"] is None else bool(row["accepted"]),
        metadata=json.loads(row["metadata_json"]),
        **values,
    )
