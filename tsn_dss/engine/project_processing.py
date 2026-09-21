from __future__ import annotations
"""Run-manager layer for TSN-controlled Siril preprocessing workspaces."""

import json
import shutil
import subprocess
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .projects import FRAME_TYPE_TO_CAPTURE_DIR, ProjectStorage, RunLayout
from .siril import DEFAULT_OSC_SCRIPT_PATH, find_osc_preprocessing_result

DEFAULT_SIRIL_EXECUTABLE = ("siril-cli",)
TERMINAL_RUN_STATUSES = {"completed", "failed"}


@dataclass(slots=True)
class ProjectRunSnapshot:
    """Serialized state of one processing run as seen by the GUI and local API."""
    id: str
    project_slug: str
    capture_name: str
    keep_process_dir: bool
    status: str
    progress_pct: int
    stage: str
    command: list[str]
    script_path: str
    workspace_dir: str
    artifacts_dir: str
    logs_dir: str
    stdout_log_path: str
    stderr_log_path: str
    status_path: str
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    output_path: str | None = None
    preview_path: str | None = None
    preview_log_path: str | None = None
    preview_error: str | None = None
    error_message: str | None = None
    combined_log: str = ""


class ProjectRunManager:
    """Starts, tracks, restores, and deletes project-local Siril runs."""

    def __init__(self, *, project_storage: ProjectStorage) -> None:
        self.project_storage = project_storage
        self._runs: dict[str, ProjectRunSnapshot] = {}
        self._lock = threading.Lock()
        self._load_existing_runs()

    def start_osc_preprocessing(
        self,
        *,
        project_slug: str,
        capture_name: str,
        executable: str | Sequence[str] = DEFAULT_SIRIL_EXECUTABLE,
        script_path: str | Path = DEFAULT_OSC_SCRIPT_PATH,
        keep_process_dir: bool = False,
    ) -> ProjectRunSnapshot:
        """Create an isolated workspace and launch Siril asynchronously for one capture."""
        # Locating only: a run for an unknown project must fail without creating that project.
        # prepare_siril_run below is the explicit write that creates the run workspace.
        capture_root = self.project_storage.project_layout(project_slug).captures_dir / capture_name
        frame_sources = self._collect_capture_frame_sources(capture_root)
        run_id = f"run_{project_slug}_{capture_name}_{_utc_now().strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"
        run_layout = self.project_storage.prepare_siril_run(
            project_slug,
            run_id,
            frame_sources=frame_sources,
        )

        resolved_executable = _normalize_executable(executable)
        command = [
            *resolved_executable,
            "-d",
            str(run_layout.workspace_dir),
            "-s",
            str(Path(script_path)),
        ]

        snapshot = ProjectRunSnapshot(
            id=run_id,
            project_slug=project_slug,
            capture_name=capture_name,
            keep_process_dir=keep_process_dir,
            status="queued",
            progress_pct=10,
            stage="Workspace prepared",
            command=command,
            script_path=str(Path(script_path)),
            workspace_dir=str(run_layout.workspace_dir),
            artifacts_dir=str(run_layout.artifacts_dir),
            logs_dir=str(run_layout.logs_dir),
            stdout_log_path=str(run_layout.logs_dir / "stdout.log"),
            stderr_log_path=str(run_layout.logs_dir / "stderr.log"),
            status_path=str(run_layout.run_root / "status.json"),
        )

        with self._lock:
            self._runs[run_id] = snapshot
        self._persist_snapshot(snapshot)

        worker = threading.Thread(
            target=self._run_osc_preprocessing,
            args=(snapshot.id, run_layout),
            daemon=True,
        )
        worker.start()
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> ProjectRunSnapshot:
        with self._lock:
            snapshot = self._runs.get(run_id)
            if snapshot is None:
                raise KeyError(f"Unknown project run: {run_id}")
            return _copy_snapshot(snapshot)

    def list_runs(self, *, project_slug: str | None = None, capture_name: str | None = None) -> list[ProjectRunSnapshot]:
        with self._lock:
            items = list(self._runs.values())
        if project_slug is not None:
            items = [item for item in items if item.project_slug == project_slug]
        if capture_name is not None:
            items = [item for item in items if item.capture_name == capture_name]
        items.sort(key=lambda item: item.id, reverse=True)
        return [_copy_snapshot(item) for item in items]

    def regenerate_preview(self, run_id: str) -> ProjectRunSnapshot:
        """Retry browser preview generation from an already-produced FITS output."""
        snapshot = self.get_run(run_id)
        if not snapshot.output_path:
            raise ValueError("This run does not have an output FITS yet.")

        executable_parts = _extract_executable_parts(snapshot.command)
        preview_path, preview_log_path, preview_error = self._generate_preview_image(
            executable_parts=executable_parts,
            output_path=Path(snapshot.output_path),
            artifacts_dir=Path(snapshot.artifacts_dir),
            logs_dir=Path(snapshot.logs_dir),
        )
        self._update_run(
            run_id,
            preview_path=preview_path,
            preview_log_path=preview_log_path,
            preview_error=preview_error,
        )
        return self.get_run(run_id)

    def delete_run(self, run_id: str) -> None:
        """Remove a finished run from disk and from the in-memory registry."""
        snapshot = self.get_run(run_id)
        if snapshot.status not in TERMINAL_RUN_STATUSES:
            raise ValueError("Cannot delete a run that is still in progress.")

        run_root = Path(snapshot.status_path).resolve().parent
        projects_root = self.project_storage.projects_root.resolve()
        try:
            run_root.relative_to(projects_root)
        except ValueError as error:
            raise ValueError(f"Run path escapes projects root: {run_id}") from error

        if run_root.exists():
            shutil.rmtree(run_root)

        with self._lock:
            self._runs.pop(run_id, None)

    def _run_osc_preprocessing(self, run_id: str, run_layout: RunLayout) -> None:
        """Worker entrypoint that streams logs, finds output, and finalizes run state."""
        snapshot = self.get_run(run_id)
        stdout_log = Path(snapshot.stdout_log_path)
        stderr_log = Path(snapshot.stderr_log_path)
        stdout_log.write_text("", encoding="utf-8")
        stderr_log.write_text("", encoding="utf-8")

        self._update_run(run_id, status="running", progress_pct=25, stage="Launching Siril", started_at=_utc_timestamp())

        process = subprocess.Popen(
            snapshot.command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        stdout_thread = threading.Thread(
            target=self._consume_stream,
            args=(run_id, process.stdout, stdout_log, False),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._consume_stream,
            args=(run_id, process.stderr, stderr_log, True),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        exit_code = process.wait()
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)

        try:
            if exit_code != 0:
                self._update_run(
                    run_id,
                    status="failed",
                    progress_pct=100,
                    stage="Siril failed",
                    exit_code=exit_code,
                    finished_at=_utc_timestamp(),
                    error_message=f"Siril exited with code {exit_code}.",
                )
                return

            self._update_run(run_id, progress_pct=90, stage="Searching for output")
            output = find_osc_preprocessing_result(run_layout.workspace_dir)
            if output is None:
                self._update_run(
                    run_id,
                    status="failed",
                    progress_pct=100,
                    stage="Output missing",
                    exit_code=exit_code,
                    finished_at=_utc_timestamp(),
                    error_message="OSC_Preprocessing finished but no result*.fit was found.",
                )
                return

            artifact_output = run_layout.artifacts_dir / output.name
            shutil.copy2(output, artifact_output)
            self._update_run(
                run_id,
                progress_pct=94,
                stage="Generating preview",
                exit_code=exit_code,
                output_path=str(artifact_output),
            )
            preview_path, preview_log_path, preview_error = self._generate_preview_image(
                executable_parts=_extract_executable_parts(snapshot.command),
                output_path=artifact_output,
                artifacts_dir=run_layout.artifacts_dir,
                logs_dir=run_layout.logs_dir,
            )
            self._update_run(
                run_id,
                status="completed",
                progress_pct=100,
                stage="Completed",
                exit_code=exit_code,
                finished_at=_utc_timestamp(),
                output_path=str(artifact_output),
                preview_path=preview_path,
                preview_log_path=preview_log_path,
                preview_error=preview_error,
            )
        finally:
            if not snapshot.keep_process_dir:
                self._cleanup_process_directory(run_layout.workspace_dir)

    def _consume_stream(
        self,
        run_id: str,
        stream: Any,
        log_path: Path,
        is_stderr: bool,
    ) -> None:
        if stream is None:
            return

        with log_path.open("a", encoding="utf-8") as handle:
            for line in iter(stream.readline, ""):
                handle.write(line)
                handle.flush()
                progress_pct, stage = _progress_from_log_line(line)
                current = self.get_run(run_id)
                combined = current.combined_log + line
                updates: dict[str, Any] = {"combined_log": combined}
                if progress_pct > current.progress_pct and current.status not in TERMINAL_RUN_STATUSES:
                    updates["progress_pct"] = progress_pct
                if stage and current.status not in TERMINAL_RUN_STATUSES:
                    updates["stage"] = stage
                self._update_run(run_id, **updates)

        stream.close()

    def _update_run(self, run_id: str, **changes: Any) -> None:
        with self._lock:
            snapshot = self._runs.get(run_id)
            if snapshot is None:
                raise KeyError(f"Unknown project run: {run_id}")
            for key, value in changes.items():
                setattr(snapshot, key, value)
            updated = _copy_snapshot(snapshot)
        self._persist_snapshot(updated)

    def _persist_snapshot(self, snapshot: ProjectRunSnapshot) -> None:
        Path(snapshot.status_path).write_text(
            json.dumps(asdict(snapshot), indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _load_existing_runs(self) -> None:
        projects_root = self.project_storage.projects_root
        if not projects_root.exists():
            return

        discovered_runs: dict[str, ProjectRunSnapshot] = {}
        for status_path in sorted(projects_root.rglob("status.json")):
            snapshot = self._load_snapshot_from_status_file(status_path)
            if snapshot is None:
                continue
            discovered_runs[snapshot.id] = snapshot

        with self._lock:
            self._runs.update(discovered_runs)

    def _load_snapshot_from_status_file(self, status_path: Path) -> ProjectRunSnapshot | None:
        try:
            payload = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

        if not isinstance(payload, dict):
            return None

        required_fields = {
            "id",
            "project_slug",
            "capture_name",
            "status",
            "progress_pct",
            "stage",
            "command",
            "script_path",
            "workspace_dir",
            "artifacts_dir",
            "logs_dir",
            "stdout_log_path",
            "stderr_log_path",
            "status_path",
        }
        if not required_fields.issubset(payload):
            return None

        try:
            return ProjectRunSnapshot(
                id=str(payload["id"]),
                project_slug=str(payload["project_slug"]),
                capture_name=str(payload["capture_name"]),
                keep_process_dir=bool(payload.get("keep_process_dir", False)),
                status=str(payload["status"]),
                progress_pct=int(payload["progress_pct"]),
                stage=str(payload["stage"]),
                command=[str(part) for part in payload["command"]],
                script_path=str(payload["script_path"]),
                workspace_dir=str(payload["workspace_dir"]),
                artifacts_dir=str(payload["artifacts_dir"]),
                logs_dir=str(payload["logs_dir"]),
                stdout_log_path=str(payload["stdout_log_path"]),
                stderr_log_path=str(payload["stderr_log_path"]),
                status_path=str(payload.get("status_path") or status_path),
                started_at=_optional_str(payload.get("started_at")),
                finished_at=_optional_str(payload.get("finished_at")),
                exit_code=_optional_int(payload.get("exit_code")),
                output_path=_optional_str(payload.get("output_path")),
                preview_path=_optional_str(payload.get("preview_path")),
                preview_log_path=_optional_str(payload.get("preview_log_path")),
                preview_error=_optional_str(payload.get("preview_error")),
                error_message=_optional_str(payload.get("error_message")),
                combined_log=_optional_str(payload.get("combined_log")) or "",
            )
        except (TypeError, ValueError):
            return None

    def _collect_capture_frame_sources(self, capture_root: Path) -> list[tuple[str, Path]]:
        if not capture_root.exists() or not capture_root.is_dir():
            raise FileNotFoundError(f"Capture directory not found: {capture_root}")

        frame_sources: list[tuple[str, Path]] = []
        missing_directories: list[str] = []
        for frame_type, directory_name in FRAME_TYPE_TO_CAPTURE_DIR.items():
            source_dir = capture_root / directory_name
            if not source_dir.is_dir():
                missing_directories.append(directory_name)
                continue
            files = sorted(path for path in source_dir.iterdir() if path.is_file())
            if not files:
                missing_directories.append(directory_name)
                continue
            frame_sources.extend((frame_type, file_path) for file_path in files)

        if missing_directories:
            raise ValueError(
                "Capture must contain files in directories: " + ", ".join(sorted(set(missing_directories)))
            )
        return frame_sources

    def _generate_preview_image(
        self,
        *,
        executable_parts: Sequence[str],
        output_path: Path,
        artifacts_dir: Path,
        logs_dir: Path,
    ) -> tuple[str | None, str | None, str | None]:
        """Ask Siril to export a stretched JPEG preview next to the FITS artifacts."""
        output_path = output_path.resolve()
        artifacts_dir = artifacts_dir.resolve()
        logs_dir = logs_dir.resolve()
        executable_name = Path(executable_parts[0]).name.lower()
        preview_log_path = logs_dir / "preview_export.log"
        if "siril" not in executable_name:
            preview_log_path.write_text(
                "Preview export skipped because the configured executable is not a Siril binary.\n",
                encoding="utf-8",
            )
            return None, str(preview_log_path), "Preview export skipped: configured executable is not Siril."

        preview_basename = f"{output_path.stem}_preview"
        preview_path = artifacts_dir / f"{preview_basename}.jpg"
        script_path = (artifacts_dir / "__tsn_preview_export.ssf").resolve()
        script_path.write_text(
            "\n".join(
                [
                    "requires 1.4.0",
                    f'load "{output_path.name}"',
                    "autostretch -linked",
                    f'savejpg "{preview_basename}" 95',
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        try:
            completed = subprocess.run(
                [*executable_parts, "-d", str(artifacts_dir), "-s", str(script_path)],
                text=True,
                capture_output=True,
                check=False,
            )
            preview_log_path.write_text(
                f"command: {' '.join([*executable_parts, '-d', str(artifacts_dir), '-s', str(script_path)])}\n"
                f"exit_code: {completed.returncode}\n\n"
                f"[stdout]\n{completed.stdout}\n\n"
                f"[stderr]\n{completed.stderr}\n",
                encoding="utf-8",
            )
            if completed.returncode != 0:
                return None, str(preview_log_path), f"Preview export failed with exit code {completed.returncode}."
            if not preview_path.exists():
                return None, str(preview_log_path), "Preview export finished without producing a JPEG file."
            return str(preview_path), str(preview_log_path), None
        finally:
            try:
                script_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _cleanup_process_directory(self, workspace_dir: Path) -> None:
        process_dir = workspace_dir / "process"
        if process_dir.exists() and process_dir.is_dir():
            shutil.rmtree(process_dir, ignore_errors=True)


def _normalize_executable(executable: str | Sequence[str]) -> tuple[str, ...]:
    """Normalize a configured executable value into subprocess-ready argv parts."""
    if isinstance(executable, str):
        normalized = executable.strip()
        if not normalized:
            raise ValueError("Siril executable must not be empty.")
        return (normalized,)

    normalized_parts = tuple(str(part).strip() for part in executable if str(part).strip())
    if not normalized_parts:
        raise ValueError("Siril executable must not be empty.")
    return normalized_parts


def _extract_executable_parts(command: Sequence[str]) -> tuple[str, ...]:
    """Recover the executable prefix from a previously built Siril command."""
    if "-d" in command:
        split_index = command.index("-d")
        return tuple(command[:split_index])
    return tuple(command)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value)
    return normalized if normalized else None


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _copy_snapshot(snapshot: ProjectRunSnapshot) -> ProjectRunSnapshot:
    return ProjectRunSnapshot(**asdict(snapshot))


def _progress_from_log_line(line: str) -> tuple[int, str | None]:
    """Map broad Siril log hints to coarse GUI progress stages."""
    text = line.lower()
    if "register" in text or "align" in text:
        return 65, "Registering frames"
    if "stack" in text:
        return 82, "Stacking frames"
    if "calibr" in text or "preprocess" in text:
        return 50, "Calibrating frames"
    if "convert" in text or "debayer" in text:
        return 40, "Preparing lights"
    if "result" in text:
        return 90, "Writing result"
    return 30, None


def _utc_timestamp() -> str:
    return _utc_now().isoformat(timespec="seconds")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
