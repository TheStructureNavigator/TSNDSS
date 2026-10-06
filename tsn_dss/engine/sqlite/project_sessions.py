from __future__ import annotations

"""Explicit Project <-> Session association persistence."""

import sqlite3

from ...domain.models import Project, Session
from .db import transaction
from .planning import ValidationError
from .project_repository import _row_to_project
from .sessions import _row_to_session


class ProjectSessionRepository:
    """Canonical non-owning many-to-many relation between Projects and Sessions."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def associate(self, project_id: str, session_id: str) -> None:
        """Persist the association. Repeated calls are idempotent."""
        self._validate_ids(project_id, session_id)
        self._require_project(project_id)
        self._require_session(session_id)
        with transaction(self.connection):
            self.connection.execute(
                """
                INSERT OR IGNORE INTO project_sessions (project_id, session_id)
                VALUES (?, ?);
                """,
                (project_id, session_id),
            )

    def remove(self, project_id: str, session_id: str) -> None:
        """Remove only the Project <-> Session association row, if present."""
        self._validate_ids(project_id, session_id)
        with transaction(self.connection):
            self.connection.execute(
                "DELETE FROM project_sessions WHERE project_id = ? AND session_id = ?;",
                (project_id, session_id),
            )

    def has_association(self, project_id: str, session_id: str) -> bool:
        self._validate_ids(project_id, session_id)
        row = self.connection.execute(
            "SELECT 1 FROM project_sessions WHERE project_id = ? AND session_id = ?;",
            (project_id, session_id),
        ).fetchone()
        return row is not None

    def list_sessions_for_project(self, project_id: str) -> list[Session]:
        if not project_id:
            raise ValidationError("Project id is required.")
        rows = self.connection.execute(
            """
            SELECT s.id, s.title, s.state, s.started_at, s.ended_at, s.final_state,
                   s.operator_id, s.site_id, s.notes
            FROM project_sessions ps
            JOIN sessions s ON s.id = ps.session_id
            WHERE ps.project_id = ?
            ORDER BY s.started_at, s.id;
            """,
            (project_id,),
        ).fetchall()
        return [_row_to_session(row) for row in rows]

    def list_projects_for_session(self, session_id: str) -> list[Project]:
        if not session_id:
            raise ValidationError("Session id is required.")
        rows = self.connection.execute(
            """
            SELECT p.id, p.display_name, p.dir_key, p.target_id, p.target_label, p.status, p.notes
            FROM project_sessions ps
            JOIN projects p ON p.id = ps.project_id
            WHERE ps.session_id = ?
            ORDER BY p.dir_key;
            """,
            (session_id,),
        ).fetchall()
        return [_row_to_project(row) for row in rows]

    @staticmethod
    def _validate_ids(project_id: str, session_id: str) -> None:
        if not project_id:
            raise ValidationError("Project id is required.")
        if not session_id:
            raise ValidationError("Session id is required.")

    def _require_project(self, project_id: str) -> None:
        if self.connection.execute("SELECT 1 FROM projects WHERE id = ?;", (project_id,)).fetchone() is None:
            raise sqlite3.IntegrityError(f"Missing project for Project <-> Session association: {project_id}")

    def _require_session(self, session_id: str) -> None:
        if self.connection.execute("SELECT 1 FROM sessions WHERE id = ?;", (session_id,)).fetchone() is None:
            raise sqlite3.IntegrityError(f"Missing session for Project <-> Session association: {session_id}")
