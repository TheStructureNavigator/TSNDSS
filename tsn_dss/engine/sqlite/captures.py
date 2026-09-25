from __future__ import annotations

"""Canonical Capture records: ingest batches. Metadata only; the files stay on disk."""

import sqlite3
import uuid
from datetime import datetime, timezone

from ...domain.models import Capture
from ..projects import validate_capture_name
from .db import transaction
from .planning import ValidationError

ALLOWED_CAPTURE_SOURCE_KINDS = {"legacy_registered", "folder_import", "device_import", "acquisition"}
ALLOWED_IMPORT_MODES = {"copy", "move", "acquired"}

_COLUMNS = (
    "id, project_id, name, rel_path, source_kind, source_label, source_path, import_mode, "
    "imported_at, registered_at, registrar_version"
)


def new_capture_id() -> str:
    """Opaque stable identity. Never derived from the capture's name or path."""
    return f"capture:{uuid.uuid4().hex[:12]}"


def capture_rel_path(name: str) -> str:
    """The Project-relative location of a capture directory."""
    return f"captures/{name}"


class CaptureRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def create_capture(self, capture: Capture) -> Capture:
        self._validate(capture)
        registered_at = capture.registered_at or _utc_now()
        try:
            with transaction(self.connection):
                self.connection.execute(
                    f"INSERT INTO captures ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                    (
                        capture.id,
                        capture.project_id,
                        capture.name,
                        capture.rel_path,
                        capture.source_kind,
                        capture.source_label,
                        capture.source_path,
                        capture.import_mode,
                        capture.imported_at,
                        registered_at,
                        capture.registrar_version,
                    ),
                )
        except sqlite3.IntegrityError as error:
            if "UNIQUE" in str(error).upper():
                raise ValidationError(
                    f"A capture named {capture.name!r} is already registered for this project."
                ) from error
            raise
        return self.get_capture(capture.id)

    def register_capture(
        self,
        *,
        project_id: str,
        name: str,
        source_kind: str,
        source_label: str | None = None,
        source_path: str | None = None,
        import_mode: str | None = None,
        imported_at: str | None = None,
        registrar_version: str | None = None,
    ) -> Capture:
        """Create a Capture with a fresh id at ``captures/<name>``."""
        return self.create_capture(
            Capture(
                id=new_capture_id(),
                project_id=project_id,
                name=name,
                rel_path=capture_rel_path(name),
                source_kind=source_kind,
                source_label=source_label,
                source_path=source_path,
                import_mode=import_mode,
                imported_at=imported_at,
                registrar_version=registrar_version,
            )
        )

    def get_capture(self, capture_id: str) -> Capture | None:
        row = self.connection.execute(f"SELECT {_COLUMNS} FROM captures WHERE id = ?;", (capture_id,)).fetchone()
        return _row_to_capture(row) if row else None

    def get_capture_by_name(self, project_id: str, name: str) -> Capture | None:
        row = self.connection.execute(
            f"SELECT {_COLUMNS} FROM captures WHERE project_id = ? AND name = ?;",
            (project_id, name),
        ).fetchone()
        return _row_to_capture(row) if row else None

    def list_captures(self, *, project_id: str | None = None) -> list[Capture]:
        if project_id is None:
            rows = self.connection.execute(f"SELECT {_COLUMNS} FROM captures ORDER BY project_id, name;").fetchall()
        else:
            rows = self.connection.execute(
                f"SELECT {_COLUMNS} FROM captures WHERE project_id = ? ORDER BY name;",
                (project_id,),
            ).fetchall()
        return [_row_to_capture(row) for row in rows]

    def count_frames(self, capture_id: str) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM frames WHERE capture_id = ?;", (capture_id,)).fetchone()[0])

    def _validate(self, capture: Capture) -> None:
        if not capture.id or not capture.project_id:
            raise ValidationError("Capture id and project_id are required.")
        try:
            validate_capture_name(capture.name)
        except ValueError as error:
            raise ValidationError(str(error)) from error
        if capture.rel_path != capture_rel_path(capture.name):
            raise ValidationError(
                f"Capture rel_path must be {capture_rel_path(capture.name)!r} (a direct child of captures/)."
            )
        if capture.source_kind not in ALLOWED_CAPTURE_SOURCE_KINDS:
            raise ValidationError(f"Unsupported capture source_kind: {capture.source_kind}")
        if capture.import_mode is not None and capture.import_mode not in ALLOWED_IMPORT_MODES:
            raise ValidationError(f"Unsupported capture import_mode: {capture.import_mode}")
        exists = self.connection.execute("SELECT 1 FROM projects WHERE id = ?;", (capture.project_id,)).fetchone()
        if exists is None:
            raise sqlite3.IntegrityError(f"Missing project for capture: {capture.project_id}")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_capture(row: sqlite3.Row) -> Capture:
    return Capture(
        id=row["id"],
        project_id=row["project_id"],
        name=row["name"],
        rel_path=row["rel_path"],
        source_kind=row["source_kind"],
        source_label=row["source_label"],
        source_path=row["source_path"],
        import_mode=row["import_mode"],
        imported_at=row["imported_at"],
        registered_at=row["registered_at"],
        registrar_version=row["registrar_version"],
    )
