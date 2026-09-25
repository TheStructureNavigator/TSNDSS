from __future__ import annotations

"""Register Captures and Frames into the canonical SQLite domain. Read-only on the files.

SQLite owns identity, metadata and provenance; the filesystem owns the bytes. The registrar only
ever *reads* existing capture contents (it opens files for reading, lists directories and
parses FITS headers) and writes to the database. It never creates, repairs, moves, renames or
rewrites anything under a project, and it stays inside the ARCH-4.2 capture boundary.

Rules
    - Only observational source formats become Frames (``FRAME_FORMATS``); everything else is
      ignored and reported by suffix.
    - frame_type comes from the capture's top-level folder (biases/darks/flats/lights) with the
      basis recorded in ``metadata_json.registration``; FITS ``IMAGETYP`` is used only when the
      folder says nothing. Otherwise it stays NULL.
    - Files are hashed with SHA-256. A recorded hash is immutable: if a registered path now holds
      different bytes it is reported as a conflict and nothing is updated.
    - Duplicate content is allowed and reported. Nothing is deduplicated or deleted.
    - A second run over unchanged data creates nothing. Unchanged files are recognised by size
      and modification time (kept in the registration metadata only for change detection; never
      used as an acquisition time) and are not re-read unless ``verify_hashes`` is set.
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from ..domain.models import Capture, Frame, Project
from .fits_metadata import IMAGETYP_FRAME_TYPES, FitsReadError, read_fits_metadata
from .project_registry import scan_filesystem_projects
from .projects import ProjectStorage, path_is_within, validate_capture_name
from .sqlite.captures import CaptureRepository
from .sqlite.frames import FrameRepository
from .sqlite.migrations import CURRENT_SCHEMA_VERSION, get_user_version
from .sqlite.project_repository import ProjectRepository

REGISTRAR_VERSION = "capture-registrar/1"

# The explicit accepted vocabulary of observational source formats (case-insensitive suffixes).
FRAME_FORMATS = {".fit": "fits", ".fits": "fits", ".fts": "fits", ".cr2": "cr2"}
# The DSLR/Siril folder convention: capture-level folder -> frame type.
DIRECTORY_FRAME_TYPES = {"biases": "bias", "darks": "dark", "flats": "flat", "lights": "light"}
COVERAGE_FIELDS = (
    "captured_at", "exposure_s", "gain", "offset_value", "filter_name", "instrument_name",
    "width_px", "height_px", "binning_x", "binning_y", "camera_temp_c",
)
_CHUNK = 1024 * 1024


@dataclass(slots=True)
class ChangedFileConflict:
    """A registered path now holds different bytes. The recorded identity is left untouched."""

    project: str
    rel_path: str
    recorded_sha256: str | None
    current_sha256: str
    recorded_size: int | None
    current_size: int


@dataclass(slots=True)
class CaptureRegistrationReport:
    dry_run: bool = False
    verify_hashes: bool = False
    projects_seen: list[str] = field(default_factory=list)
    projects_skipped: list[str] = field(default_factory=list)        # canonical, but the directory is missing
    filesystem_only_projects: list[str] = field(default_factory=list)  # directory without a canonical record
    captures_created: list[str] = field(default_factory=list)
    captures_unchanged: list[str] = field(default_factory=list)
    filesystem_only_captures: list[str] = field(default_factory=list)  # dry run: would be created
    db_only_captures: list[str] = field(default_factory=list)          # record without a usable directory
    frames_created: int = 0
    frames_unchanged: int = 0
    hashes_recorded: int = 0
    frames_by_format: Counter = field(default_factory=Counter)
    frames_by_type: Counter = field(default_factory=Counter)
    fits_frames: int = 0
    fits_field_coverage: Counter = field(default_factory=Counter)
    conflicts: list[ChangedFileConflict] = field(default_factory=list)
    duplicate_hashes: dict[str, list[str]] = field(default_factory=dict)
    ignored_suffixes: Counter = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)
    unreadable_files: list[str] = field(default_factory=list)
    outside_boundary: list[str] = field(default_factory=list)
    missing_files: list[str] = field(default_factory=list)             # registered frame, file gone

    @property
    def needs_attention(self) -> bool:
        return bool(
            self.conflicts or self.warnings or self.unreadable_files or self.outside_boundary
            or self.db_only_captures or self.missing_files or self.projects_skipped
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("frames_by_format", "frames_by_type", "fits_field_coverage", "ignored_suffixes"):
            data[key] = dict(getattr(self, key))
        return data

    def summary(self) -> str:
        verb = "would create" if self.dry_run else "created"
        captures = self.filesystem_only_captures if self.dry_run else self.captures_created
        return (
            f"captures: {verb} {len(captures)}, unchanged {len(self.captures_unchanged)}; "
            f"frames: {verb} {self.frames_created}, unchanged {self.frames_unchanged}, "
            f"hashes recorded {self.hashes_recorded}; conflicts {len(self.conflicts)}, "
            f"duplicate-content groups {len(self.duplicate_hashes)}, ignored files {sum(self.ignored_suffixes.values())}, "
            f"warnings {len(self.warnings)}"
        )


@dataclass(slots=True)
class _Found:
    path: Path
    rel_path: str
    file_format: str
    size: int
    mtime_ns: int


class CaptureRegistrar:
    def __init__(self, storage: ProjectStorage, connection: sqlite3.Connection) -> None:
        self.storage = storage
        self.connection = connection
        self.projects = ProjectRepository(connection)
        self.captures = CaptureRepository(connection)
        self.frames = FrameRepository(connection)
        # Dry runs store nothing, so frames they *would* create are tracked here for duplicate reporting.
        self._planned_hashes: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))

    # -- entry points -----------------------------------------------------------------------------

    def register_all(self, *, dry_run: bool = False, verify_hashes: bool = False) -> CaptureRegistrationReport:
        """Register every capture of every canonical project. Idempotent."""
        report = CaptureRegistrationReport(dry_run=dry_run, verify_hashes=verify_hashes)
        self._planned_hashes.clear()
        canonical = self.projects.list_projects()
        known = {project.dir_key for project in canonical}
        report.filesystem_only_projects = [
            entry.dir_key for entry in scan_filesystem_projects(self.storage.projects_root) if entry.dir_key not in known
        ]
        for project in canonical:
            self._register_project(project, report)
        return report

    def register_project(
        self, dir_key: str, *, dry_run: bool = False, verify_hashes: bool = False
    ) -> CaptureRegistrationReport:
        project = self.projects.get_project_by_dir_key(dir_key)
        if project is None:
            raise KeyError(f"Project is not registered: {dir_key}")
        report = CaptureRegistrationReport(dry_run=dry_run, verify_hashes=verify_hashes)
        self._planned_hashes.clear()
        self._register_project(project, report)
        return report

    def register_imported_capture(
        self,
        dir_key: str,
        capture_name: str,
        *,
        source_dir: str | Path,
        move: bool,
    ) -> CaptureRegistrationReport:
        """Register a capture that was just imported by ``ProjectStorage.import_capture``.

        Only the database is written. A failure here never touches the imported files; the
        registrar can be run again to repair the metadata.
        """
        project = self.projects.get_project_by_dir_key(dir_key)
        if project is None:
            raise KeyError(f"Project is not registered: {dir_key}")
        report = CaptureRegistrationReport()
        report.projects_seen.append(dir_key)
        self._register_capture(
            project,
            capture_name,
            report,
            capture_defaults={
                "source_kind": "folder_import",
                "source_path": str(source_dir),
                "import_mode": "move" if move else "copy",
                "imported_at": _utc_now(),
            },
        )
        self._collect_duplicates(project, report)
        return report

    # -- per project / capture ----------------------------------------------------------------------

    def _register_project(self, project: Project, report: CaptureRegistrationReport) -> None:
        try:
            layout = self.storage.locate_project(project.dir_key)
        except FileNotFoundError:
            report.projects_skipped.append(project.dir_key)
            report.db_only_captures.extend(
                f"{project.dir_key}/{capture.name}" for capture in self.captures.list_captures(project_id=project.id)
            )
            return
        report.projects_seen.append(project.dir_key)

        on_disk: list[str] = []
        if layout.captures_dir.is_dir():
            for child in sorted(layout.captures_dir.iterdir(), key=lambda path: path.name):
                if not child.is_dir():
                    continue
                try:
                    validate_capture_name(child.name)
                except ValueError:
                    report.warnings.append(f"{project.dir_key}: skipped directory with an invalid capture name {child.name!r}")
                    continue
                on_disk.append(child.name)

        for name in on_disk:
            self._register_capture(project, name, report)

        for capture in self.captures.list_captures(project_id=project.id):
            if capture.name not in on_disk:
                report.db_only_captures.append(f"{project.dir_key}/{capture.name}")
        self._collect_duplicates(project, report)

    def _register_capture(
        self,
        project: Project,
        capture_name: str,
        report: CaptureRegistrationReport,
        *,
        capture_defaults: dict[str, Any] | None = None,
    ) -> None:
        label = f"{project.dir_key}/{capture_name}"
        try:
            capture_root = self.storage.resolve_capture_root(project.dir_key, capture_name)
        except FileNotFoundError:
            report.outside_boundary.append(label)
            report.warnings.append(f"{label}: capture does not resolve to a directory inside captures/; skipped")
            return
        unresolved_root = self.storage.project_layout(project.dir_key).captures_dir / capture_name

        capture = self.captures.get_capture_by_name(project.id, capture_name)
        existing_by_path = (
            {frame.rel_path: frame for frame in self.frames.list_frames(capture_id=capture.id)} if capture else {}
        )

        found = self._scan_capture(unresolved_root, capture_root, capture_name, label, report)
        new_frames: list[Frame] = []
        hashes_to_record: list[tuple[int, str, int]] = []
        seen_paths: set[str] = set()

        for item in found:
            seen_paths.add(item.rel_path)
            record = existing_by_path.get(item.rel_path)
            if record is None:
                frame = self._build_frame(project, item, label, report)
                if frame is not None:
                    new_frames.append(frame)
                continue
            self._check_existing(project, record, item, label, report, hashes_to_record)

        for rel_path in existing_by_path:
            if rel_path not in seen_paths:
                report.missing_files.append(f"{project.dir_key}/{rel_path}")

        if capture is None:
            if report.dry_run:
                report.filesystem_only_captures.append(label)
            else:
                defaults = {"source_kind": "legacy_registered"} | (capture_defaults or {})
                capture = self.captures.register_capture(
                    project_id=project.id,
                    name=capture_name,
                    registrar_version=REGISTRAR_VERSION,
                    **defaults,
                )
                report.captures_created.append(label)
        else:
            report.captures_unchanged.append(label)

        for frame in new_frames:
            self._tally(frame, report)
            if report.dry_run:
                self._planned_hashes[project.id][frame.content_sha256].append(frame.rel_path)
            else:
                frame.capture_id = capture.id
                self.frames.create_frame(frame)
            report.frames_created += 1
        for frame_id, digest, size in hashes_to_record:
            if not report.dry_run:
                self.frames.record_content_hash(frame_id, content_sha256=digest, size_bytes=size)
            report.hashes_recorded += 1

    # -- scanning ---------------------------------------------------------------------------------

    @staticmethod
    def _scan_capture(
        unresolved_root: Path,
        resolved_root: Path,
        capture_name: str,
        label: str,
        report: CaptureRegistrationReport,
    ) -> list[_Found]:
        """Every observational file inside the capture. Directories are only listed, never changed."""
        found: list[_Found] = []
        visited: set[Path] = {resolved_root}
        stack = [unresolved_root]
        while stack:
            directory = stack.pop()
            try:
                children = sorted(os.scandir(directory), key=lambda entry: entry.name)
            except OSError as error:
                report.warnings.append(f"{label}: cannot list {directory.name!r}: {error}")
                continue
            for entry in children:
                path = Path(entry.path)
                resolved = path.resolve()
                if not path_is_within(resolved, resolved_root):
                    report.outside_boundary.append(f"{label}/{path.relative_to(unresolved_root).as_posix()}")
                    continue
                if path.is_dir():
                    if resolved not in visited:
                        visited.add(resolved)
                        stack.append(path)
                    continue
                if not path.is_file():
                    continue
                file_format = FRAME_FORMATS.get(path.suffix.lower())
                if file_format is None:
                    report.ignored_suffixes[path.suffix.lower() or "(none)"] += 1
                    continue
                try:
                    stat = path.stat()
                except OSError as error:
                    report.unreadable_files.append(f"{label}/{path.name}: {error}")
                    continue
                rel_path = PurePosixPath("captures", capture_name, *path.relative_to(unresolved_root).parts).as_posix()
                found.append(_Found(path, rel_path, file_format, stat.st_size, stat.st_mtime_ns))
        return sorted(found, key=lambda item: item.rel_path)

    # -- frames -----------------------------------------------------------------------------------

    def _build_frame(
        self,
        project: Project,
        item: _Found,
        label: str,
        report: CaptureRegistrationReport,
    ) -> Frame | None:
        try:
            digest, size, stable = _sha256_stable(item.path)
        except OSError as error:
            report.unreadable_files.append(f"{project.dir_key}/{item.rel_path}: {error}")
            return None
        if not stable:
            report.warnings.append(f"{project.dir_key}/{item.rel_path}: file changed while being read; skipped this run")
            return None

        registration: dict[str, Any] = {
            "registrar_version": REGISTRAR_VERSION,
            "format_basis": f"suffix:{item.path.suffix}",
            "mtime_ns": item.mtime_ns,
        }
        metadata: dict[str, Any] = {"registration": registration}
        normalized: dict[str, Any] = {}
        imagetyp: str | None = None
        origin = "raw" if item.file_format == "cr2" else "unknown"

        if item.file_format == "fits":
            try:
                parsed = read_fits_metadata(item.path)
            except FitsReadError as error:
                registration["parse_error"] = str(error)
                report.warnings.append(f"{project.dir_key}/{item.rel_path}: could not read FITS metadata ({error})")
            else:
                normalized, imagetyp = parsed.fields, parsed.imagetyp
                metadata["fits"] = {"hdu": 0, "cards": parsed.header, "truncated": parsed.truncated}
                if parsed.warnings:
                    registration["warnings"] = parsed.warnings
                    report.warnings.extend(f"{project.dir_key}/{item.rel_path}: {text}" for text in parsed.warnings)

        frame_type, basis = _frame_type_from_directory(item.rel_path)
        if frame_type is None and imagetyp is not None:
            mapped = IMAGETYP_FRAME_TYPES.get(imagetyp.strip().lower())
            if mapped is not None:
                frame_type, basis = mapped, "fits:IMAGETYP"
        elif frame_type is not None and imagetyp is not None:
            mapped = IMAGETYP_FRAME_TYPES.get(imagetyp.strip().lower())
            if mapped is not None and mapped != frame_type:
                registration["frame_type_conflict"] = {"directory": frame_type, "fits_imagetyp": imagetyp}
                report.warnings.append(
                    f"{project.dir_key}/{item.rel_path}: folder says {frame_type!r} but FITS IMAGETYP is {imagetyp!r}; "
                    f"kept the folder-based type"
                )
        registration["frame_type_basis"] = basis

        return Frame(
            project_id=project.id,
            capture_id="",  # set once the capture record exists
            rel_path=item.rel_path,
            frame_type=frame_type,
            origin=origin,
            file_format=item.file_format,
            size_bytes=size,
            content_sha256=digest,
            hashed_at=_utc_now(),
            metadata=metadata,
            **normalized,
        )

    def _check_existing(
        self,
        project: Project,
        record: Frame,
        item: _Found,
        label: str,
        report: CaptureRegistrationReport,
        hashes_to_record: list[tuple[int, str, int]],
    ) -> None:
        registration = record.metadata.get("registration", {}) if isinstance(record.metadata, dict) else {}
        cheap_match = (
            not report.verify_hashes
            and record.content_sha256 is not None
            and record.size_bytes == item.size
            and registration.get("mtime_ns") == item.mtime_ns
        )
        if cheap_match:
            report.frames_unchanged += 1
            return
        try:
            digest, size, stable = _sha256_stable(item.path)
        except OSError as error:
            report.unreadable_files.append(f"{project.dir_key}/{item.rel_path}: {error}")
            return
        if not stable:
            report.warnings.append(f"{project.dir_key}/{item.rel_path}: file changed while being read; skipped this run")
            return
        if record.content_sha256 is None:
            hashes_to_record.append((record.id, digest, size))  # record once; it is immutable afterwards
            report.frames_unchanged += 1
            return
        if digest == record.content_sha256 and size == record.size_bytes:
            report.frames_unchanged += 1
            return
        report.conflicts.append(
            ChangedFileConflict(
                project=project.dir_key,
                rel_path=item.rel_path,
                recorded_sha256=record.content_sha256,
                current_sha256=digest,
                recorded_size=record.size_bytes,
                current_size=size,
            )
        )

    def _tally(self, frame: Frame, report: CaptureRegistrationReport) -> None:
        report.frames_by_format[frame.file_format or "unknown"] += 1
        report.frames_by_type[frame.frame_type or "unknown"] += 1
        if frame.file_format == "fits":
            report.fits_frames += 1
            for name in COVERAGE_FIELDS:
                if getattr(frame, name) is not None:
                    report.fits_field_coverage[name] += 1

    def _collect_duplicates(self, project: Project, report: CaptureRegistrationReport) -> None:
        """Content hashes shared by several paths. Reported only; nothing is deduplicated or deleted."""
        groups: dict[str, list[str]] = defaultdict(list)
        for frame in self.frames.list_frames(project_id=project.id):
            if frame.content_sha256:
                groups[frame.content_sha256].append(frame.rel_path)
        if report.dry_run:  # a real run has already stored its frames, so the database is complete
            for digest, paths in self._planned_hashes.get(project.id, {}).items():
                groups[digest].extend(paths)
        for digest, paths in groups.items():
            if len(paths) > 1:
                report.duplicate_hashes[digest] = [f"{project.dir_key}/{path}" for path in sorted(paths)]


# -- helpers ------------------------------------------------------------------------------------------


def _frame_type_from_directory(rel_path: str) -> tuple[str | None, str | None]:
    """The type implied by the capture-level folder of ``captures/<name>/<folder>/...``."""
    parts = PurePosixPath(rel_path).parts
    if len(parts) >= 4:  # captures / name / folder / file...
        folder = parts[2]
        frame_type = DIRECTORY_FRAME_TYPES.get(folder.lower())
        if frame_type is not None:
            return frame_type, f"directory:{folder}"
    return None, None


def _sha256_stable(path: Path) -> tuple[str, int, bool]:
    """SHA-256 and size of a file read-only, plus whether it stayed unchanged while being read."""
    before = path.stat()
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    after = path.stat()
    stable = (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns) == (size, before.st_mtime_ns)
    return digest.hexdigest(), size, stable


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dry_run_filesystem_preview(
    storage: ProjectStorage,
    connection: sqlite3.Connection | None = None,
) -> CaptureRegistrationReport:
    """Preview registration from the filesystem without changing the database.

    This intentionally does not initialize or migrate the database. It is useful before S3 has
    been applied, and before existing filesystem projects have canonical Project rows.
    """
    report = CaptureRegistrationReport(dry_run=True)
    canonical_project_keys = _canonical_project_keys(connection)
    registered_capture_keys = _registered_capture_keys(connection)

    for entry in scan_filesystem_projects(storage.projects_root):
        report.projects_seen.append(entry.dir_key)
        if entry.dir_key not in canonical_project_keys:
            report.filesystem_only_projects.append(entry.dir_key)
        try:
            layout = storage.locate_project(entry.dir_key)
        except FileNotFoundError:
            report.projects_skipped.append(entry.dir_key)
            continue
        if not layout.captures_dir.is_dir():
            continue

        for child in sorted(layout.captures_dir.iterdir(), key=lambda path: path.name):
            if not child.is_dir():
                continue
            try:
                validate_capture_name(child.name)
            except ValueError:
                report.warnings.append(f"{entry.dir_key}: skipped directory with an invalid capture name {child.name!r}")
                continue

            label = f"{entry.dir_key}/{child.name}"
            if (entry.dir_key, child.name) not in registered_capture_keys:
                report.filesystem_only_captures.append(label)
            try:
                resolved_root = storage.resolve_capture_root(entry.dir_key, child.name)
            except FileNotFoundError:
                report.outside_boundary.append(label)
                report.warnings.append(f"{label}: capture does not resolve to a directory inside captures/; skipped")
                continue

            found = CaptureRegistrar._scan_capture(child, resolved_root, child.name, label, report)
            for item in found:
                report.frames_created += 1
                report.frames_by_format[item.file_format] += 1
                frame_type, _ = _frame_type_from_directory(item.rel_path)
                report.frames_by_type[frame_type or "unknown"] += 1
                if item.file_format == "fits":
                    report.fits_frames += 1
    return report


def _canonical_project_keys(connection: sqlite3.Connection | None) -> set[str]:
    if connection is None or not _table_exists(connection, "projects"):
        return set()
    return {str(row[0]) for row in connection.execute("SELECT dir_key FROM projects;").fetchall()}


def _registered_capture_keys(connection: sqlite3.Connection | None) -> set[tuple[str, str]]:
    if connection is None or not (_table_exists(connection, "projects") and _table_exists(connection, "captures")):
        return set()
    return {
        (str(row[0]), str(row[1]))
        for row in connection.execute(
            """
            SELECT p.dir_key, c.name
            FROM captures c
            JOIN projects p ON p.id = c.project_id;
            """
        ).fetchall()
    }


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?;",
        (name,),
    ).fetchone() is not None


# -- command line -------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    from .project_registry import ProjectRegistry
    from .sqlite.db import connect_database, initialize_database

    parser = argparse.ArgumentParser(
        description="Register Captures and Frames of existing projects into the canonical database (read-only on files)."
    )
    parser.add_argument("--projects-root", default="projects")
    parser.add_argument("--database-path", default=None, help="Defaults to <projects-root>/tsn_dss.db")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be registered; write no captures or frames.")
    parser.add_argument("--verify-hashes", action="store_true", help="Re-hash every registered file to detect changed bytes.")
    parser.add_argument("--json", action="store_true", help="Print the full report as JSON.")
    args = parser.parse_args(argv)

    root = Path(args.projects_root)
    database = Path(args.database_path) if args.database_path else root / "tsn_dss.db"
    if args.dry_run:
        connection = connect_database(database) if database.exists() else None
        try:
            if connection is not None and get_user_version(connection) > CURRENT_SCHEMA_VERSION:
                raise RuntimeError("Database schema is newer than this TSN DSS build.")
            report = dry_run_filesystem_preview(ProjectStorage(root), connection)
        finally:
            if connection is not None:
                connection.close()
        if args.json:
            print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
        else:
            print(report.summary())
            for text in [*report.warnings, *[f"unreadable: {item}" for item in report.unreadable_files]]:
                print(f"  warning: {text}", file=sys.stderr)
        return 0

    connection = initialize_database(database)  # applies any pending schema migrations, with a backup
    try:
        storage = ProjectStorage(root)
        if not args.dry_run:
            ProjectRegistry(storage, connection).register_existing()  # canonical projects first (database only)
        report = CaptureRegistrar(storage, connection).register_all(dry_run=args.dry_run, verify_hashes=args.verify_hashes)
    finally:
        connection.close()

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(report.summary())
        for conflict in report.conflicts:
            print(f"  CONFLICT {conflict.project}/{conflict.rel_path}: bytes changed since registration", file=sys.stderr)
        for text in [*report.warnings, *[f"unreadable: {item}" for item in report.unreadable_files]]:
            print(f"  warning: {text}", file=sys.stderr)
    return 1 if report.conflicts else 0


if __name__ == "__main__":
    raise SystemExit(main())
