from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

RAW_CAPTURE_DIRECTORIES = ("biases", "darks", "flats", "lights")
FRAME_TYPE_TO_CAPTURE_DIR = {
    "bias": "biases",
    "dark": "darks",
    "flat": "flats",
    "light": "lights",
}


@dataclass(slots=True)
class ProjectLayout:
    project_root: Path
    captures_dir: Path
    runs_dir: Path


@dataclass(slots=True)
class RunLayout:
    run_root: Path
    workspace_dir: Path
    artifacts_dir: Path
    logs_dir: Path
    workspace_frame_paths: tuple[Path, ...] = ()


@dataclass(slots=True)
class ProjectSummary:
    slug: str
    project_root: Path
    captures_dir: Path
    runs_dir: Path
    capture_names: tuple[str, ...]
    run_names: tuple[str, ...]
    sky_target: str | None = None


class ProjectStorage:
    def __init__(self, projects_root: str | Path) -> None:
        self.projects_root = Path(projects_root)

    def ensure_project(self, project_slug: str) -> ProjectLayout:
        project_root = self.projects_root / project_slug
        captures_dir = project_root / "captures"
        runs_dir = project_root / "runs"

        captures_dir.mkdir(parents=True, exist_ok=True)
        runs_dir.mkdir(parents=True, exist_ok=True)

        return ProjectLayout(
            project_root=project_root,
            captures_dir=captures_dir,
            runs_dir=runs_dir,
        )

    def list_projects(self) -> list[ProjectSummary]:
        if not self.projects_root.exists():
            return []

        projects: list[ProjectSummary] = []
        for project_root in sorted(
            (path for path in self.projects_root.iterdir() if path.is_dir()),
            key=lambda path: path.name.lower(),
        ):
            layout = self.ensure_project(project_root.name)
            capture_names = tuple(
                sorted(
                    child.name
                    for child in layout.captures_dir.iterdir()
                    if child.is_dir()
                )
            )
            run_names = tuple(
                sorted(
                    child.name
                    for child in layout.runs_dir.iterdir()
                    if child.is_dir()
                )
            )
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
        source_path = Path(source_dir)
        self._validate_capture_source(source_path)

        layout = self.ensure_project(project_slug)
        destination = layout.captures_dir / capture_name

        if destination.exists():
            raise FileExistsError(f"Capture already exists: {destination}")

        if move:
            shutil.move(str(source_path), str(destination))
        else:
            shutil.copytree(source_path, destination)

        return destination

    def prepare_siril_run(
        self,
        project_slug: str,
        run_id: str,
        *,
        frame_sources: list[tuple[str, str | Path]],
    ) -> RunLayout:
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


def safe_path_component(value: str) -> str:
    sanitized = "".join(character if character.isalnum() or character in ("-", "_", ".") else "_" for character in value)
    return sanitized.strip("._") or "unnamed"


def _link_or_copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)
