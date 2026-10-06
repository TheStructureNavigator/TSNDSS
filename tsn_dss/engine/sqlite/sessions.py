from __future__ import annotations

"""Canonical Session records for bounded operational field periods."""

import sqlite3
import uuid
from datetime import datetime, timezone

from ...domain.models import Session
from .db import transaction
from .planning import ValidationError

ALLOWED_SESSION_STATES = {"planned", "preparing", "active", "closing", "completed", "aborted"}
TERMINAL_SESSION_STATES = {"completed", "aborted"}
POST_ACTIVATION_STATES = {"active", "closing", "completed", "aborted"}
ALLOWED_SESSION_TRANSITIONS = {
    "planned": {"preparing"},
    "preparing": {"active"},
    "active": {"closing", "aborted"},
    "closing": {"completed"},
    "completed": set(),
    "aborted": set(),
}

_COLUMNS = "id, title, state, started_at, ended_at, final_state, operator_id, site_id, notes"
_UNSET = object()


def new_session_id() -> str:
    """Opaque stable Session identity."""
    return f"session:{uuid.uuid4().hex[:12]}"


class SessionRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def create_session(self, session: Session) -> Session:
        _validate_session_payload(self.connection, session)
        try:
            with transaction(self.connection):
                self.connection.execute(
                    f"INSERT INTO sessions ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);",
                    _session_values(session),
                )
        except sqlite3.IntegrityError as error:
            if "UNIQUE" in str(error).upper():
                raise ValidationError(f"A session with id {session.id!r} already exists.") from error
            raise
        return self.get_session(session.id)

    def register_session(
        self,
        *,
        started_at: str,
        title: str | None = None,
        operator_id: str | None = None,
        site_id: str | None = None,
        notes: str | None = None,
    ) -> Session:
        return self.create_session(
            Session(
                id=new_session_id(),
                title=title,
                state="planned",
                started_at=started_at,
                operator_id=operator_id,
                site_id=site_id,
                notes=notes,
            )
        )

    def get_session(self, session_id: str) -> Session | None:
        row = self.connection.execute(
            f"SELECT {_COLUMNS} FROM sessions WHERE id = ?;",
            (session_id,),
        ).fetchone()
        return _row_to_session(row) if row else None

    def list_sessions(
        self,
        *,
        state: str | None = None,
        site_id: str | None = None,
    ) -> list[Session]:
        clauses: list[str] = []
        params: list[object] = []
        if state is not None:
            if state not in ALLOWED_SESSION_STATES:
                raise ValidationError(f"Unsupported session state: {state}")
            clauses.append("state = ?")
            params.append(state)
        if site_id is not None:
            clauses.append("site_id = ?")
            params.append(site_id)

        query = f"SELECT {_COLUMNS} FROM sessions"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY started_at, id;"
        return [_row_to_session(row) for row in self.connection.execute(query, params).fetchall()]

    def update_session_metadata(
        self,
        session_id: str,
        *,
        title: str | None | object = _UNSET,
        operator_id: str | None | object = _UNSET,
        site_id: str | None | object = _UNSET,
        notes: str | None | object = _UNSET,
    ) -> Session:
        current = self.get_session(session_id)
        if current is None:
            raise KeyError(f"Session not found: {session_id}")
        if (
            current.state in POST_ACTIVATION_STATES
            and operator_id is not _UNSET
            and operator_id != current.operator_id
        ):
            raise ValidationError("Session operator_id is immutable once a Session is active.")

        updated = Session(
            id=current.id,
            title=current.title if title is _UNSET else title,
            state=current.state,
            started_at=current.started_at,
            ended_at=current.ended_at,
            final_state=current.final_state,
            operator_id=current.operator_id if operator_id is _UNSET else operator_id,
            site_id=current.site_id if site_id is _UNSET else site_id,
            notes=current.notes if notes is _UNSET else notes,
        )
        _validate_session_payload(self.connection, updated)
        with transaction(self.connection):
            self.connection.execute(
                """
                UPDATE sessions
                SET title = ?,
                    operator_id = ?,
                    site_id = ?,
                    notes = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?;
                """,
                (updated.title, updated.operator_id, updated.site_id, updated.notes, session_id),
            )
        return self.get_session(session_id)

    def transition_session(
        self,
        session_id: str,
        new_state: str,
        *,
        operator_id: str | None = None,
        ended_at: str | None = None,
    ) -> Session:
        current = self.get_session(session_id)
        if current is None:
            raise KeyError(f"Session not found: {session_id}")
        if new_state not in ALLOWED_SESSION_STATES:
            raise ValidationError(f"Unsupported session state: {new_state}")
        if new_state not in ALLOWED_SESSION_TRANSITIONS[current.state]:
            raise ValidationError(f"Invalid session transition: {current.state} -> {new_state}")
        if current.state in POST_ACTIVATION_STATES and operator_id is not None and operator_id != current.operator_id:
            raise ValidationError("Session operator_id is immutable once a Session is active.")

        resolved_operator_id = operator_id if operator_id is not None else current.operator_id
        resolved_ended_at = None
        final_state = None
        if new_state in TERMINAL_SESSION_STATES:
            resolved_ended_at = ended_at or _utc_now()
            final_state = new_state

        updated = Session(
            id=current.id,
            title=current.title,
            state=new_state,
            started_at=current.started_at,
            ended_at=resolved_ended_at,
            final_state=final_state,
            operator_id=resolved_operator_id,
            site_id=current.site_id,
            notes=current.notes,
        )
        _validate_session_payload(self.connection, updated)
        with transaction(self.connection):
            self.connection.execute(
                """
                UPDATE sessions
                SET state = ?,
                    ended_at = ?,
                    final_state = ?,
                    operator_id = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?;
                """,
                (updated.state, updated.ended_at, updated.final_state, updated.operator_id, session_id),
            )
        return self.get_session(session_id)


def _validate_session_payload(connection: sqlite3.Connection, session: Session) -> None:
    if not session.id:
        raise ValidationError("Session id is required.")
    if not session.started_at:
        raise ValidationError("Session started_at is required.")
    if session.state not in ALLOWED_SESSION_STATES:
        raise ValidationError(f"Unsupported session state: {session.state}")
    if session.state in POST_ACTIVATION_STATES and not session.operator_id:
        raise ValidationError("Session operator_id is required once a Session is active.")

    is_terminal = session.state in TERMINAL_SESSION_STATES
    if is_terminal:
        if not session.ended_at:
            raise ValidationError("Terminal sessions require ended_at.")
        if session.final_state != session.state:
            raise ValidationError("Terminal session final_state must match state.")
    else:
        if session.ended_at is not None:
            raise ValidationError("Non-terminal sessions cannot have ended_at.")
        if session.final_state is not None:
            raise ValidationError("Non-terminal sessions cannot have final_state.")

    if session.ended_at is not None and session.ended_at < session.started_at:
        raise ValidationError("Session ended_at cannot be earlier than started_at.")
    if session.final_state is not None and session.final_state not in TERMINAL_SESSION_STATES:
        raise ValidationError(f"Unsupported session final_state: {session.final_state}")
    if session.site_id is not None:
        exists = connection.execute("SELECT 1 FROM sites WHERE id = ?;", (session.site_id,)).fetchone()
        if exists is None:
            raise ValidationError(f"Missing site for session: {session.site_id}")


def _session_values(session: Session) -> tuple:
    return (
        session.id,
        session.title,
        session.state,
        session.started_at,
        session.ended_at,
        session.final_state,
        session.operator_id,
        session.site_id,
        session.notes,
    )


def _row_to_session(row: sqlite3.Row) -> Session:
    return Session(
        id=row["id"],
        title=row["title"],
        state=row["state"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        final_state=row["final_state"],
        operator_id=row["operator_id"],
        site_id=row["site_id"],
        notes=row["notes"],
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
