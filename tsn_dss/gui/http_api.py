from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from typing import Any

try:
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover - exercised only when optional dependency is missing
    Image = None
    ImageOps = None

from ..engine.projects import ProjectStorage
from ..engine.project_processing import DEFAULT_SIRIL_EXECUTABLE, ProjectRunManager
from ..engine.siril import DEFAULT_OSC_SCRIPT_PATH


@dataclass(slots=True)
class ApiContext:
    projects_root: Path
    run_manager: ProjectRunManager

    @property
    def storage(self) -> ProjectStorage:
        return ProjectStorage(self.projects_root)


def create_http_server(
    *,
    host: str,
    port: int,
    projects_root: str | Path,
) -> ThreadingHTTPServer:
    storage = ProjectStorage(Path(projects_root))
    context = ApiContext(
        projects_root=Path(projects_root),
        run_manager=ProjectRunManager(project_storage=storage),
    )
    handler_class = _build_handler(context)
    return ThreadingHTTPServer((host, port), handler_class)


def run_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    projects_root: str | Path = "projects",
) -> None:
    server = create_http_server(host=host, port=port, projects_root=projects_root)
    print(
        f"TSN DSS API listening on http://{host}:{port} "
        f"(projects_root={Path(projects_root).resolve()})"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping TSN DSS API...")
    finally:
        server.server_close()


def _build_handler(context: ApiContext) -> type[BaseHTTPRequestHandler]:
    class TsnDssApiHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed_url = urlsplit(self.path)
            path = parsed_url.path
            query_params = parse_qs(parsed_url.query, keep_blank_values=True)

            if path == "/api/health":
                self._write_json(
                    HTTPStatus.OK,
                    {
                        "status": "ok",
                        "service": "tsn-dss-api",
                        "projects_root": str(context.projects_root.resolve()),
                        "default_siril_executable": os.environ.get("TSN_DSS_SIRIL_EXECUTABLE", "siril-cli"),
                    },
                )
                return

            if path == "/api/projects":
                self._write_json(
                    HTTPStatus.OK,
                    {
                        "projects": [
                            _project_to_dict(project)
                            for project in context.storage.list_projects()
                        ]
                    },
                )
                return

            if path.startswith("/api/projects/") and "/captures/" in path and "/files/" in path:
                prefix = "/api/projects/"
                remainder = unquote(path[len(prefix):])
                project_slug, _, capture_remainder = remainder.partition("/captures/")
                capture_name, _, relative_path = capture_remainder.partition("/files/")
                if not project_slug or not capture_name or not relative_path:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "capture_file_not_found", "message": f"Unknown capture file endpoint: {path}"},
                    )
                    return
                try:
                    _, file_path = context.storage.resolve_capture_file(project_slug, capture_name, relative_path)
                except ValueError:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_capture_file_path", "message": "Capture file path escapes capture root."},
                    )
                    return
                except FileNotFoundError:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "file_missing", "message": "Capture file is not available."},
                    )
                    return
                self._write_filesystem_file(file_path, fallback_message="Capture file is not available.")
                return

            if path.startswith("/api/projects/") and "/captures/" in path and "/thumbnails/" in path:
                prefix = "/api/projects/"
                remainder = unquote(path[len(prefix):])
                project_slug, _, capture_remainder = remainder.partition("/captures/")
                capture_name, _, relative_path = capture_remainder.partition("/thumbnails/")
                if not project_slug or not capture_name or not relative_path:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "capture_thumbnail_not_found", "message": f"Unknown capture thumbnail endpoint: {path}"},
                    )
                    return

                try:
                    _, source_path = context.storage.resolve_capture_file(project_slug, capture_name, relative_path)
                except ValueError:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_capture_thumbnail_path", "message": "Capture thumbnail path escapes capture root."},
                    )
                    return
                except FileNotFoundError:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "file_missing", "message": "Capture file is not available."},
                    )
                    return

                requested_size = query_params.get("size", ["384"])[0]
                try:
                    thumbnail_size = max(64, min(1024, int(requested_size)))
                except ValueError:
                    thumbnail_size = 384

                try:
                    thumbnail_path = _ensure_capture_thumbnail(
                        context.storage,
                        project_slug=project_slug,
                        capture_name=capture_name,
                        source_path=source_path,
                        size=thumbnail_size,
                    )
                except RuntimeError as error:
                    self._write_json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "thumbnail_support_unavailable", "message": str(error)},
                    )
                    return
                except OSError as error:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "thumbnail_generation_failed", "message": str(error)},
                    )
                    return

                self._write_filesystem_file(thumbnail_path, fallback_message="Capture thumbnail is not available.")
                return

            if path.startswith("/api/projects/") and "/captures/" in path:
                prefix = "/api/projects/"
                remainder = unquote(path[len(prefix):])
                project_slug, _, capture_name = remainder.partition("/captures/")
                if not project_slug or not capture_name:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "capture_not_found", "message": f"Unknown capture endpoint: {path}"},
                    )
                    return
                try:
                    capture = context.storage.describe_capture(project_slug, capture_name)
                except FileNotFoundError:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "capture_not_found", "message": f"Unknown capture: {capture_name}"},
                    )
                    return
                self._write_json(
                    HTTPStatus.OK,
                    {"capture": _capture_to_dict(capture)},
                )
                return

            if path.startswith("/api/projects/"):
                project_slug = unquote(path.removeprefix("/api/projects/")).split("/", 1)[0]
                project = context.storage.get_project(project_slug)
                if project is None:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {
                            "error": "project_not_found",
                            "message": f"Unknown project: {project_slug}",
                        },
                    )
                    return

                self._write_json(
                    HTTPStatus.OK,
                    {
                        "project": _project_to_dict(project),
                    },
                )
                return

            if path.startswith("/api/project-runs/"):
                run_path = unquote(path.removeprefix("/api/project-runs/"))
                run_id, suffix = _split_run_path(run_path)
                try:
                    snapshot = context.run_manager.get_run(run_id)
                except KeyError:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {
                            "error": "run_not_found",
                            "message": f"Unknown run: {run_id}",
                        },
                    )
                    return

                if suffix == "preview":
                    self._write_run_file(snapshot.preview_path, fallback_message="Preview image is not available for this run.")
                    return

                if suffix == "output":
                    self._write_run_file(snapshot.output_path, fallback_message="Output FITS is not available for this run.")
                    return

                if suffix == "preview-log":
                    self._write_run_file(snapshot.preview_log_path, fallback_message="Preview export log is not available for this run.")
                    return

                if suffix and suffix.startswith("artifacts/"):
                    relative_artifact_path = suffix.removeprefix("artifacts/")
                    try:
                        artifact_path = _resolve_run_artifact_file(snapshot, relative_artifact_path)
                    except ValueError:
                        self._write_json(
                            HTTPStatus.BAD_REQUEST,
                            {"error": "invalid_run_artifact_path", "message": "Run artifact path escapes artifacts root."},
                        )
                        return
                    except FileNotFoundError:
                        self._write_json(
                            HTTPStatus.NOT_FOUND,
                            {"error": "run_artifact_missing", "message": "Run artifact is not available for this run."},
                        )
                        return

                    self._write_filesystem_file(artifact_path, fallback_message="Run artifact is not available for this run.")
                    return

                if suffix is not None:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {
                            "error": "run_asset_not_found",
                            "message": f"Unknown run asset: {suffix}",
                        },
                    )
                    return

                self._write_json(HTTPStatus.OK, {"run": _run_to_dict(snapshot)})
                return

            if path == "/api/project-runs":
                project_slug = self._get_query_param("project_slug")
                capture_name = self._get_query_param("capture_name")
                runs = context.run_manager.list_runs(project_slug=project_slug, capture_name=capture_name)
                self._write_json(
                    HTTPStatus.OK,
                    {"runs": [_run_to_dict(run) for run in runs]},
                )
                return

            self._write_json(
                HTTPStatus.NOT_FOUND,
                {
                    "error": "not_found",
                    "message": f"Unknown endpoint: {path}",
                },
            )

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            payload = self._read_json_body()
            if payload is None:
                return

            if path == "/api/projects":
                project_slug = str(payload.get("slug", "")).strip()
                if not project_slug:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_project_slug", "message": "Field 'slug' is required."},
                    )
                    return

                try:
                    project = context.storage.create_project(project_slug)
                except Exception as error:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "project_create_failed", "message": str(error)},
                    )
                    return
                self._write_json(HTTPStatus.CREATED, {"project": _project_to_dict(project)})
                return

            if path.startswith("/api/projects/") and path.endswith("/sky-target"):
                prefix = "/api/projects/"
                suffix = "/sky-target"
                project_slug = unquote(path[len(prefix):-len(suffix)])
                sky_target_value = payload.get("sky_target")
                sky_target = str(sky_target_value).strip() if sky_target_value is not None else None
                try:
                    project = context.storage.set_project_sky_target(project_slug, sky_target)
                except Exception as error:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "project_target_update_failed", "message": str(error)},
                    )
                    return
                self._write_json(HTTPStatus.OK, {"project": _project_to_dict(project)})
                return

            if path == "/api/import-capture":
                project_slug = str(payload.get("project_slug", "")).strip()
                capture_name = str(payload.get("capture_name", "")).strip()
                source_dir = str(payload.get("source_dir", "")).strip()
                move = bool(payload.get("move", False))

                if not project_slug or not capture_name or not source_dir:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "error": "invalid_import_request",
                            "message": "Fields 'project_slug', 'capture_name' and 'source_dir' are required.",
                        },
                    )
                    return

                try:
                    destination = context.storage.import_capture(
                        project_slug,
                        capture_name,
                        source_dir,
                        move=move,
                    )
                except Exception as error:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "capture_import_failed", "message": str(error)},
                    )
                    return
                project = context.storage.get_project(project_slug)
                self._write_json(
                    HTTPStatus.CREATED,
                    {
                        "capture_root": str(destination),
                        "project": _project_to_dict(project),
                    },
                )
                return

            if path == "/api/project-runs":
                project_slug = str(payload.get("project_slug", "")).strip()
                capture_name = str(payload.get("capture_name", "")).strip()
                script_path = str(payload.get("script_path", "")).strip() or str(DEFAULT_OSC_SCRIPT_PATH)
                executable = payload.get("executable", None)
                executable_parts = payload.get("executable_parts", None)
                keep_process_dir = bool(payload.get("keep_process_dir", False))

                if not project_slug or not capture_name:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {
                            "error": "invalid_run_request",
                            "message": "Fields 'project_slug' and 'capture_name' are required.",
                        },
                    )
                    return

                selected_executable = executable_parts if executable_parts is not None else (
                    executable
                    if executable is not None and str(executable).strip()
                    else os.environ.get("TSN_DSS_SIRIL_EXECUTABLE", list(DEFAULT_SIRIL_EXECUTABLE)[0])
                )

                try:
                    snapshot = context.run_manager.start_osc_preprocessing(
                        project_slug=project_slug,
                        capture_name=capture_name,
                        executable=selected_executable,
                        script_path=script_path,
                        keep_process_dir=keep_process_dir,
                    )
                except Exception as error:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "run_start_failed", "message": str(error)},
                    )
                    return

                self._write_json(HTTPStatus.CREATED, {"run": _run_to_dict(snapshot)})
                return

            if path.startswith("/api/project-runs/") and path.endswith("/generate-preview"):
                prefix = "/api/project-runs/"
                suffix = "/generate-preview"
                run_id = unquote(path[len(prefix):-len(suffix)])
                try:
                    snapshot = context.run_manager.regenerate_preview(run_id)
                except KeyError:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "run_not_found", "message": f"Unknown run: {run_id}"},
                    )
                    return
                except Exception as error:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "preview_generation_failed", "message": str(error)},
                    )
                    return

                self._write_json(HTTPStatus.OK, {"run": _run_to_dict(snapshot)})
                return

            self._write_json(
                HTTPStatus.NOT_FOUND,
                {
                    "error": "not_found",
                    "message": f"Unknown endpoint: {path}",
                },
            )

        def do_DELETE(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]

            if path.startswith("/api/project-runs/"):
                run_path = unquote(path.removeprefix("/api/project-runs/"))
                run_id, suffix = _split_run_path(run_path)
                if suffix is not None:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {
                            "error": "not_found",
                            "message": f"Unknown endpoint: {path}",
                        },
                    )
                    return
                try:
                    context.run_manager.delete_run(run_id)
                except KeyError:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "run_not_found", "message": f"Unknown run: {run_id}"},
                    )
                    return
                except Exception as error:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "run_delete_failed", "message": str(error)},
                    )
                    return

                self._write_json(
                    HTTPStatus.OK,
                    {"deleted": True, "run_id": run_id},
                )
                return

            if path.startswith("/api/projects/"):
                project_slug = unquote(path.removeprefix("/api/projects/")).split("/", 1)[0]
                try:
                    context.storage.delete_project(project_slug)
                except FileNotFoundError:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {
                            "error": "project_not_found",
                            "message": f"Unknown project: {project_slug}",
                        },
                    )
                    return
                except Exception as error:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "project_delete_failed", "message": str(error)},
                    )
                    return

                self._write_json(
                    HTTPStatus.OK,
                    {"deleted": True, "project_slug": project_slug},
                )
                return

            self._write_json(
                HTTPStatus.NOT_FOUND,
                {
                    "error": "not_found",
                    "message": f"Unknown endpoint: {path}",
                },
            )

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(HTTPStatus.NO_CONTENT)
            self._send_cors_headers()
            self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _read_json_body(self) -> dict[str, Any] | None:
            content_length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"
            try:
                decoded = raw_body.decode("utf-8")
                payload = json.loads(decoded or "{}")
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid_json", "message": "Request body must be valid JSON."},
                )
                return None

            if not isinstance(payload, dict):
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "invalid_json", "message": "JSON body must be an object."},
                )
                return None
            return payload

        def _write_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(int(status))
            self._send_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_cors_headers(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")

        def _get_query_param(self, key: str) -> str | None:
            query = urlsplit(self.path).query
            values = parse_qs(query, keep_blank_values=True).get(key)
            if not values:
                return None
            return unquote(values[0])

        def _write_run_file(self, path_value: str | None, *, fallback_message: str) -> None:
            if not path_value:
                self._write_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "run_file_missing", "message": fallback_message},
                )
                return

            file_path = Path(path_value)
            if not file_path.exists() or not file_path.is_file():
                self._write_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "run_file_missing", "message": fallback_message},
                )
                return

            content = file_path.read_bytes()
            content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self._send_cors_headers()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Content-Disposition", f'inline; filename="{file_path.name}"')
            self.end_headers()
            self.wfile.write(content)

        def _write_filesystem_file(self, file_path: Path, *, fallback_message: str) -> None:
            if not file_path.exists() or not file_path.is_file():
                self._write_json(
                    HTTPStatus.NOT_FOUND,
                    {"error": "file_missing", "message": fallback_message},
                )
                return

            content = file_path.read_bytes()
            content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self._send_cors_headers()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Content-Disposition", f'inline; filename="{file_path.name}"')
            self.end_headers()
            self.wfile.write(content)

    return TsnDssApiHandler


def _project_to_dict(project: Any) -> dict[str, Any]:
    return {
        "slug": project.slug,
        "project_root": str(project.project_root),
        "captures_dir": str(project.captures_dir),
        "runs_dir": str(project.runs_dir),
        "capture_count": len(project.capture_names),
        "run_count": len(project.run_names),
        "capture_names": list(project.capture_names),
        "run_names": list(project.run_names),
        "sky_target": project.sky_target,
    }


def _capture_to_dict(capture: Any) -> dict[str, Any]:
    return {
        "project_slug": capture.project_slug,
        "capture_name": capture.capture_name,
        "capture_root": str(capture.capture_root),
        "folders": [
            {
                "name": folder.name,
                "file_count": folder.file_count,
                "files": [
                    {
                        "name": file_entry.name,
                        "relative_path": file_entry.relative_path,
                        "size_bytes": file_entry.size_bytes,
                        "suffix": file_entry.suffix,
                    }
                    for file_entry in folder.files
                ],
            }
            for folder in capture.folders
        ],
    }


def _run_to_dict(snapshot: Any) -> dict[str, Any]:
    payload = asdict(snapshot)
    payload["artifact_images"] = _list_run_artifact_images(snapshot)
    return payload


def _list_run_artifact_images(snapshot: Any) -> list[dict[str, Any]]:
    artifacts_dir = Path(snapshot.artifacts_dir)
    if not artifacts_dir.exists() or not artifacts_dir.is_dir():
        return []

    preview_path = Path(snapshot.preview_path).resolve() if snapshot.preview_path else None
    supported_suffixes = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
    images: list[dict[str, Any]] = []
    for artifact_path in sorted((path for path in artifacts_dir.rglob("*") if path.is_file()), key=lambda path: path.name.lower()):
        if artifact_path.suffix.lower() not in supported_suffixes:
            continue
        if preview_path is not None and artifact_path.resolve() == preview_path:
            continue

        images.append(
            {
                "name": artifact_path.name,
                "relative_path": str(artifact_path.relative_to(artifacts_dir)),
                "size_bytes": artifact_path.stat().st_size,
                "suffix": artifact_path.suffix,
            }
        )
    return images


def _resolve_run_artifact_file(snapshot: Any, relative_path: str | Path) -> Path:
    artifacts_root = Path(snapshot.artifacts_dir).resolve()
    artifact_path = (artifacts_root / Path(relative_path)).resolve()
    try:
        artifact_path.relative_to(artifacts_root)
    except ValueError as error:
        raise ValueError("Run artifact path escapes artifacts root.") from error

    if not artifact_path.exists() or not artifact_path.is_file():
        raise FileNotFoundError(f"Run artifact not found: {relative_path}")

    return artifact_path


def _split_run_path(run_path: str) -> tuple[str, str | None]:
    parts = [part for part in run_path.split("/") if part]
    if not parts:
        return "", None
    if len(parts) == 1:
        return parts[0], None
    return parts[0], "/".join(parts[1:])


def _ensure_capture_thumbnail(
    storage: ProjectStorage,
    *,
    project_slug: str,
    capture_name: str,
    source_path: Path,
    size: int,
) -> Path:
    if Image is None or ImageOps is None:
        raise RuntimeError("Thumbnail support requires Pillow. Install dependencies from requirements.txt.")

    project_layout = storage.ensure_project(project_slug)
    cache_root = project_layout.project_root / ".cache" / "capture_thumbnails" / capture_name
    relative_source = source_path.relative_to((project_layout.captures_dir / capture_name).resolve())
    thumbnail_dir = (cache_root / relative_source.parent).resolve()
    thumbnail_dir.mkdir(parents=True, exist_ok=True)

    source_stat = source_path.stat()
    fingerprint = hashlib.sha1(
        f"{relative_source.as_posix()}:{source_stat.st_mtime_ns}:{source_stat.st_size}:{size}".encode("utf-8")
    ).hexdigest()[:12]
    thumbnail_name = f"{source_path.stem}__{fingerprint}.jpg"
    thumbnail_path = thumbnail_dir / thumbnail_name

    if thumbnail_path.exists() and thumbnail_path.is_file():
        return thumbnail_path

    for stale_path in thumbnail_dir.glob(f"{source_path.stem}__*.jpg"):
        if stale_path.name != thumbnail_name:
            stale_path.unlink(missing_ok=True)

    with Image.open(source_path) as image:
        prepared = ImageOps.exif_transpose(image)
        prepared.thumbnail((size, size))
        if prepared.mode not in ("RGB", "L"):
            prepared = prepared.convert("RGB")
        elif prepared.mode == "L":
            prepared = prepared.convert("RGB")
        prepared.save(thumbnail_path, format="JPEG", quality=82, optimize=True)

    return thumbnail_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the TSN DSS local HTTP API.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--projects-root", default="projects")
    args = parser.parse_args(argv)

    run_server(host=args.host, port=args.port, projects_root=args.projects_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
