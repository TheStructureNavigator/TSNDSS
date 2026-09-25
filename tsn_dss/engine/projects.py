from __future__ import annotations
"""Filesystem layout helpers for TSN DSS projects, captures, and run workspaces.

Read operations never change the filesystem. Only the explicit write operations
(``ensure_project``, ``create_project``, ``set_project_sky_target``, ``import_capture``,
``prepare_siril_run`` and ``delete_project``) create or remove anything. Use
``locate_project`` / ``project_layout`` to find a project without creating it.
"""

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

RAW_CAPTURE_DIRECTORIES = ("biases", "darks", "flats", "lights")
FRAME_TYPE_TO_CAPTURE_DIR = {
    "bias": "biases",
    "dark": "darks",
    "flat": "flats",
    "light": "lights",
}


@dataclass(slots=True)
class ProjectLayout:
    """Stable top-level directories belonging to one TSN DSS project."""
    project_root: Path
    captures_dir: Path
    runs_dir: Path


@dataclass(slots=True)
class RunLayout:
    """Concrete workspace directories prepared for one processing run."""
    run_root: Path
    workspace_dir: Path
    artifacts_dir: Path
    logs_dir: Path
    workspace_frame_paths: tuple[Path, ...] = ()


@dataclass(slots=True)
class ProjectSummary:
    """Lightweight project listing payload for UI and API consumers."""
    slug: str
    project_root: Path
    captures_dir: Path
    runs_dir: Path
    capture_names: tuple[str, ...]
    run_names: tuple[str, ...]
    sky_target: str | None = None


@dataclass(slots=True)
class CaptureFileEntry:
    """One file inside a capture folder listing."""
    name: str
    relative_path: str
    size_bytes: int
    suffix: str


@dataclass(slots=True)
class CaptureFolderEntry:
    """A raw capture subfolder and the files currently visible inside it."""
    name: str
    file_count: int
    files: tuple[CaptureFileEntry, ...]


@dataclass(slots=True)
class CaptureDetails:
    """Expanded capture description grouped by canonical raw frame folders."""
    project_slug: str
    capture_name: str
    capture_root: Path
    folders: tuple[CaptureFolderEntry, ...]


class ProjectStorage:
    """Owns the on-disk project layout used by captures and processing runs."""

    def __init__(self, projects_root: str | Path) -> None:
        self.projects_root = Path(projects_root)

    # -- locating a project (READ: never creates anything) --------------------------------------

    def project_layout(self, project_slug: str) -> ProjectLayout:
        """Where a project's directories are, or would be. Pure path arithmetic.

        Validates that the name is a single directory name (no separators, ``..`` or drive
        prefix) and creates nothing, whether or not the project exists. Raises ``ValueError``
        for an invalid name.
        """
        validate_dir_key(project_slug)
        return _layout_for(self.projects_root / project_slug)

    def locate_project(self, project_slug: str) -> ProjectLayout:
        """The layout of an *existing* project directory. Never creates or repairs anything.

        Raises ``FileNotFoundError`` when there is no such project, which includes names
        that are not a valid single directory name. ``captures_dir`` and ``runs_dir`` are
        returned as paths even if they do not exist yet (an incomplete project).
        """
        try:
            layout = self.project_layout(project_slug)
        except ValueError as error:
            raise FileNotFoundError(f"Project not found: {project_slug}") from error
        if not layout.project_root.is_dir():
            raise FileNotFoundError(f"Project not found: {project_slug}")
        return layout

    # -- creating structure (WRITE: explicit operations only) -------------------------------------

    def ensure_project(self, project_slug: str) -> ProjectLayout:
        """Create the canonical folder structure for a project if it does not exist.

        This is a WRITE helper. Read operations must use ``locate_project`` instead.
        """
        layout = self.project_layout(project_slug)
        layout.captures_dir.mkdir(parents=True, exist_ok=True)
        layout.runs_dir.mkdir(parents=True, exist_ok=True)
        return layout

    def list_projects(self) -> list[ProjectSummary]:
        """Every project directory under the root. A READ: nothing is created or repaired.

        A project that lacks ``captures/`` or ``runs/`` is reported truthfully with no
        captures or runs.
        """
        if not self.projects_root.exists():
            return []

        projects: list[ProjectSummary] = []
        for project_root in sorted(
            (path for path in self.projects_root.iterdir() if path.is_dir()),
            key=lambda path: path.name.lower(),
        ):
            layout = _layout_for(project_root)
            capture_names = _child_directory_names(layout.captures_dir)
            run_names = _child_directory_names(layout.runs_dir)
            projects.append(
                self._build_project_summary(
                    layout=layout,
                    capture_names=capture_names,
                    run_names=run_names,
                )
            )
        return projects

    def get_project(self, project_slug: str) -> ProjectSummary | None:
        for project in self.list_projects():
            if project.slug == project_slug:
                return project
        return None

    def create_project(self, project_slug: str) -> ProjectSummary:
        layout = self.ensure_project(project_slug)
        return self._build_project_summary(
            layout=layout,
            capture_names=tuple(
                sorted(child.name for child in layout.captures_dir.iterdir() if child.is_dir())
            ),
            run_names=tuple(
                sorted(child.name for child in layout.runs_dir.iterdir() if child.is_dir())
            ),
        )

    def delete_project(self, project_slug: str) -> None:
        project_root = (self.projects_root / project_slug).resolve()
        projects_root = self.projects_root.resolve()

        try:
            project_root.relative_to(projects_root)
        except ValueError as error:
            raise ValueError(f"Project path escapes projects root: {project_slug}") from error

        if not project_root.exists() or not project_root.is_dir():
            raise FileNotFoundError(f"Project not found: {project_slug}")

        shutil.rmtree(project_root)

    def set_project_sky_target(self, project_slug: str, sky_target: str | None) -> ProjectSummary:
        layout = self.ensure_project(project_slug)
        metadata = self._read_project_metadata(layout.project_root)
        normalized_target = sky_target.strip() if isinstance(sky_target, str) else None
        metadata["sky_target"] = normalized_target or None
        self._write_project_metadata(layout.project_root, metadata)
        project = self.get_project(project_slug)
        if project is None:
            raise KeyError(f"Project not found: {project_slug}")
        return project

    def import_capture(
        self,
        project_slug: str,
        capture_name: str,
        source_dir: str | Path,
        *,
        move: bool = False,
    ) -> Path:
        """Copy or move a user-provided capture tree into the project workspace.

        The capture name is validated first (a pure check), so an invalid name fails before
        anything is created, copied or moved. The destination is always a new direct child of
        ``captures/``: a name that already exists there, even as a dangling link, is refused.

        Links inside the source tree: a copy follows them and stores the linked content as
        ordinary files, so the destination contains no links. A move keeps the links as they
        are (a same-volume move is a rename). Either way the boundary is enforced when a capture
        is read or processed (``resolve_capture_root`` and the callers), not at import.
        """
        validate_capture_name(capture_name)
        source_path = Path(source_dir)
        self._validate_capture_source(source_path)

        layout = self.ensure_project(project_slug)
        destination = layout.captures_dir / capture_name

        if os.path.lexists(destination):
            raise FileExistsError(f"Capture already exists: {destination}")

        if move:
            shutil.move(str(source_path), str(destination))
        else:
            shutil.copytree(source_path, destination)

        return destination

    def resolve_capture_root(self, project_slug: str, capture_name: str) -> Path:
        """The resolved physical root of an existing capture. A READ: creates nothing.

        The capture must be a direct child directory of the project's ``captures/`` directory
        after symlinks and junctions are resolved. Raises ``FileNotFoundError`` for an unknown
        project or capture, an invalid name, or a capture that would resolve outside
        ``captures/`` (for example ``..``, a nested path, or a link to another location).
        """
        return self._capture_root_within(self.locate_project(project_slug), capture_name)

    def _capture_root_within(self, layout: ProjectLayout, capture_name: str) -> Path:
        try:
            validate_capture_name(capture_name)
        except ValueError as error:
            raise FileNotFoundError(f"Capture not found: {capture_name}") from error
        captures_root = layout.captures_dir.resolve()
        resolved = (layout.captures_dir / capture_name).resolve()
        if resolved.parent != captures_root or not resolved.is_dir():
            raise FileNotFoundError(f"Capture not found: {capture_name}")
        return resolved

    def describe_capture(self, project_slug: str, capture_name: str) -> CaptureDetails:
        """Return a UI-friendly listing of the canonical capture subdirectories.

        Only content that physically lies inside the capture is listed: a subfolder or file
        that resolves elsewhere (a link) is treated as absent, so nothing outside the capture
        is exposed.
        """
        layout = self.locate_project(project_slug)
        resolved_root = self._capture_root_within(layout, capture_name)
        capture_root = layout.captures_dir / capture_name

        folders: list[CaptureFolderEntry] = []
        for folder_name in RAW_CAPTURE_DIRECTORIES:
            folder_path = capture_root / folder_name
            files: list[CaptureFileEntry] = []
            if folder_path.is_dir() and path_is_within(folder_path.resolve(), resolved_root):
                for file_path in sorted(
                    (
                        path
                        for path in folder_path.iterdir()
                        if path.is_file() and path_is_within(path.resolve(), resolved_root)
                    ),
                    key=lambda path: path.name.lower(),
                ):
                    files.append(
                        CaptureFileEntry(
                            name=file_path.name,
                            relative_path=str(file_path.relative_to(capture_root)),
                            size_bytes=file_path.stat().st_size,
                            suffix=file_path.suffix,
                        )
                    )
            folders.append(
                CaptureFolderEntry(
                    name=folder_name,
                    file_count=len(files),
                    files=tuple(files),
                )
            )

        return CaptureDetails(
            project_slug=project_slug,
            capture_name=capture_name,
            capture_root=capture_root,
            folders=tuple(folders),
        )

    def resolve_capture_file(self, project_slug: str, capture_name: str, relative_path: str | Path) -> tuple[Path, Path]:
        """Resolve a capture-relative file path while preventing path escape.

        The capture itself must lie inside ``captures/`` (unknown, invalid or escaping captures
        are ``FileNotFoundError``). The file path must be relative, and the resolved file must
        lie inside the resolved capture, so ``..``, absolute paths and links to elsewhere raise
        ``ValueError``. Nested files inside the capture are allowed.
        """
        capture_root = self.resolve_capture_root(project_slug, capture_name)
        _validate_relative_file_path(relative_path)

        file_path = (capture_root / Path(relative_path)).resolve()
        if not path_is_within(file_path, capture_root):
            raise ValueError("Capture file path escapes capture root.")

        if not file_path.exists() or not file_path.is_file():
            raise FileNotFoundError(f"Capture file not found: {relative_path}")

        return capture_root, file_path

    def prepare_siril_run(
        self,
        project_slug: str,
        run_id: str,
        *,
        frame_sources: list[tuple[str, str | Path]],
    ) -> RunLayout:
        """Build an isolated Siril workspace containing only the selected dataset files."""
        layout = self.ensure_project(project_slug)

        run_root = layout.runs_dir / safe_path_component(run_id)
        if run_root.exists():
            raise FileExistsError(f"Run workspace already exists: {run_root}")

        workspace_dir = run_root / "workspace"
        artifacts_dir = run_root / "artifacts"
        logs_dir = run_root / "logs"

        workspace_dir.mkdir(parents=True, exist_ok=False)
        artifacts_dir.mkdir(parents=True, exist_ok=False)
        logs_dir.mkdir(parents=True, exist_ok=False)

        workspace_frame_paths: list[Path] = []
        seen_destinations: set[Path] = set()

        for directory_name in RAW_CAPTURE_DIRECTORIES:
            (workspace_dir / directory_name).mkdir(parents=True, exist_ok=True)

        if not frame_sources:
            raise ValueError("Siril workspace requires at least one source frame.")

        for frame_type, source in frame_sources:
            destination_dir_name = FRAME_TYPE_TO_CAPTURE_DIR.get(frame_type)
            if destination_dir_name is None:
                raise ValueError(f"Unsupported frame_type for Siril workspace: {frame_type}")

            source_path = Path(source)
            if not source_path.exists() or not source_path.is_file():
                raise FileNotFoundError(f"Frame source file not found: {source_path}")

            destination = workspace_dir / destination_dir_name / source_path.name
            if destination in seen_destinations:
                raise ValueError(f"Duplicate destination in Siril workspace: {destination}")

            _link_or_copy_file(source_path, destination)
            seen_destinations.add(destination)
            workspace_frame_paths.append(destination)

        missing_required_content = [
            directory_name
            for directory_name in RAW_CAPTURE_DIRECTORIES
            if not any((workspace_dir / directory_name).iterdir())
        ]
        if missing_required_content:
            raise ValueError(
                "Siril workspace must contain at least one file in each required directory: "
                + ", ".join(missing_required_content)
            )

        return RunLayout(
            run_root=run_root,
            workspace_dir=workspace_dir,
            artifacts_dir=artifacts_dir,
            logs_dir=logs_dir,
            workspace_frame_paths=tuple(workspace_frame_paths),
        )

    def _validate_capture_source(self, source_path: Path) -> None:
        if not source_path.exists() or not source_path.is_dir():
            raise FileNotFoundError(f"Capture directory not found: {source_path}")

        missing = [
            directory_name
            for directory_name in RAW_CAPTURE_DIRECTORIES
            if not (source_path / directory_name).is_dir()
        ]
        if missing:
            raise ValueError(
                f"Capture directory must contain {RAW_CAPTURE_DIRECTORIES}. Missing: {', '.join(missing)}"
            )

    def _build_project_summary(
        self,
        *,
        layout: ProjectLayout,
        capture_names: tuple[str, ...],
        run_names: tuple[str, ...],
    ) -> ProjectSummary:
        metadata = self._read_project_metadata(layout.project_root)
        sky_target = metadata.get("sky_target")
        return ProjectSummary(
            slug=layout.project_root.name,
            project_root=layout.project_root,
            captures_dir=layout.captures_dir,
            runs_dir=layout.runs_dir,
            capture_names=capture_names,
            run_names=run_names,
            sky_target=sky_target if isinstance(sky_target, str) and sky_target.strip() else None,
        )

    def _metadata_path(self, project_root: Path) -> Path:
        return project_root / "project.json"

    def _read_project_metadata(self, project_root: Path) -> dict[str, object]:
        metadata_path = self._metadata_path(project_root)
        if not metadata_path.exists():
            return {}
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_project_metadata(self, project_root: Path, metadata: dict[str, object]) -> None:
        self._metadata_path(project_root).write_text(
            json.dumps(metadata, indent=2, sort_keys=True),
            encoding="utf-8",
        )


def _validate_single_component(name: str, what: str) -> str:
    """``name`` must be exactly one directory-name component: no separators, no ``.``/``..``,
    nothing absolute, rooted or drive-qualified (checked with Windows rules on every platform)."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{what} must not be empty.")
    if "\x00" in name or "/" in name or "\\" in name or name in {".", ".."}:
        raise ValueError(f"{what} must be a single directory name: {name!r}")
    windows = PureWindowsPath(name)
    path = Path(name)
    if windows.drive or windows.root or path.name != name or path.is_absolute():
        raise ValueError(f"{what} must be a single directory name: {name!r}")
    return name


def validate_dir_key(dir_key: str) -> str:
    """A project name (``dir_key``) is exactly one directory name under the projects root."""
    return _validate_single_component(dir_key, "Project name")


def validate_capture_name(capture_name: str) -> str:
    """A capture name identifies exactly one direct child directory of ``<project>/captures/``.

    Unicode and spaces are fine; nothing is slugified or renamed. Rejected: empty or blank,
    ``.``, ``..``, separators, absolute, rooted or drive-qualified names. This is a pure check;
    whether the directory really lies inside ``captures/`` once links are resolved is checked
    against the filesystem by ``ProjectStorage.resolve_capture_root``.
    """
    return _validate_single_component(capture_name, "Capture name")


def validate_project_relative_path(rel_path: str) -> str:
    """A stored file locator: relative to the Project root, ``/``-separated, no escapes.

    Rejected: empty, absolute, rooted or drive-qualified paths, backslashes, NUL, and any
    empty, ``.`` or ``..`` segment. Returns the path unchanged; this only validates.
    """
    if not isinstance(rel_path, str) or not rel_path:
        raise ValueError("Relative path must not be empty.")
    if "\x00" in rel_path or "\\" in rel_path:
        raise ValueError(f"Relative path must use '/' separators and contain no NUL: {rel_path!r}")
    windows = PureWindowsPath(rel_path)
    if rel_path.startswith("/") or windows.drive or windows.root or Path(rel_path).is_absolute():
        raise ValueError(f"Relative path must not be absolute: {rel_path!r}")
    if any(segment in {"", ".", ".."} for segment in rel_path.split("/")):
        raise ValueError(f"Relative path must not contain empty, '.' or '..' segments: {rel_path!r}")
    return rel_path


def path_is_within(path: Path, root: Path) -> bool:
    """True if ``path`` is ``root`` or lies beneath it. Callers pass already-resolved paths."""
    return path == root or root in path.parents


def _validate_relative_file_path(relative_path: str | Path) -> None:
    """A path beneath a capture must be relative: no drive, no root, no NUL."""
    text = str(relative_path)
    windows = PureWindowsPath(text)
    if "\x00" in text or windows.drive or windows.root or Path(text).is_absolute():
        raise ValueError("Capture file path must be relative to the capture.")


def _layout_for(project_root: Path) -> ProjectLayout:
    return ProjectLayout(
        project_root=project_root,
        captures_dir=project_root / "captures",
        runs_dir=project_root / "runs",
    )


def _child_directory_names(directory: Path) -> tuple[str, ...]:
    """Names of the subdirectories of ``directory``; empty if it does not exist."""
    if not directory.is_dir():
        return ()
    return tuple(sorted(child.name for child in directory.iterdir() if child.is_dir()))


def safe_path_component(value: str) -> str:
    """Normalize free-form text into a filesystem-safe path fragment."""
    sanitized = "".join(character if character.isalnum() or character in ("-", "_", ".") else "_" for character in value)
    return sanitized.strip("._") or "unnamed"


def _link_or_copy_file(source: Path, destination: Path) -> None:
    """Prefer hardlinks for workspace assembly and fall back to file copy when needed."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)
