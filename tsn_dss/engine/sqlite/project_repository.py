from __future__ import annotations

"""Canonical Project records.

Metadata persistence only. Filesystem layout stays with ``ProjectStorage``; the
coordination of the two lives in ``engine/project_registry.py``.
"""

import sqlite3
import uuid

from ...domain.models import Project
from .db import transaction
from .planning import ValidationError

ALLOWED_PROJECT_STATUSES = {"active", "archived"}

_COLUMNS = "id, display_name, dir_key, target_id, target_label, status, notes"


def new_project_id() -> str:
    """Opaque stable identity. Never derived from the directory name."""
    return f"project:{uuid.uuid4().hex[:12]}"


class ProjectInUseError(RuntimeError):
    """A Project cannot be deleted while canonical records depend on it."""

    def __init__(
        self,
        project_id: str,
        mosaic_plan_count: int,
        *,
        capture_count: int = 0,
        frame_count: int = 0,
    ) -> None:
        parts = []
        if mosaic_plan_count:
            parts.append(f"{mosaic_plan_count} mosaic plan(s)")
        if capture_count:
            parts.append(f"{capture_count} registered capture(s)")
        if frame_count:
            parts.append(f"{frame_count} registered frame(s)")
        super().__init__(f"Project {project_id} is referenced by {', '.join(parts) or 'other records'} and cannot be deleted.")
        self.project_id = project_id
        self.mosaic_plan_count = mosaic_plan_count
        self.capture_count = capture_count
        self.frame_count = frame_count


class ProjectRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def create_project(self, project: Project) -> Project:
        _validate_project(project)
        try:
            with transaction(self.connection):
                self.connection.execute(
                    """
                    INSERT INTO projects (id, display_name, dir_key, target_id, target_label, status, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        project.id,
                        project.display_name,
                        project.dir_key,
                        project.target_id,
                        project.target_label,
                        project.status,
                        project.notes,
                    ),
                )
        except sqlite3.IntegrityError as error:
            if "UNIQUE" in str(error).upper():
                raise ValidationError(
                    f"A project with id {project.id!r} or dir_key {project.dir_key!r} already exists."
                ) from error
            raise
        return self.get_project(project.id)

    def register_project(
        self,
        *,
        dir_key: str,
        display_name: str | None = None,
        target_label: str | None = None,
    ) -> Project:
        """Create a Project with a freshly generated id for an existing or new directory."""
        return self.create_project(
            Project(
                id=new_project_id(),
                display_name=display_name if display_name else dir_key,
                dir_key=dir_key,
                target_label=target_label,
            )
        )

    def get_project(self, project_id: str) -> Project | None:
        row = self.connection.execute(
            f"SELECT {_COLUMNS} FROM projects WHERE id = ?;",
            (project_id,),
        ).fetchone()
        return _row_to_project(row) if row else None

    def get_project_by_dir_key(self, dir_key: str) -> Project | None:
        row = self.connection.execute(
            f"SELECT {_COLUMNS} FROM projects WHERE dir_key = ?;",
            (dir_key,),
        ).fetchone()
        return _row_to_project(row) if row else None

    def list_projects(self) -> list[Project]:
        rows = self.connection.execute(f"SELECT {_COLUMNS} FROM projects ORDER BY dir_key;").fetchall()
        return [_row_to_project(row) for row in rows]

    def dir_key_to_id(self) -> dict[str, str]:
        return {
            str(row["dir_key"]): str(row["id"])
            for row in self.connection.execute("SELECT id, dir_key FROM projects;").fetchall()
        }

    def set_target_label(self, project_id: str, target_label: str | None) -> Project:
        if target_label is not None and not isinstance(target_label, str):
            raise ValidationError("Project target_label must be a string or None.")
        with transaction(self.connection):
            cursor = self.connection.execute(
                """
                UPDATE projects
                SET target_label = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?;
                """,
                (target_label, project_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Project not found: {project_id}")
        return self.get_project(project_id)

    def count_mosaic_plans(self, project_id: str) -> int:
        return int(
            self.connection.execute(
                "SELECT COUNT(*) FROM mosaic_plans WHERE project_id = ?;",
                (project_id,),
            ).fetchone()[0]
        )

    def count_captures(self, project_id: str) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM captures WHERE project_id = ?;", (project_id,)).fetchone()[0])

    def count_frames(self, project_id: str) -> int:
        return int(self.connection.execute("SELECT COUNT(*) FROM frames WHERE project_id = ?;", (project_id,)).fetchone()[0])

    def dependents(self, project_id: str) -> ProjectInUseError | None:
        """The canonical records that depend on a project, or None if it can be deleted."""
        plans, captures, frames = (
            self.count_mosaic_plans(project_id),
            self.count_captures(project_id),
            self.count_frames(project_id),
        )
        if plans or captures or frames:
            return ProjectInUseError(project_id, plans, capture_count=captures, frame_count=frames)
        return None

    def delete_project(self, project_id: str) -> None:
        """Delete the canonical record. Refuses (ProjectInUseError) while anything depends on it."""
        try:
            with transaction(self.connection):
                cursor = self.connection.execute("DELETE FROM projects WHERE id = ?;", (project_id,))
                if cursor.rowcount == 0:
                    raise KeyError(f"Project not found: {project_id}")
        except sqlite3.IntegrityError as error:
            raise (self.dependents(project_id) or ProjectInUseError(project_id, 0)) from error


def _validate_project(project: Project) -> None:
    if not project.id:
        raise ValidationError("Project id is required.")
    if not project.dir_key:
        raise ValidationError("Project dir_key is required.")
    if not project.display_name:
        raise ValidationError("Project display_name is required.")
    if project.status not in ALLOWED_PROJECT_STATUSES:
        raise ValidationError(f"Unsupported project status: {project.status}")
    if project.target_label is not None and not isinstance(project.target_label, str):
        raise ValidationError("Project target_label must be a string or None.")


def _row_to_project(row: sqlite3.Row) -> Project:
    return Project(
        id=row["id"],
        display_name=row["display_name"],
        dir_key=row["dir_key"],
        target_id=row["target_id"],
        target_label=row["target_label"],
        status=row["status"],
        notes=row["notes"],
    )
