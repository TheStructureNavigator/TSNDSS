from __future__ import annotations

"""Keep canonical Project records (SQLite) and the filesystem Project workflow in step.

Responsibilities are split on purpose:

- ``ProjectStorage`` (engine/projects.py) owns the physical layout on disk.
- ``ProjectRepository`` (engine/sqlite/project_repository.py) owns canonical metadata.
- ``ProjectRegistry`` (this module) only decides ordering and compensation when both
  must change, and scans the filesystem *read-only* to register existing projects.

During S2 the directory name (``dir_key``) is still the identifier used by the HTTP API.
``project.json`` remains a compatibility mirror of the canonical ``target_label``.
"""

import json
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ..domain.models import Project
from .projects import ProjectStorage, ProjectSummary
from .sqlite.mosaics import MosaicRepository
from .sqlite.project_repository import ProjectInUseError, ProjectRepository

__all__ = [
    "MetadataConflict",
    "ProjectInUseError",
    "ProjectRegistry",
    "RegistrationReport",
    "FilesystemProject",
    "scan_filesystem_projects",
    "validate_dir_key",
]


@dataclass(frozen=True, slots=True)
class FilesystemProject:
    """What a read-only scan learned about one directory under the projects root."""

    dir_key: str
    target_label: str | None
    project_json: str  # absent | ok | malformed | not_an_object | unreadable


@dataclass(frozen=True, slots=True)
class MetadataConflict:
    """Canonical metadata and the project.json mirror disagree. The database wins."""

    dir_key: str
    field: str
    database_value: str | None
    project_json_value: str | None


@dataclass(slots=True)
class RegistrationReport:
    created: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    conflicts: list[MetadataConflict] = field(default_factory=list)
    filesystem_only: list[str] = field(default_factory=list)
    db_only: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    mosaic_linked: list[str] = field(default_factory=list)
    mosaic_unmatched: list[tuple[str, str]] = field(default_factory=list)
    dry_run: bool = False

    @property
    def needs_attention(self) -> bool:
        return bool(self.conflicts or self.db_only or self.warnings or self.mosaic_unmatched)

    def summary(self) -> str:
        prefix = "would register" if self.dry_run else "registered"
        return (
            f"projects: {prefix} {len(self.filesystem_only) if self.dry_run else len(self.created)}, "
            f"unchanged {len(self.unchanged)}, metadata conflicts {len(self.conflicts)}, "
            f"database-only {len(self.db_only)}; mosaic plans linked {len(self.mosaic_linked)}, "
            f"unmatched {len(self.mosaic_unmatched)}"
        )


def validate_dir_key(dir_key: str) -> str:
    """A dir_key is exactly one directory name under the projects root."""
    if not isinstance(dir_key, str) or not dir_key.strip():
        raise ValueError("Project name must not be empty.")
    if "\x00" in dir_key or "/" in dir_key or "\\" in dir_key or dir_key in {".", ".."}:
        raise ValueError(f"Project name must be a single directory name: {dir_key!r}")
    path = Path(dir_key)
    if path.name != dir_key or path.is_absolute() or path.drive:
        raise ValueError(f"Project name must be a single directory name: {dir_key!r}")
    return dir_key


def scan_filesystem_projects(projects_root: Path) -> list[FilesystemProject]:
    """List project directories and their ``sky_target`` without touching anything.

    Deliberately independent of ``ProjectStorage.list_projects``, which creates
    ``captures/`` and ``runs/`` folders as a side effect. Every subdirectory of the root
    is a project, matching what the application lists. Non-directories are ignored.
    """
    if not projects_root.is_dir():
        return []
    return [
        _read_project_directory(child)
        for child in sorted(projects_root.iterdir(), key=lambda path: path.name)
        if child.is_dir()
    ]


def _read_project_directory(directory: Path) -> FilesystemProject:
    metadata_path = directory / "project.json"
    if not metadata_path.is_file():
        return FilesystemProject(directory.name, None, "absent")
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return FilesystemProject(directory.name, None, "malformed")
    except (OSError, UnicodeDecodeError):
        return FilesystemProject(directory.name, None, "unreadable")
    if not isinstance(payload, dict):
        return FilesystemProject(directory.name, None, "not_an_object")

    sky_target = payload.get("sky_target")
    # Same rule as ProjectStorage: a non-blank string is a target, copied verbatim.
    label = sky_target if isinstance(sky_target, str) and sky_target.strip() else None
    return FilesystemProject(directory.name, label, "ok")


class ProjectRegistry:
    def __init__(self, storage: ProjectStorage, connection: sqlite3.Connection) -> None:
        self.storage = storage
        self.connection = connection
        self.projects = ProjectRepository(connection)
        self.mosaics = MosaicRepository(connection)

    # -- registration of existing filesystem projects -------------------------------------

    def inspect(self) -> RegistrationReport:
        """Report what registration would do. Writes nothing to the database or the filesystem."""
        return self._reconcile(dry_run=True)

    def register_existing(self) -> RegistrationReport:
        """Register every unregistered project directory. Idempotent.

        Only the database is written. Canonical metadata is never overwritten from
        project.json; disagreements are reported as conflicts. Then unlinked mosaic plans are
        linked by exact ``project_slug == dir_key``.
        """
        return self._reconcile(dry_run=False)

    def _reconcile(self, *, dry_run: bool) -> RegistrationReport:
        report = RegistrationReport(dry_run=dry_run)
        scanned = scan_filesystem_projects(self.storage.projects_root)
        existing = {project.dir_key: project for project in self.projects.list_projects()}

        for entry in scanned:
            if entry.project_json in {"malformed", "not_an_object", "unreadable"}:
                report.warnings.append(f"{entry.dir_key}: project.json is {entry.project_json}; no target imported.")

            record = existing.get(entry.dir_key)
            if record is None:
                report.filesystem_only.append(entry.dir_key)
                if not dry_run:
                    self.projects.register_project(dir_key=entry.dir_key, target_label=entry.target_label)
                    report.created.append(entry.dir_key)
            elif record.target_label != entry.target_label:
                report.conflicts.append(
                    MetadataConflict(
                        dir_key=entry.dir_key,
                        field="target_label",
                        database_value=record.target_label,
                        project_json_value=entry.target_label,
                    )
                )
            else:
                report.unchanged.append(entry.dir_key)

        scanned_keys = {entry.dir_key for entry in scanned}
        report.db_only = sorted(key for key in existing if key not in scanned_keys)

        if dry_run:
            known = scanned_keys | set(existing)
            for plan_id, slug in self.mosaics.list_unlinked_plans():
                if slug in known:
                    report.mosaic_linked.append(plan_id)
                else:
                    report.mosaic_unmatched.append((plan_id, slug))
        else:
            report.mosaic_linked, report.mosaic_unmatched = self.mosaics.link_plans_to_projects()
        return report

    def ensure_registered(self, dir_key: str) -> Project:
        """Return the canonical record for an existing directory, registering it if needed."""
        validate_dir_key(dir_key)
        record = self.projects.get_project_by_dir_key(dir_key)
        if record is not None:
            return record
        directory = self.storage.projects_root / dir_key
        if not directory.is_dir():
            raise FileNotFoundError(f"Project directory not found: {dir_key}")
        entry = _read_project_directory(directory)
        return self.projects.register_project(dir_key=dir_key, target_label=entry.target_label)

    # -- transitional dual-write --------------------------------------------------------------

    def create_project(self, dir_key: str) -> tuple[ProjectSummary, Project]:
        """Create (or return) a project on disk and its canonical record.

        Filesystem first, then the record, so a failure never leaves a record without a
        directory. If the record cannot be written, a directory created by this very call is
        removed again (only when it holds nothing but empty folders); a directory that
        already existed is left alone and the error is raised. A directory left without a
        record is registered by the next ``register_existing`` run.
        """
        validate_dir_key(dir_key)
        directory = self.storage.projects_root / dir_key
        existed = directory.exists()

        try:
            summary = self.storage.create_project(dir_key)
            record = self.projects.get_project_by_dir_key(dir_key)
            if record is None:
                entry = _read_project_directory(directory)
                record = self.projects.register_project(dir_key=dir_key, target_label=entry.target_label)
        except BaseException:
            if not existed:
                _remove_if_only_empty_directories(directory)
            raise
        return summary, record

    def set_target_label(self, dir_key: str, sky_target: str | None) -> tuple[ProjectSummary, Project]:
        """Change the project's target label: canonical record first, project.json mirror second.

        If the mirror cannot be written, the canonical value is restored and the error is
        raised, so the two never silently diverge. ``target_id`` is never touched.
        """
        validate_dir_key(dir_key)
        label = (sky_target.strip() or None) if isinstance(sky_target, str) else None

        record = self.projects.get_project_by_dir_key(dir_key)
        if record is None:
            self.storage.ensure_project(dir_key)  # as the existing behaviour: creates a missing project
            record = self.ensure_registered(dir_key)

        previous = record.target_label
        record = self.projects.set_target_label(record.id, label)
        try:
            summary = self.storage.set_project_sky_target(dir_key, label)
        except BaseException:
            self.projects.set_target_label(record.id, previous)
            raise
        return summary, record

    def delete_project(self, dir_key: str) -> None:
        """Delete a project, refusing while canonical records depend on it.

        Raises ``ProjectInUseError`` (nothing is changed) if MosaicPlans reference the project,
        and ``FileNotFoundError`` if there is neither a directory nor a record.

        Order: dependents check, then the directory, then the record. If removing the directory
        fails, the record stays and describes a project that still exists. If the record cannot
        be deleted afterwards, it remains as a database-only project that ``inspect`` reports,
        and a retry finishes the job. Identity therefore never disappears while dependents remain.

        S2 limit: captures and runs are not canonical records yet, so their files are deleted
        with the directory exactly as before.
        """
        validate_dir_key(dir_key)
        record = self.projects.get_project_by_dir_key(dir_key)
        if record is not None:
            dependents = self.projects.count_mosaic_plans(record.id)
            if dependents:
                raise ProjectInUseError(record.id, dependents)

        if (self.storage.projects_root / dir_key).is_dir():
            self.storage.delete_project(dir_key)
        elif record is None:
            raise FileNotFoundError(f"Project not found: {dir_key}")

        if record is not None:
            self.projects.delete_project(record.id)


def _remove_if_only_empty_directories(directory: Path) -> None:
    """Best-effort cleanup of a directory this process just created."""
    try:
        if not directory.is_dir():
            return
        for _, _, files in os.walk(directory):
            if files:
                return
        shutil.rmtree(directory)
    except OSError:
        pass  # the leftover directory is registered by the next register_existing run
