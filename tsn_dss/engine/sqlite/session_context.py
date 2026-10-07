from __future__ import annotations

"""Canonical SessionContext fact persistence."""

import sqlite3

from ...domain.models import SessionContextFact
from .db import transaction
from .planning import ValidationError

_ALLOWED_EPISTEMIC_KINDS = {"forecast", "observed", "derived", "declared"}
_COLUMNS = "id, session_id, fact_name, epistemic_kind, number_value, text_value, unit, recorded_at, valid_from, source"


class SessionContextRepository:
    """Append-only persistence for Session-owned contextual facts."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add_fact(self, fact: SessionContextFact) -> SessionContextFact:
        self._validate_fact(fact)
        with transaction(self.connection):
            cursor = self.connection.execute(
                """
                INSERT INTO session_context_facts (
                    session_id, fact_name, epistemic_kind, number_value, text_value,
                    unit, recorded_at, valid_from, source
                ) VALUES (?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), ?, ?);
                """,
                (
                    fact.session_id,
                    fact.fact_name,
                    fact.epistemic_kind,
                    fact.number_value,
                    fact.text_value,
                    fact.unit,
                    fact.recorded_at,
                    fact.valid_from,
                    fact.source,
                ),
            )
            fact_id = int(cursor.lastrowid)
        row = self.connection.execute(
            f"SELECT {_COLUMNS} FROM session_context_facts WHERE id = ?;",
            (fact_id,),
        ).fetchone()
        if row is None:  # pragma: no cover - defensive guard for impossible committed state
            raise RuntimeError(f"SessionContextFact was not saved: {fact_id}")
        return _row_to_fact(row)

    def list_facts(self, session_id: str) -> list[SessionContextFact]:
        if not _nonblank(session_id):
            raise ValidationError("Session id is required.")
        rows = self.connection.execute(
            f"""
            SELECT {_COLUMNS}
            FROM session_context_facts
            WHERE session_id = ?
            ORDER BY valid_from IS NULL, valid_from, recorded_at, id;
            """,
            (session_id,),
        ).fetchall()
        return [_row_to_fact(row) for row in rows]

    def _validate_fact(self, fact: SessionContextFact) -> None:
        if not _nonblank(fact.session_id):
            raise ValidationError("Session id is required.")
        if self.connection.execute("SELECT 1 FROM sessions WHERE id = ?;", (fact.session_id,)).fetchone() is None:
            raise sqlite3.IntegrityError(f"Missing session for SessionContextFact: {fact.session_id}")
        if not _nonblank(fact.fact_name):
            raise ValidationError("SessionContextFact fact_name is required.")
        if fact.epistemic_kind not in _ALLOWED_EPISTEMIC_KINDS:
            raise ValidationError(f"Unsupported SessionContextFact epistemic_kind: {fact.epistemic_kind}")

        has_number = fact.number_value is not None
        has_text = fact.text_value is not None
        if has_number == has_text:
            raise ValidationError("SessionContextFact requires exactly one numeric or text value.")
        if has_number and not _nonblank(fact.unit):
            raise ValidationError("Numeric SessionContextFact requires a nonblank unit.")
        if has_text:
            if not _nonblank(fact.text_value):
                raise ValidationError("Text SessionContextFact value cannot be blank.")
            if fact.unit is not None:
                raise ValidationError("Text SessionContextFact cannot have a unit.")
        if fact.epistemic_kind == "forecast" and not _nonblank(fact.valid_from):
            raise ValidationError("Forecast SessionContextFact requires valid_from.")
        if fact.source is not None and not _nonblank(fact.source):
            raise ValidationError("SessionContextFact source cannot be blank.")


def _row_to_fact(row: sqlite3.Row) -> SessionContextFact:
    return SessionContextFact(
        id=row["id"],
        session_id=row["session_id"],
        fact_name=row["fact_name"],
        epistemic_kind=row["epistemic_kind"],
        number_value=row["number_value"],
        text_value=row["text_value"],
        unit=row["unit"],
        recorded_at=row["recorded_at"],
        valid_from=row["valid_from"],
        source=row["source"],
    )


def _nonblank(value: object) -> bool:
    return value is not None and str(value).strip() != ""
