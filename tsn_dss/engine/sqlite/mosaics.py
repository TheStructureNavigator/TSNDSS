from __future__ import annotations

import math
import sqlite3
import uuid

from ...domain.models import MosaicPanel, MosaicPlan
from .db import transaction

ALLOWED_MOSAIC_PLAN_STATUSES = {"draft", "ready", "active", "archived"}
ALLOWED_MOSAIC_PANEL_STATUSES = {"not_started", "in_progress", "complete"}
MAX_GENERATED_PANELS = 256


class ValidationError(ValueError):
    pass


class MosaicRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def save_mosaic_plan(self, plan: MosaicPlan) -> MosaicPlan:
        _validate_mosaic_plan(plan)
        current = self.get_mosaic_plan(plan.id)
        project_id = self._resolve_project_id(plan, current=current)
        with transaction(self.connection):
            if current is not None:
                self.connection.execute(
                    """
                    UPDATE mosaic_plans
                    SET
                        project_slug = ?,
                        name = ?,
                        target_name = ?,
                        observation_type = ?,
                        filter = ?,
                        imaging_profile_id = ?,
                        imaging_profile_label = ?,
                        fov_width_deg = ?,
                        fov_height_deg = ?,
                        center_ra_deg = ?,
                        center_dec_deg = ?,
                        region_width_deg = ?,
                        region_height_deg = ?,
                        rotation_deg = ?,
                        overlap_percent = ?,
                        status = ?,
                        selected_panel_id = ?,
                        project_id = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?;
                    """,
                    (
                        plan.project_slug,
                        plan.name,
                        plan.target_name,
                        plan.observation_type,
                        plan.filter,
                        plan.imaging_profile_id,
                        plan.imaging_profile_label,
                        plan.fov_width_deg,
                        plan.fov_height_deg,
                        plan.center_ra_deg,
                        plan.center_dec_deg,
                        plan.region_width_deg,
                        plan.region_height_deg,
                        plan.rotation_deg,
                        plan.overlap_percent,
                        plan.status,
                        plan.selected_panel_id,
                        project_id,
                        plan.id,
                    ),
                )
            else:
                self.connection.execute(
                    """
                    INSERT INTO mosaic_plans (
                        id,
                        project_slug,
                        name,
                        target_name,
                        observation_type,
                        filter,
                        imaging_profile_id,
                        imaging_profile_label,
                        fov_width_deg,
                        fov_height_deg,
                        center_ra_deg,
                        center_dec_deg,
                        region_width_deg,
                        region_height_deg,
                        rotation_deg,
                        overlap_percent,
                        status,
                        selected_panel_id,
                        project_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        plan.id,
                        plan.project_slug,
                        plan.name,
                        plan.target_name,
                        plan.observation_type,
                        plan.filter,
                        plan.imaging_profile_id,
                        plan.imaging_profile_label,
                        plan.fov_width_deg,
                        plan.fov_height_deg,
                        plan.center_ra_deg,
                        plan.center_dec_deg,
                        plan.region_width_deg,
                        plan.region_height_deg,
                        plan.rotation_deg,
                        plan.overlap_percent,
                        plan.status,
                        plan.selected_panel_id,
                        project_id,
                    ),
                )

            if plan.panels:
                self._replace_panels_locked(plan.id, plan.panels)

        return self.get_mosaic_plan(plan.id)

    def get_mosaic_plan(self, plan_id: str) -> MosaicPlan | None:
        row = self.connection.execute(
            """
            SELECT
                id,
                project_slug,
                name,
                target_name,
                observation_type,
                filter,
                imaging_profile_id,
                imaging_profile_label,
                fov_width_deg,
                fov_height_deg,
                center_ra_deg,
                center_dec_deg,
                region_width_deg,
                region_height_deg,
                rotation_deg,
                overlap_percent,
                status,
                selected_panel_id,
                project_id
            FROM mosaic_plans
            WHERE id = ?;
            """,
            (plan_id,),
        ).fetchone()
        if row is None:
            return None

        panels = self.list_mosaic_panels(plan_id)
        return _row_to_mosaic_plan(row, panels)

    def list_mosaic_plans(self, *, project_slug: str | None = None) -> list[MosaicPlan]:
        if project_slug is None:
            rows = self.connection.execute(
                """
                SELECT
                    id,
                    project_slug,
                    name,
                    target_name,
                    observation_type,
                    filter,
                    imaging_profile_id,
                    imaging_profile_label,
                    fov_width_deg,
                    fov_height_deg,
                    center_ra_deg,
                    center_dec_deg,
                    region_width_deg,
                    region_height_deg,
                    rotation_deg,
                    overlap_percent,
                    status,
                    selected_panel_id,
                    project_id
                FROM mosaic_plans
                ORDER BY created_at DESC, id DESC;
                """
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT
                    id,
                    project_slug,
                    name,
                    target_name,
                    observation_type,
                    filter,
                    imaging_profile_id,
                    imaging_profile_label,
                    fov_width_deg,
                    fov_height_deg,
                    center_ra_deg,
                    center_dec_deg,
                    region_width_deg,
                    region_height_deg,
                    rotation_deg,
                    overlap_percent,
                    status,
                    selected_panel_id,
                    project_id
                FROM mosaic_plans
                WHERE project_slug = ?
                ORDER BY created_at DESC, id DESC;
                """,
                (project_slug,),
            ).fetchall()

        return [_row_to_mosaic_plan(row, self.list_mosaic_panels(row["id"])) for row in rows]

    def _resolve_project_id(self, plan: MosaicPlan, *, current: MosaicPlan | None = None) -> str:
        """Resolve and authorize the canonical Project ownership for a MosaicPlan.

        New canonical plans must resolve to an existing Project. Legacy unresolved plans
        (persisted with project_id NULL) can only resolve from persisted
        project_slug evidence; caller-modified slugs cannot invent historical ownership.
        Linked plans cannot be orphaned or reassigned in Wave 8.
        """
        resolver_slug = plan.project_slug
        if current is not None and current.project_id is None:
            resolver_slug = current.project_slug
            if plan.project_slug != current.project_slug:
                raise ValidationError(
                    "Unresolved legacy mosaic plan project_slug cannot be changed during ownership resolution."
                )

        if plan.project_id is not None:
            row = self.connection.execute(
                "SELECT dir_key FROM projects WHERE id = ?;",
                (plan.project_id,),
            ).fetchone()
            if row is None:
                raise ValidationError(f"Unknown project_id for mosaic plan: {plan.project_id}")
            if row["dir_key"] != resolver_slug:
                raise ValidationError("Mosaic plan project_id and project_slug refer to different projects.")
            requested_project_id = plan.project_id
        else:
            row = self.connection.execute(
                "SELECT id FROM projects WHERE dir_key = ?;",
                (resolver_slug,),
            ).fetchone()
            if row is None:
                raise ValidationError(f"Mosaic plan project_slug does not resolve to a Project: {resolver_slug}")
            requested_project_id = str(row["id"])

        if current is not None and current.project_id is not None and requested_project_id != current.project_id:
            raise ValidationError("Mosaic plan Project ownership cannot be changed in this wave.")
        return requested_project_id

    def _require_mutable_mosaic_plan(self, plan_id: str) -> None:
        row = self.connection.execute(
            "SELECT project_id FROM mosaic_plans WHERE id = ?;",
            (plan_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Mosaic plan not found: {plan_id}")
        if row["project_id"] is None:
            raise ValidationError("Unresolved legacy mosaic plan cannot be modified until Project ownership is resolved.")

    def list_unlinked_plans(self) -> list[tuple[str, str]]:
        """Read-only: ``(plan_id, project_slug)`` for plans that have no project_id yet."""
        return [
            (str(row["id"]), str(row["project_slug"]))
            for row in self.connection.execute(
                "SELECT id, project_slug FROM mosaic_plans WHERE project_id IS NULL ORDER BY id;"
            ).fetchall()
        ]

    def link_plans_to_projects(self) -> tuple[list[str], list[tuple[str, str]]]:
        """Backfill ``project_id`` for unlinked plans by exact ``project_slug == dir_key``.

        Returns ``(linked_plan_ids, unmatched)`` where unmatched lists ``(plan_id,
        project_slug)`` for plans that still have no project. Already-linked plans and
        ``updated_at`` are left untouched.
        """
        with transaction(self.connection):
            matches = self.connection.execute(
                """
                SELECT plan.id AS plan_id, project.id AS project_id
                FROM mosaic_plans AS plan
                JOIN projects AS project ON project.dir_key = plan.project_slug
                WHERE plan.project_id IS NULL
                ORDER BY plan.id;
                """
            ).fetchall()
            for match in matches:
                self.connection.execute(
                    "UPDATE mosaic_plans SET project_id = ? WHERE id = ? AND project_id IS NULL;",
                    (match["project_id"], match["plan_id"]),
                )

        unmatched = [
            (str(row["id"]), str(row["project_slug"]))
            for row in self.connection.execute(
                "SELECT id, project_slug FROM mosaic_plans WHERE project_id IS NULL ORDER BY id;"
            ).fetchall()
        ]
        return [str(match["plan_id"]) for match in matches], unmatched

    def delete_mosaic_plan(self, plan_id: str) -> None:
        self._require_mutable_mosaic_plan(plan_id)
        with transaction(self.connection):
            self.connection.execute("DELETE FROM mosaic_plans WHERE id = ?;", (plan_id,))

    def generate_panels(self, plan_id: str) -> list[MosaicPanel]:
        self._require_mutable_mosaic_plan(plan_id)
        plan = self.get_mosaic_plan(plan_id)
        if plan is None:
            raise KeyError(f"Mosaic plan not found: {plan_id}")

        panels = _generate_regular_mosaic_panels(plan)
        with transaction(self.connection):
            self._replace_panels_locked(plan_id, panels)
            selected_panel_id = panels[0].id if panels else None
            self.connection.execute(
                """
                UPDATE mosaic_plans
                SET selected_panel_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?;
                """,
                (selected_panel_id, plan_id),
            )
        return self.list_mosaic_panels(plan_id)

    def list_mosaic_panels(self, plan_id: str) -> list[MosaicPanel]:
        rows = self.connection.execute(
            """
            SELECT
                id,
                mosaic_plan_id,
                panel_index,
                panel_label,
                center_ra_deg,
                center_dec_deg,
                fov_width_deg,
                fov_height_deg,
                rotation_deg,
                row_index,
                column_index,
                status,
                target_integration_seconds,
                acquired_integration_seconds
            FROM mosaic_panels
            WHERE mosaic_plan_id = ?
            ORDER BY panel_index;
            """,
            (plan_id,),
        ).fetchall()
        return [_row_to_mosaic_panel(row) for row in rows]

    def get_mosaic_panel(self, panel_id: str) -> MosaicPanel | None:
        row = self.connection.execute(
            """
            SELECT
                id,
                mosaic_plan_id,
                panel_index,
                panel_label,
                center_ra_deg,
                center_dec_deg,
                fov_width_deg,
                fov_height_deg,
                rotation_deg,
                row_index,
                column_index,
                status,
                target_integration_seconds,
                acquired_integration_seconds
            FROM mosaic_panels
            WHERE id = ?;
            """,
            (panel_id,),
        ).fetchone()
        return _row_to_mosaic_panel(row) if row else None

    def update_mosaic_panel(self, panel: MosaicPanel) -> MosaicPanel:
        _validate_mosaic_panel(panel)
        row = self.connection.execute(
            "SELECT mosaic_plan_id FROM mosaic_panels WHERE id = ?;",
            (panel.id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Mosaic panel not found: {panel.id}")
        self._require_mutable_mosaic_plan(str(row["mosaic_plan_id"]))
        with transaction(self.connection):
            cursor = self.connection.execute(
                """
                UPDATE mosaic_panels
                SET
                    panel_label = ?,
                    center_ra_deg = ?,
                    center_dec_deg = ?,
                    fov_width_deg = ?,
                    fov_height_deg = ?,
                    rotation_deg = ?,
                    row_index = ?,
                    column_index = ?,
                    status = ?,
                    target_integration_seconds = ?,
                    acquired_integration_seconds = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?;
                """,
                (
                    panel.panel_label,
                    panel.center_ra_deg,
                    panel.center_dec_deg,
                    panel.fov_width_deg,
                    panel.fov_height_deg,
                    panel.rotation_deg,
                    panel.row_index,
                    panel.column_index,
                    panel.status,
                    panel.target_integration_seconds,
                    panel.acquired_integration_seconds,
                    panel.id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Mosaic panel not found: {panel.id}")
        return self.get_mosaic_panel(panel.id)

    def select_active_panel(self, plan_id: str, panel_id: str) -> MosaicPlan:
        panel = self.get_mosaic_panel(panel_id)
        if panel is None or panel.mosaic_plan_id != plan_id:
            raise KeyError(f"Mosaic panel not found for plan: {panel_id}")
        self._require_mutable_mosaic_plan(panel.mosaic_plan_id)

        with transaction(self.connection):
            self.connection.execute(
                """
                UPDATE mosaic_plans
                SET selected_panel_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?;
                """,
                (panel_id, plan_id),
            )
        return self.get_mosaic_plan(plan_id)

    def _replace_panels_locked(self, plan_id: str, panels: list[MosaicPanel]) -> None:
        for panel in panels:
            _validate_mosaic_panel(panel)
            if panel.mosaic_plan_id != plan_id:
                raise ValidationError(
                    f"Mosaic panel {panel.id} belongs to {panel.mosaic_plan_id}, expected {plan_id}."
                )

        self.connection.execute("DELETE FROM mosaic_panels WHERE mosaic_plan_id = ?;", (plan_id,))
        for panel in panels:
            self.connection.execute(
                """
                INSERT INTO mosaic_panels (
                    id,
                    mosaic_plan_id,
                    panel_index,
                    panel_label,
                    center_ra_deg,
                    center_dec_deg,
                    fov_width_deg,
                    fov_height_deg,
                    rotation_deg,
                    row_index,
                    column_index,
                    status,
                    target_integration_seconds,
                    acquired_integration_seconds
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    panel.id,
                    panel.mosaic_plan_id,
                    panel.panel_index,
                    panel.panel_label,
                    panel.center_ra_deg,
                    panel.center_dec_deg,
                    panel.fov_width_deg,
                    panel.fov_height_deg,
                    panel.rotation_deg,
                    panel.row_index,
                    panel.column_index,
                    panel.status,
                    panel.target_integration_seconds,
                    panel.acquired_integration_seconds,
                ),
            )


def _validate_mosaic_plan(plan: MosaicPlan) -> None:
    if not plan.id or not plan.project_slug or not plan.name:
        raise ValidationError("Mosaic plan id, project_slug and name are required.")
    if not plan.imaging_profile_id or not plan.imaging_profile_label:
        raise ValidationError("Mosaic plan imaging profile id and label are required.")
    if plan.status not in ALLOWED_MOSAIC_PLAN_STATUSES:
        raise ValidationError(f"Unsupported mosaic plan status: {plan.status}")
    if not 0 <= plan.center_ra_deg < 360:
        raise ValidationError("Mosaic plan center_ra_deg must be in [0, 360).")
    if not -90 <= plan.center_dec_deg <= 90:
        raise ValidationError("Mosaic plan center_dec_deg must be in [-90, 90].")
    if plan.fov_width_deg <= 0 or plan.fov_height_deg <= 0:
        raise ValidationError("Mosaic plan FOV values must be > 0.")
    if plan.region_width_deg <= 0 or plan.region_height_deg <= 0:
        raise ValidationError("Mosaic plan region dimensions must be > 0.")
    if not 0 <= plan.overlap_percent < 100:
        raise ValidationError("Mosaic plan overlap_percent must be in [0, 100).")

    panel_ids: set[str] = set()
    for panel in plan.panels:
        _validate_mosaic_panel(panel)
        if panel.id in panel_ids:
            raise ValidationError(f"Duplicate mosaic panel id: {panel.id}")
        panel_ids.add(panel.id)
        if panel.mosaic_plan_id != plan.id:
            raise ValidationError(
                f"Mosaic panel {panel.id} belongs to {panel.mosaic_plan_id}, expected {plan.id}."
            )

    if plan.selected_panel_id is not None and plan.panels:
        if plan.selected_panel_id not in panel_ids:
            raise ValidationError("selected_panel_id must match one of the provided panels.")


def _validate_mosaic_panel(panel: MosaicPanel) -> None:
    if not panel.id or not panel.mosaic_plan_id or not panel.panel_label:
        raise ValidationError("Mosaic panel id, mosaic_plan_id and panel_label are required.")
    if panel.panel_index < 0:
        raise ValidationError("Mosaic panel panel_index must be >= 0.")
    if not 0 <= panel.center_ra_deg < 360:
        raise ValidationError("Mosaic panel center_ra_deg must be in [0, 360).")
    if not -90 <= panel.center_dec_deg <= 90:
        raise ValidationError("Mosaic panel center_dec_deg must be in [-90, 90].")
    if panel.fov_width_deg <= 0 or panel.fov_height_deg <= 0:
        raise ValidationError("Mosaic panel FOV values must be > 0.")
    if panel.status not in ALLOWED_MOSAIC_PANEL_STATUSES:
        raise ValidationError(f"Unsupported mosaic panel status: {panel.status}")
    if panel.target_integration_seconds is not None and panel.target_integration_seconds < 0:
        raise ValidationError("Mosaic panel target_integration_seconds must be >= 0.")
    if panel.acquired_integration_seconds is not None and panel.acquired_integration_seconds < 0:
        raise ValidationError("Mosaic panel acquired_integration_seconds must be >= 0.")


def _generate_regular_mosaic_panels(plan: MosaicPlan) -> list[MosaicPanel]:
    step_x = plan.fov_width_deg * (1.0 - plan.overlap_percent / 100.0)
    step_y = plan.fov_height_deg * (1.0 - plan.overlap_percent / 100.0)
    if step_x <= 0 or step_y <= 0:
        raise ValidationError("Mosaic plan overlap leaves no usable panel step.")

    column_count = _compute_axis_count(plan.region_width_deg, plan.fov_width_deg, step_x)
    row_count = _compute_axis_count(plan.region_height_deg, plan.fov_height_deg, step_y)
    panel_count = column_count * row_count
    if panel_count > MAX_GENERATED_PANELS:
        raise ValidationError(
            f"Mosaic plan would generate {panel_count} panels, which exceeds the safety limit of {MAX_GENERATED_PANELS}."
        )

    x_offsets = _centered_offsets(column_count, step_x)
    y_offsets = _centered_offsets(row_count, step_y)

    panels: list[MosaicPanel] = []
    panel_index = 0
    for row_index, y_offset in enumerate(y_offsets):
        panel_dec = max(-90.0, min(90.0, plan.center_dec_deg + y_offset))
        cos_dec = max(math.cos(math.radians(panel_dec)), 1e-6)
        for column_index, x_offset in enumerate(x_offsets):
            panel_ra = _normalize_ra_deg(plan.center_ra_deg + (x_offset / cos_dec))
            panel_label = f"P{panel_index + 1:02d}"
            panels.append(
                MosaicPanel(
                    id=f"mosaic_panel:{uuid.uuid4().hex[:12]}",
                    mosaic_plan_id=plan.id,
                    panel_index=panel_index,
                    panel_label=panel_label,
                    center_ra_deg=panel_ra,
                    center_dec_deg=panel_dec,
                    fov_width_deg=plan.fov_width_deg,
                    fov_height_deg=plan.fov_height_deg,
                    rotation_deg=plan.rotation_deg,
                    row_index=row_index,
                    column_index=column_index,
                )
            )
            panel_index += 1
    return panels


def _compute_axis_count(region_size_deg: float, fov_size_deg: float, step_deg: float) -> int:
    if region_size_deg <= fov_size_deg:
        return 1
    return max(1, int(math.ceil((region_size_deg - fov_size_deg) / step_deg)) + 1)


def _centered_offsets(count: int, step_deg: float) -> list[float]:
    center_index = (count - 1) / 2.0
    return [(index - center_index) * step_deg for index in range(count)]


def _normalize_ra_deg(value: float) -> float:
    normalized = value % 360.0
    if normalized < 0:
        normalized += 360.0
    return normalized


def _row_to_mosaic_plan(row: sqlite3.Row, panels: list[MosaicPanel]) -> MosaicPlan:
    return MosaicPlan(
        id=row["id"],
        project_slug=row["project_slug"],
        name=row["name"],
        target_name=row["target_name"],
        observation_type=row["observation_type"],
        filter=row["filter"],
        imaging_profile_id=row["imaging_profile_id"],
        imaging_profile_label=row["imaging_profile_label"],
        fov_width_deg=row["fov_width_deg"],
        fov_height_deg=row["fov_height_deg"],
        center_ra_deg=row["center_ra_deg"],
        center_dec_deg=row["center_dec_deg"],
        region_width_deg=row["region_width_deg"],
        region_height_deg=row["region_height_deg"],
        rotation_deg=row["rotation_deg"],
        overlap_percent=row["overlap_percent"],
        status=row["status"],
        selected_panel_id=row["selected_panel_id"],
        panels=panels,
        project_id=row["project_id"],
    )


def _row_to_mosaic_panel(row: sqlite3.Row) -> MosaicPanel:
    return MosaicPanel(
        id=row["id"],
        mosaic_plan_id=row["mosaic_plan_id"],
        panel_index=row["panel_index"],
        panel_label=row["panel_label"],
        center_ra_deg=row["center_ra_deg"],
        center_dec_deg=row["center_dec_deg"],
        fov_width_deg=row["fov_width_deg"],
        fov_height_deg=row["fov_height_deg"],
        rotation_deg=row["rotation_deg"],
        row_index=row["row_index"],
        column_index=row["column_index"],
        status=row["status"],
        target_integration_seconds=row["target_integration_seconds"],
        acquired_integration_seconds=row["acquired_integration_seconds"],
    )
