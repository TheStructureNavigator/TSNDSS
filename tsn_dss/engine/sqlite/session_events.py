from __future__ import annotations

"""Canonical SessionEvent append-only persistence."""

import sqlite3

from ...domain.models import SessionEvent
from .db import transaction
from .planning import ValidationError

_COLUMNS = "id, session_id, event_type, occurred_at, observation_id, source"


class SessionEventRepository:
    """Append-only persistence for Session-owned historical events."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add_event(self, event: SessionEvent) -> SessionEvent:
        self._validate_event(event)
        try:
            with transaction(self.connection):
                self.connection.execute(
                    f"""
                    INSERT INTO session_events ({_COLUMNS})
                    VALUES (?, ?, ?, ?, ?, ?);
                    """,
                    (
                        event.id,
                        event.session_id,
                        event.event_type,
                        event.occurred_at,
                        event.observation_id,
                        event.source,
                    ),
                )
        except sqlite3.IntegrityError as error:
            if "UNIQUE" in str(error).upper():
                raise ValidationError(f"A SessionEvent with id {event.id!r} already exists.") from error
            raise

        row = self.connection.execute(
            f"SELECT {_COLUMNS} FROM session_events WHERE id = ?;",
            (event.id,),
        ).fetchone()
        if row is None:  # pragma: no cover - defensive guard for impossible committed state
            raise RuntimeError(f"SessionEvent was not saved: {event.id}")
        return _row_to_event(row)

    def list_events(self, session_id: str) -> list[SessionEvent]:
        if not _nonblank(session_id):
            raise ValidationError("Session id is required.")
        rows = self.connection.execute(
            f"""
            SELECT {_COLUMNS}
            FROM session_events
            WHERE session_id = ?
            ORDER BY occurred_at, id;
            """,
            (session_id,),
        ).fetchall()
        return [_row_to_event(row) for row in rows]

    def _validate_event(self, event: SessionEvent) -> None:
        if not _nonblank(event.id):
            raise ValidationError("SessionEvent id is required.")
        if not _nonblank(event.session_id):
            raise ValidationError("SessionEvent session_id is required.")
        if not _nonblank(event.event_type):
            raise ValidationError("SessionEvent event_type is required.")
        if not _nonblank(event.occurred_at):
            raise ValidationError("SessionEvent occurred_at is required.")
        if event.source is not None and not _nonblank(event.source):
            raise ValidationError("SessionEvent source cannot be blank.")

        if self.connection.execute("SELECT 1 FROM sessions WHERE id = ?;", (event.session_id,)).fetchone() is None:
            raise sqlite3.IntegrityError(f"Missing session for SessionEvent: {event.session_id}")

        if event.observation_id is not None:
            row = self.connection.execute(
                "SELECT session_id FROM observations WHERE id = ?;",
                (event.observation_id,),
            ).fetchone()
            if row is None:
                raise sqlite3.IntegrityError(f"Missing observation for SessionEvent: {event.observation_id}")
            if row["session_id"] != event.session_id:
                raise ValidationError("SessionEvent observation_id must belong to the same Session.")


def _row_to_event(row: sqlite3.Row) -> SessionEvent:
    return SessionEvent(
        id=row["id"],
        session_id=row["session_id"],
        event_type=row["event_type"],
        occurred_at=row["occurred_at"],
        observation_id=row["observation_id"],
        source=row["source"],
    )


def _nonblank(value: object) -> bool:
    return value is not None and str(value).strip() != ""
