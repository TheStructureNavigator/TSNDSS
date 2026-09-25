from __future__ import annotations

"""Reusable target resolution for astronomy queries.

This module deliberately has no HTTP, GUI, or MCP concepts. Callers translate their own
request shape into these plain keyword arguments.
"""

from dataclasses import dataclass
from typing import Protocol

from .astronomy import AstronomicalTargetContext
from .sqlite.mosaics import MosaicRepository
from .sqlite.planning import PlanningRepository


class PlannedPointingLike(Protocol):
    target_name: str | None
    ra_hours: float
    dec_deg: float
    source_kind: str
    source_id: str | None


@dataclass(frozen=True, slots=True)
class TargetResolutionRequest:
    target_ra_deg: float | None = None
    target_dec_deg: float | None = None
    target_name: str | None = None
    source_kind: str | None = None
    source_id: str | None = None
    mosaic_panel_id: str | None = None
    target_id: str | None = None
    use_planned_pointing: bool = False
    planned_pointing: PlannedPointingLike | None = None


def resolve_astronomical_target_context(
    request: TargetResolutionRequest,
    *,
    planning_repository: PlanningRepository,
    mosaic_repository: MosaicRepository | None = None,
) -> AstronomicalTargetContext | None:
    """Resolve one astronomy target using TSN DSS repositories and runtime pointing state.

    Resolution order intentionally preserves the existing HTTP API behavior:
    explicit RA/Dec, target name lookup, mosaic panel, target id, planned pointing.
    """
    explicit_target_name = _normalize_text(request.target_name)
    if request.target_ra_deg is not None or request.target_dec_deg is not None:
        if request.target_ra_deg is None or request.target_dec_deg is None:
            raise ValueError("Both target_ra_deg and target_dec_deg are required when explicit target coordinates are provided.")
        return AstronomicalTargetContext(
            target_name=explicit_target_name,
            ra_deg=float(request.target_ra_deg),
            dec_deg=float(request.target_dec_deg),
            source_kind=_normalize_text(request.source_kind) or "manual",
            source_id=_normalize_text(request.source_id),
        )

    if explicit_target_name:
        matched_target = planning_repository.find_target_by_query(explicit_target_name)
        if matched_target is not None:
            return AstronomicalTargetContext(
                target_name=matched_target.name,
                ra_deg=matched_target.ra_deg,
                dec_deg=matched_target.dec_deg,
                source_kind="target",
                source_id=matched_target.id,
            )

    mosaic_panel_id = _normalize_text(request.mosaic_panel_id)
    if mosaic_panel_id:
        if mosaic_repository is None:
            raise ValueError("mosaic_repository is required when mosaic_panel_id is provided.")
        panel = mosaic_repository.get_mosaic_panel(mosaic_panel_id)
        if panel is None:
            raise ValueError(f"Unknown mosaic_panel_id: {mosaic_panel_id}")
        return AstronomicalTargetContext(
            target_name=panel.panel_label,
            ra_deg=panel.center_ra_deg,
            dec_deg=panel.center_dec_deg,
            source_kind="mosaic_panel",
            source_id=panel.id,
        )

    target_id = _normalize_text(request.target_id)
    if target_id:
        target = planning_repository.get_target(target_id)
        if target is None:
            raise ValueError(f"Unknown target_id: {target_id}")
        return AstronomicalTargetContext(
            target_name=target.name,
            ra_deg=target.ra_deg,
            dec_deg=target.dec_deg,
            source_kind="target",
            source_id=target.id,
        )

    if request.use_planned_pointing:
        planned = request.planned_pointing
        if planned is None:
            raise ValueError("No planned pointing is available.")
        return AstronomicalTargetContext(
            target_name=planned.target_name,
            ra_deg=planned.ra_hours * 15.0,
            dec_deg=planned.dec_deg,
            source_kind=planned.source_kind,
            source_id=planned.source_id,
        )

    return None


def _normalize_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
