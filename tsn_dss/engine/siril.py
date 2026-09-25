from __future__ import annotations
"""Thin helpers around the official Siril CLI and stock OSC preprocessing script."""

import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from ..domain.models import ProcessingRun
from .projects import ProjectStorage, RunLayout, path_is_within
from .sqlite import DatasetRepository, FrameRepository, ProcessingRunRepository, ProjectRepository

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_OSC_SCRIPT_PATH = REPO_ROOT / "siril-1.4.4" / "scripts" / "OSC_Preprocessing.ssf"


@dataclass(slots=True)
class SirilProcessResult:
    """Completed Siril subprocess execution with command, logs, and exit code."""
    command: tuple[str, ...]
    working_directory: Path
    script_path: Path
    stdout: str
    stderr: str
    exit_code: int


class SirilError(RuntimeError):
    """Base class for Siril workflow failures detected by TSN DSS."""
    pass


class SirilOutputNotFoundError(SirilError):
    """Raised when Siril finishes but the expected stacked FITS cannot be found."""
    pass


class SirilRunner:
    """Small subprocess wrapper for running Siril scripts against a workspace."""

    def __init__(self, executable: str | Path | Sequence[str | Path]) -> None:
        if isinstance(executable, (str, Path)):
            self.executable = (str(executable),)
        else:
            self.executable = tuple(str(part) for part in executable)

        if not self.executable:
            raise ValueError("Siril executable must not be empty.")

    def build_command(
        self,
        *,
        working_directory: str | Path,
        script_path: str | Path,
    ) -> list[str]:
        return [
            *self.executable,
            "-d",
            str(Path(working_directory)),
            "-s",
            str(Path(script_path)),
        ]

    def run_script(
        self,
        *,
        working_directory: str | Path,
        script_path: str | Path,
        timeout_s: float | None = None,
    ) -> SirilProcessResult:
        """Execute one Siril script and return its raw process result."""
        resolved_working_directory = Path(working_directory)
        resolved_script_path = Path(script_path)
        command = self.build_command(
            working_directory=resolved_working_directory,
            script_path=resolved_script_path,
        )

        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )

        return SirilProcessResult(
            command=tuple(command),
            working_directory=resolved_working_directory,
            script_path=resolved_script_path,
            stdout=completed.stdout,
            stderr=completed.stderr,
            exit_code=completed.returncode,
        )


class SirilProcessingService:
    """Map a TSN processing run onto an isolated Siril CLI execution."""

    def __init__(
        self,
        *,
        project_storage: ProjectStorage,
        runner: SirilRunner,
        processing_runs: ProcessingRunRepository,
    ) -> None:
        self.project_storage = project_storage
        self.runner = runner
        self.processing_runs = processing_runs
        self.datasets = DatasetRepository(processing_runs.connection)
        self.frames = FrameRepository(processing_runs.connection)

    def execute_osc_preprocessing(
        self,
        *,
        processing_run_id: str,
        project_slug: str,
        capture_name: str,
        script_path: str | Path = DEFAULT_OSC_SCRIPT_PATH,
        timeout_s: float | None = None,
    ) -> SirilProcessResult:
        """Run OSC preprocessing for the exact dataset frames owned by the processing run."""
        current = self.processing_runs.get_processing_run(processing_run_id)
        if current is None:
            raise KeyError(f"ProcessingRun not found: {processing_run_id}")
        dataset = self.datasets.get_dataset(current.dataset_id)
        if dataset is None:
            raise KeyError(f"Dataset not found for ProcessingRun: {current.dataset_id}")

        frame_sources = self._collect_dataset_frame_sources(dataset.id)
        run_started_at = _utc_timestamp()
        self.processing_runs.set_processing_run_status(
            processing_run_id,
            "running",
            started_at=run_started_at,
        )

        run_layout = self.project_storage.prepare_siril_run(
            project_slug=project_slug,
            run_id=processing_run_id,
            frame_sources=frame_sources,
        )
        resolved_script_path = Path(script_path)
        result = self.runner.run_script(
            working_directory=run_layout.workspace_dir,
            script_path=resolved_script_path,
            timeout_s=timeout_s,
        )

        stdout_log_path = run_layout.logs_dir / "stdout.log"
        stderr_log_path = run_layout.logs_dir / "stderr.log"
        stdout_log_path.write_text(result.stdout, encoding="utf-8")
        stderr_log_path.write_text(result.stderr, encoding="utf-8")

        if result.exit_code != 0:
            self._mark_failed(
                current=self.processing_runs.get_processing_run(processing_run_id),
                finished_at=_utc_timestamp(),
                project_slug=project_slug,
                capture_name=capture_name,
                dataset_id=current.dataset_id,
                run_layout=run_layout,
                result=result,
                stdout_log_path=stdout_log_path,
                stderr_log_path=stderr_log_path,
            )
            return result

        workspace_output = find_osc_preprocessing_result(run_layout.workspace_dir)
        if workspace_output is None:
            self._mark_failed(
                current=self.processing_runs.get_processing_run(processing_run_id),
                finished_at=_utc_timestamp(),
                project_slug=project_slug,
                capture_name=capture_name,
                dataset_id=current.dataset_id,
                run_layout=run_layout,
                result=result,
                stdout_log_path=stdout_log_path,
                stderr_log_path=stderr_log_path,
            )
            raise SirilOutputNotFoundError(
                f"OSC_Preprocessing completed but no result*.fit was found in {run_layout.workspace_dir}"
            )

        artifact_output = run_layout.artifacts_dir / workspace_output.name
        shutil.copy2(workspace_output, artifact_output)

        finished_at = _utc_timestamp()
        running = self.processing_runs.get_processing_run(processing_run_id)
        assert running is not None
        updated = ProcessingRun(
            id=running.id,
            dataset_id=running.dataset_id,
            version_label=running.version_label,
            engine_name=running.engine_name,
            engine_version=running.engine_version,
            pipeline=running.pipeline,
            parameters={
                **running.parameters,
                **_result_metadata(
                    project_slug=project_slug,
                    capture_name=capture_name,
                    dataset_id=running.dataset_id,
                    run_layout=run_layout,
                    result=result,
                    stdout_log_path=stdout_log_path,
                    stderr_log_path=stderr_log_path,
                    output_path=artifact_output,
                    started_at=running.started_at,
                    finished_at=finished_at,
                ),
            },
            linear_stack_path=str(artifact_output),
            preview_path=running.preview_path,
            final_image_path=str(artifact_output),
            status="completed",
            started_at=running.started_at,
            finished_at=finished_at,
            notes=running.notes,
        )
        self.processing_runs.update_processing_run(updated)
        return result

    def _mark_failed(
        self,
        *,
        current: ProcessingRun | None,
        finished_at: str,
        project_slug: str,
        capture_name: str,
        dataset_id: str,
        run_layout: RunLayout,
        result: SirilProcessResult,
        stdout_log_path: Path,
        stderr_log_path: Path,
    ) -> None:
        if current is None:
            return

        updated = ProcessingRun(
            id=current.id,
            dataset_id=current.dataset_id,
            version_label=current.version_label,
            engine_name=current.engine_name,
            engine_version=current.engine_version,
            pipeline=current.pipeline,
            parameters={
                **current.parameters,
                **_result_metadata(
                    project_slug=project_slug,
                    capture_name=capture_name,
                    dataset_id=dataset_id,
                    run_layout=run_layout,
                    result=result,
                    stdout_log_path=stdout_log_path,
                    stderr_log_path=stderr_log_path,
                    output_path=None,
                    started_at=current.started_at,
                    finished_at=finished_at,
                ),
            },
            linear_stack_path=current.linear_stack_path,
            preview_path=current.preview_path,
            final_image_path=current.final_image_path,
            status="failed",
            started_at=current.started_at,
            finished_at=finished_at,
            notes=current.notes,
        )
        self.processing_runs.update_processing_run(updated)

    def _collect_dataset_frame_sources(self, dataset_id: str) -> list[tuple[str, Path]]:
        dataset = self.datasets.get_dataset(dataset_id)
        if dataset is None:
            raise KeyError(f"Dataset not found: {dataset_id}")

        projects = ProjectRepository(self.processing_runs.connection)
        frame_sources: list[tuple[str, Path]] = []
        for frame_id in dataset.frame_ids:
            frame = self.frames.get_frame(frame_id)
            if frame is None:
                raise KeyError(f"Frame not found for dataset {dataset_id}: {frame_id}")
            if frame.frame_type is None:
                raise ValueError(f"Frame {frame_id} has no frame_type, so it cannot be placed in a Siril workspace.")
            project = projects.get_project(frame.project_id)
            if project is None:
                raise KeyError(f"Project not found for frame {frame_id}: {frame.project_id}")
            project_root = self.project_storage.locate_project(project.dir_key).project_root
            frame_path = project_root.joinpath(*frame.rel_path.split("/"))
            if not path_is_within(frame_path.resolve(), project_root.resolve()):
                raise ValueError(f"Frame {frame_id} resolves outside its project: {frame.rel_path}")
            frame_sources.append((frame.frame_type, frame_path))
        return frame_sources


def find_osc_preprocessing_result(working_directory: str | Path) -> Path | None:
    """Return the newest Siril result FITS from a completed OSC preprocessing workspace."""
    workdir = Path(working_directory)
    preferred = sorted(
        workdir.glob("result_*.fit"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if preferred:
        return preferred[0]

    fallback = sorted(
        workdir.glob("result.fit"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if fallback:
        return fallback[0]
    return None


def _result_metadata(
    *,
    project_slug: str,
    capture_name: str,
    dataset_id: str,
    run_layout: RunLayout,
    result: SirilProcessResult,
    stdout_log_path: Path,
    stderr_log_path: Path,
    output_path: Path | None,
    started_at: str | None,
    finished_at: str | None,
) -> dict[str, object]:
    metadata: dict[str, object] = {
        "dataset_id": dataset_id,
        "project_slug": project_slug,
        "capture_name": capture_name,
        "workspace_dir": str(run_layout.workspace_dir),
        "workspace_frame_paths": [str(path) for path in run_layout.workspace_frame_paths],
        "artifacts_dir": str(run_layout.artifacts_dir),
        "stdout_log_path": str(stdout_log_path),
        "stderr_log_path": str(stderr_log_path),
        "script_path": str(result.script_path),
        "engine_name": result.command[0],
        "command": list(result.command),
        "exit_code": result.exit_code,
    }
    if started_at is not None:
        metadata["started_at"] = started_at
    if finished_at is not None:
        metadata["finished_at"] = finished_at
        if started_at is not None:
            metadata["duration_seconds"] = _duration_seconds(started_at, finished_at)
    if output_path is not None:
        metadata["output_path"] = str(output_path)
    return metadata


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _duration_seconds(started_at: str, finished_at: str) -> float:
    start = datetime.fromisoformat(started_at)
    finish = datetime.fromisoformat(finished_at)
    return max((finish - start).total_seconds(), 0.0)
