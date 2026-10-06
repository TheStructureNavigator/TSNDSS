from __future__ import annotations

"""Canonical SessionPlan persistence."""

import sqlite3

from ...domain.models import SessionPlan, SessionPlanItem
from .db import transaction
from .planning import ValidationError


class SessionPlanRepository:
    """Persistence for the Session-owned canonical SessionPlan aggregate."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def save_plan(self, plan: SessionPlan) -> SessionPlan:
        self._validate_plan(plan)
        with transaction(self.connection):
            self.connection.execute(
                """
                INSERT OR IGNORE INTO session_plans (session_id)
                VALUES (?);
                """,
                (plan.session_id,),
            )
            self.connection.execute(
                "DELETE FROM session_plan_items WHERE session_id = ?;",
                (plan.session_id,),
            )
            for item in plan.items:
                self.connection.execute(
                    """
                    INSERT INTO session_plan_items (
                        session_id,
                        item_order,
                        target_id,
                        acquisition_plan_id,
                        mosaic_panel_id
                    ) VALUES (?, ?, ?, ?, ?);
                    """,
                    (
                        plan.session_id,
                        item.item_order,
                        item.target_id,
                        item.acquisition_plan_id,
                        item.mosaic_panel_id,
                    ),
                )
        saved = self.get_plan_for_session(plan.session_id)
        if saved is None:  # pragma: no cover - defensive guard for impossible committed state
            raise RuntimeError(f"SessionPlan was not saved for session: {plan.session_id}")
        return saved

    def get_plan_for_session(self, session_id: str) -> SessionPlan | None:
        if not session_id:
            raise ValidationError("Session id is required.")
        row = self.connection.execute(
            "SELECT session_id FROM session_plans WHERE session_id = ?;",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        item_rows = self.connection.execute(
            """
            SELECT id, item_order, target_id, acquisition_plan_id, mosaic_panel_id
            FROM session_plan_items
            WHERE session_id = ?
            ORDER BY
                item_order IS NULL,
                item_order,
                id;
            """,
            (session_id,),
        ).fetchall()
        return SessionPlan(
            session_id=row["session_id"],
            items=[_row_to_item(item_row) for item_row in item_rows],
        )

    def delete_plan_for_session(self, session_id: str) -> None:
        if not session_id:
            raise ValidationError("Session id is required.")
        with transaction(self.connection):
            self.connection.execute("DELETE FROM session_plans WHERE session_id = ?;", (session_id,))

    def _validate_plan(self, plan: SessionPlan) -> None:
        if not plan.session_id:
            raise ValidationError("Session id is required.")
        self._require_session(plan.session_id)
        for item in plan.items:
            self._validate_item(item)

    def _validate_item(self, item: SessionPlanItem) -> None:
        if item.item_order is not None and item.item_order < 0:
            raise ValidationError("SessionPlanItem item_order must be >= 0 when provided.")
        if item.target_id is None and item.acquisition_plan_id is None and item.mosaic_panel_id is None:
            raise ValidationError("SessionPlanItem requires at least one canonical planning anchor.")
        if item.target_id is not None:
            self._require_target(item.target_id)
        acquisition_target_id = None
        if item.acquisition_plan_id is not None:
            acquisition_target_id = self._require_acquisition_plan(item.acquisition_plan_id)
        if item.mosaic_panel_id is not None:
            self._require_mosaic_panel(item.mosaic_panel_id)
        if item.target_id is not None and acquisition_target_id is not None and acquisition_target_id != item.target_id:
            raise ValidationError("SessionPlanItem target_id must match the referenced AcquisitionPlan target_id.")

    def _require_session(self, session_id: str) -> None:
        if self.connection.execute("SELECT 1 FROM sessions WHERE id = ?;", (session_id,)).fetchone() is None:
            raise sqlite3.IntegrityError(f"Missing session for SessionPlan: {session_id}")

    def _require_target(self, target_id: str) -> None:
        if self.connection.execute("SELECT 1 FROM targets WHERE id = ?;", (target_id,)).fetchone() is None:
            raise sqlite3.IntegrityError(f"Missing target for SessionPlanItem: {target_id}")

    def _require_acquisition_plan(self, acquisition_plan_id: str) -> str:
        row = self.connection.execute(
            "SELECT target_id FROM acquisition_plans WHERE id = ?;",
            (acquisition_plan_id,),
        ).fetchone()
        if row is None:
            raise sqlite3.IntegrityError(f"Missing acquisition plan for SessionPlanItem: {acquisition_plan_id}")
        return str(row["target_id"])

    def _require_mosaic_panel(self, mosaic_panel_id: str) -> None:
        if self.connection.execute("SELECT 1 FROM mosaic_panels WHERE id = ?;", (mosaic_panel_id,)).fetchone() is None:
            raise sqlite3.IntegrityError(f"Missing mosaic panel for SessionPlanItem: {mosaic_panel_id}")


def _row_to_item(row: sqlite3.Row) -> SessionPlanItem:
    return SessionPlanItem(
        id=row["id"],
        item_order=row["item_order"],
        target_id=row["target_id"],
        acquisition_plan_id=row["acquisition_plan_id"],
        mosaic_panel_id=row["mosaic_panel_id"],
    )
