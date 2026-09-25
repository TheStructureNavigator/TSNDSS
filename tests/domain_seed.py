from __future__ import annotations

"""Shared test helper: the canonical Project and Capture that Frames now require."""

import sqlite3

from tsn_dss.engine.sqlite.captures import CaptureRepository
from tsn_dss.engine.sqlite.project_repository import ProjectRepository


def seed_project_and_capture(
    connection: sqlite3.Connection,
    *,
    dir_key: str = "M42",
    capture_name: str = "Night1",
) -> tuple[str, str]:
    """Create a Project and a Capture; return ``(project_id, capture_id)``.

    Frames created in tests use ``rel_path="captures/<capture_name>/..."``.
    """
    project = ProjectRepository(connection).register_project(dir_key=dir_key)
    capture = CaptureRepository(connection).register_capture(
        project_id=project.id,
        name=capture_name,
        source_kind="legacy_registered",
    )
    return project.id, capture.id
