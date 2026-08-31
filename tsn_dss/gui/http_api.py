from __future__ import annotations
"""Local HTTP API that bridges the TSN DSS engine to the browser frontend."""

import argparse
import hashlib
import json
import mimetypes
import os
import sqlite3
import uuid
from contextlib import contextmanager
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

from ..domain.models import MosaicPanel, MosaicPlan, Site
from ..engine.astronomy import AstronomicalConditionsService, AstronomicalTargetContext
from ..engine.projects import ProjectStorage
from ..engine.project_processing import DEFAULT_SIRIL_EXECUTABLE, ProjectRunManager
from ..engine.siril import DEFAULT_OSC_SCRIPT_PATH
from ..engine.sqlite import MosaicRepository, PlanningRepository, connect_database, initialize_database
from ..engine.telescope import TelescopeStateService
from ..engine.weather import OpenMeteoForecastClient


@dataclass(slots=True)
class ApiContext:
    """Runtime dependencies shared by every HTTP handler instance."""
    projects_root: Path
    database_path: Path
    core_content_path: Path
    run_manager: ProjectRunManager
    telescope_service: TelescopeStateService
    weather_client: OpenMeteoForecastClient
    astronomy_service: AstronomicalConditionsService

    @property
    def storage(self) -> ProjectStorage:
        return ProjectStorage(self.projects_root)

    @contextmanager
    def open_database(self):
        connection = connect_database(self.database_path)
        try:
            yield connection
        finally:
            connection.close()


def create_http_server(
    *,
    host: str,
    port: int,
    projects_root: str | Path,
    database_path: str | Path | None = None,
    weather_client: OpenMeteoForecastClient | None = None,
    astronomy_service: AstronomicalConditionsService | None = None,
) -> ThreadingHTTPServer:
    """Create a local threaded API server wired to the project workspace and SQLite db."""
    resolved_projects_root = Path(projects_root)
    resolved_projects_root.mkdir(parents=True, exist_ok=True)
    resolved_database_path = Path(database_path) if database_path is not None else (resolved_projects_root / "tsn_dss.db")
    bootstrap_connection = initialize_database(resolved_database_path)
    bootstrap_connection.close()
    storage = ProjectStorage(resolved_projects_root)
    context = ApiContext(
        projects_root=resolved_projects_root,
        database_path=resolved_database_path,
        core_content_path=_default_core_content_path(),
        run_manager=ProjectRunManager(project_storage=storage),
        telescope_service=TelescopeStateService(),
        weather_client=weather_client or OpenMeteoForecastClient(),
        astronomy_service=astronomy_service or AstronomicalConditionsService(),
    )
    handler_class = _build_handler(context)
    return ThreadingHTTPServer((host, port), handler_class)


def run_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    projects_root: str | Path = "projects",
    database_path: str | Path | None = None,
) -> None:
    """Run the local TSN DSS HTTP API until interrupted."""
    server = create_http_server(host=host, port=port, projects_root=projects_root, database_path=database_path)
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
    """Build a request handler class bound to one concrete API context."""
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

            if path == "/api/core-content":
                self._write_json(
                    HTTPStatus.OK,
                    _read_core_content(context.core_content_path),
                )
                return

            if path == "/api/sites":
                with context.open_database() as connection:
                    repository = PlanningRepository(connection)
                    sites = repository.list_sites()
                self._write_json(
                    HTTPStatus.OK,
                    {
                        "sites": [_site_to_dict(site) for site in sites],
                        "active_site_id": context.telescope_service.get_active_site_id(),
                    },
                )
                return

            if path == "/api/site-forecast":
                requested_site_id = self._get_query_param("site_id") or context.telescope_service.get_active_site_id()
                if not requested_site_id:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "site_id_required", "message": "Provide ?site_id=... or select an active site first."},
                    )
                    return

                with context.open_database() as connection:
                    repository = PlanningRepository(connection)
                    site = repository.get_site(requested_site_id)

                if site is None:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "site_not_found", "message": f"Unknown site: {requested_site_id}"},
                    )
                    return

                if site.latitude_deg is None or site.longitude_deg is None:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "site_coordinates_missing", "message": "Selected site must define latitude and longitude."},
                    )
                    return

                try:
                    forecast = context.weather_client.fetch_site_forecast(
                        site,
                        forecast_hours=_coerce_optional_query_int(self._get_query_param("forecast_hours"), fallback=24) or 24,
                    )
                except ValueError as error:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_site_forecast_request", "message": str(error)},
                    )
                    return
                except Exception as error:
                    self._write_json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "forecast_provider_unavailable", "message": str(error)},
                    )
                    return

                self._write_json(HTTPStatus.OK, {"forecast": forecast.to_dict()})
                return

            if path == "/api/astronomical-conditions":
                requested_site_id = self._get_query_param("site_id") or context.telescope_service.get_active_site_id()
                if not requested_site_id:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "site_id_required", "message": "Provide ?site_id=... or select an active site first."},
                    )
                    return

                with context.open_database() as connection:
                    planning_repository = PlanningRepository(connection)
                    site = planning_repository.get_site(requested_site_id)
                    target_context = _resolve_astronomical_target_context(
                        query_params,
                        planning_repository=planning_repository,
                        mosaic_repository=MosaicRepository(connection),
                        telescope_service=context.telescope_service,
                    )

                if site is None:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "site_not_found", "message": f"Unknown site: {requested_site_id}"},
                    )
                    return

                if site.latitude_deg is None or site.longitude_deg is None:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "site_coordinates_missing", "message": "Selected site must define latitude and longitude."},
                    )
                    return

                try:
                    conditions = context.astronomy_service.fetch_conditions(
                        site,
                        reference_time_utc=self._get_query_param("time_utc"),
                        target=target_context,
                        min_target_altitude_deg=_coerce_optional_query_float(
                            self._get_query_param("min_target_altitude_deg"),
                            fallback=30.0,
                        )
                        or 30.0,
                        forecast_hours=_coerce_optional_query_int(self._get_query_param("forecast_hours"), fallback=24) or 24,
                    )
                except ValueError as error:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_astronomical_conditions_request", "message": str(error)},
                    )
                    return
                except Exception as error:
                    self._write_json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "astronomical_conditions_unavailable", "message": str(error)},
                    )
                    return

                self._write_json(HTTPStatus.OK, {"conditions": conditions.to_dict()})
                return

            if path == "/api/telescope/state":
                self._write_json(
                    HTTPStatus.OK,
                    context.telescope_service.get_snapshot().to_dict(),
                )
                return

            if path.startswith("/api/sites/"):
                site_id = unquote(path.removeprefix("/api/sites/")).split("/", 1)[0]
                with context.open_database() as connection:
                    repository = PlanningRepository(connection)
                    site = repository.get_site(site_id)
                if site is None:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "site_not_found", "message": f"Unknown site: {site_id}"},
                    )
                    return
                self._write_json(HTTPStatus.OK, {"site": _site_to_dict(site)})
                return

            if path == "/api/telescope/adapters":
                self._write_json(
                    HTTPStatus.OK,
                    {
                        "active_adapter_id": context.telescope_service.get_active_adapter_id(),
                        "adapters": [
                            _telescope_adapter_descriptor_to_dict(descriptor)
                            for descriptor in context.telescope_service.list_available_adapters()
                        ],
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

            if path == "/api/mosaics":
                project_slug = self._get_query_param("project_slug")
                with context.open_database() as connection:
                    repository = MosaicRepository(connection)
                    mosaics = repository.list_mosaic_plans(project_slug=project_slug)
                self._write_json(
                    HTTPStatus.OK,
                    {"mosaics": [_mosaic_plan_to_dict(plan) for plan in mosaics]},
                )
                return

            if path.startswith("/api/mosaics/"):
                mosaic_path = unquote(path.removeprefix("/api/mosaics/"))
                mosaic_id, suffix = _split_run_path(mosaic_path)
                with context.open_database() as connection:
                    repository = MosaicRepository(connection)
                    plan = repository.get_mosaic_plan(mosaic_id)

                if plan is None:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "mosaic_not_found", "message": f"Unknown mosaic: {mosaic_id}"},
                    )
                    return

                if suffix == "panels":
                    self._write_json(
                        HTTPStatus.OK,
                        {"panels": [_mosaic_panel_to_dict(panel) for panel in plan.panels]},
                    )
                    return

                if suffix is not None:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "mosaic_asset_not_found", "message": f"Unknown mosaic asset: {suffix}"},
                    )
                    return

                self._write_json(HTTPStatus.OK, {"mosaic": _mosaic_plan_to_dict(plan)})
                return

            if path.startswith("/api/mosaic-panels/"):
                panel_id = unquote(path.removeprefix("/api/mosaic-panels/")).split("/", 1)[0]
                with context.open_database() as connection:
                    repository = MosaicRepository(connection)
                    panel = repository.get_mosaic_panel(panel_id)
                if panel is None:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "mosaic_panel_not_found", "message": f"Unknown mosaic panel: {panel_id}"},
                    )
                    return
                self._write_json(HTTPStatus.OK, {"panel": _mosaic_panel_to_dict(panel)})
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

            if path == "/api/sites":
                try:
                    site = _site_from_payload(payload)
                except ValueError as error:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_site_payload", "message": str(error)},
                    )
                    return

                try:
                    with context.open_database() as connection:
                        repository = PlanningRepository(connection)
                        created = repository.create_site(site)
                except Exception as error:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "site_create_failed", "message": str(error)},
                    )
                    return

                self._write_json(HTTPStatus.CREATED, {"site": _site_to_dict(created)})
                return

            if path == "/api/sites/active":
                site_id = _coerce_optional_string(payload.get("site_id"))
                if site_id is None:
                    snapshot = context.telescope_service.set_active_site(None)
                    self._write_json(
                        HTTPStatus.OK,
                        {"active_site_id": None, "snapshot": snapshot.to_dict()},
                    )
                    return

                with context.open_database() as connection:
                    repository = PlanningRepository(connection)
                    site = repository.get_site(site_id)
                if site is None:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "site_not_found", "message": f"Unknown site: {site_id}"},
                    )
                    return

                snapshot = context.telescope_service.set_active_site(site)
                self._write_json(
                    HTTPStatus.OK,
                    {"active_site_id": site.id, "snapshot": snapshot.to_dict()},
                )
                return

            if path == "/api/mosaics":
                try:
                    plan = _mosaic_plan_from_payload(
                        payload,
                        fallback_profile=context.telescope_service.get_snapshot().imaging_profile,
                    )
                except ValueError as error:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_mosaic_request", "message": str(error)},
                    )
                    return

                try:
                    with context.open_database() as connection:
                        repository = MosaicRepository(connection)
                        saved = repository.save_mosaic_plan(plan)
                except Exception as error:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "mosaic_create_failed", "message": str(error)},
                    )
                    return

                self._write_json(HTTPStatus.CREATED, {"mosaic": _mosaic_plan_to_dict(saved)})
                return

            if path.startswith("/api/mosaics/") and not path.endswith("/generate-panels") and not path.endswith("/select-panel"):
                mosaic_id = unquote(path.removeprefix("/api/mosaics/")).split("/", 1)[0]
                try:
                    with context.open_database() as connection:
                        repository = MosaicRepository(connection)
                        current = repository.get_mosaic_plan(mosaic_id)
                        if current is None:
                            raise KeyError(mosaic_id)
                        updated = repository.save_mosaic_plan(
                            _merge_mosaic_plan_payload(current, payload)
                        )
                except KeyError:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "mosaic_not_found", "message": f"Unknown mosaic: {mosaic_id}"},
                    )
                    return
                except Exception as error:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "mosaic_update_failed", "message": str(error)},
                    )
                    return

                self._write_json(HTTPStatus.OK, {"mosaic": _mosaic_plan_to_dict(updated)})
                return

            if path.startswith("/api/mosaics/") and path.endswith("/generate-panels"):
                prefix = "/api/mosaics/"
                suffix = "/generate-panels"
                mosaic_id = unquote(path[len(prefix):-len(suffix)])
                try:
                    with context.open_database() as connection:
                        repository = MosaicRepository(connection)
                        panels = repository.generate_panels(mosaic_id)
                        plan = repository.get_mosaic_plan(mosaic_id)
                except KeyError:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "mosaic_not_found", "message": f"Unknown mosaic: {mosaic_id}"},
                    )
                    return
                except Exception as error:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "mosaic_generation_failed", "message": str(error)},
                    )
                    return

                self._write_json(
                    HTTPStatus.OK,
                    {
                        "mosaic": _mosaic_plan_to_dict(plan),
                        "panels": [_mosaic_panel_to_dict(panel) for panel in panels],
                    },
                )
                return

            if path.startswith("/api/mosaics/") and path.endswith("/select-panel"):
                prefix = "/api/mosaics/"
                suffix = "/select-panel"
                mosaic_id = unquote(path[len(prefix):-len(suffix)])
                panel_id = str(payload.get("panel_id", "")).strip()
                if not panel_id:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_mosaic_selection", "message": "Field 'panel_id' is required."},
                    )
                    return
                try:
                    with context.open_database() as connection:
                        repository = MosaicRepository(connection)
                        plan = repository.select_active_panel(mosaic_id, panel_id)
                except KeyError:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "mosaic_panel_not_found", "message": f"Unknown panel for mosaic: {panel_id}"},
                    )
                    return
                except Exception as error:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "mosaic_selection_failed", "message": str(error)},
                    )
                    return

                self._write_json(HTTPStatus.OK, {"mosaic": _mosaic_plan_to_dict(plan)})
                return

            if path.startswith("/api/mosaic-panels/"):
                panel_id = unquote(path.removeprefix("/api/mosaic-panels/")).split("/", 1)[0]
                try:
                    with context.open_database() as connection:
                        repository = MosaicRepository(connection)
                        current = repository.get_mosaic_panel(panel_id)
                        if current is None:
                            raise KeyError(panel_id)
                        updated = repository.update_mosaic_panel(
                            _merge_mosaic_panel_payload(current, payload)
                        )
                except KeyError:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "mosaic_panel_not_found", "message": f"Unknown mosaic panel: {panel_id}"},
                    )
                    return
                except Exception as error:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "mosaic_panel_update_failed", "message": str(error)},
                    )
                    return

                self._write_json(HTTPStatus.OK, {"panel": _mosaic_panel_to_dict(updated)})
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

            if path.startswith("/api/sites/"):
                site_id = unquote(path.removeprefix("/api/sites/")).split("/", 1)[0]
                try:
                    with context.open_database() as connection:
                        repository = PlanningRepository(connection)
                        current = repository.get_site(site_id)
                        if current is None:
                            raise KeyError(f"Site not found: {site_id}")
                        updated = repository.update_site(_merge_site_payload(current, payload))
                except KeyError as error:
                    self._write_json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "site_not_found", "message": str(error)},
                    )
                    return
                except Exception as error:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "site_update_failed", "message": str(error)},
                    )
                    return

                if context.telescope_service.get_active_site_id() == updated.id:
                    context.telescope_service.set_active_site(updated)

                self._write_json(HTTPStatus.OK, {"site": _site_to_dict(updated)})
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

            if path == "/api/telescope/simulator/state":
                try:
                    snapshot = context.telescope_service.update_simulator_pointing(
                        ra_hours=_coerce_optional_float(payload.get("ra_hours")),
                        dec_deg=_coerce_optional_float(payload.get("dec_deg")),
                        alt_deg=_coerce_optional_float(payload.get("alt_deg")),
                        az_deg=_coerce_optional_float(payload.get("az_deg")),
                        target_name=_coerce_optional_string(payload.get("target_name")),
                        status=_coerce_optional_string(payload.get("status")),
                    )
                except ValueError as error:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_telescope_state", "message": str(error)},
                    )
                    return

                self._write_json(HTTPStatus.OK, snapshot.to_dict())
                return

            if path == "/api/telescope/active-adapter":
                adapter_id = str(payload.get("adapter_id", "")).strip()
                if not adapter_id:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_adapter_id", "message": "Field 'adapter_id' is required."},
                    )
                    return

                try:
                    snapshot = context.telescope_service.set_active_adapter(adapter_id)
                except (KeyError, ValueError) as error:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_adapter_id", "message": str(error)},
                    )
                    return

                self._write_json(
                    HTTPStatus.OK,
                    {
                        "active_adapter_id": context.telescope_service.get_active_adapter_id(),
                        "snapshot": snapshot.to_dict(),
                        "capabilities": _telescope_adapter_capabilities_to_dict(
                            context.telescope_service.get_adapter_capabilities()
                        ),
                    },
                )
                return

            if path == "/api/telescope/planned-pointing":
                try:
                    ra_hours = _coerce_required_float(payload.get("ra_hours"))
                    dec_deg = _coerce_required_float(payload.get("dec_deg"))
                    snapshot = context.telescope_service.set_planned_pointing(
                        ra_hours=ra_hours,
                        dec_deg=dec_deg,
                        target_name=_coerce_optional_string(payload.get("target_name")),
                        source_kind=_coerce_optional_string(payload.get("source_kind")) or "manual",
                        source_id=_coerce_optional_string(payload.get("source_id")),
                    )
                except ValueError as error:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_planned_pointing", "message": str(error)},
                    )
                    return

                self._write_json(HTTPStatus.OK, snapshot.to_dict())
                return

            if path == "/api/telescope/slew-to-planned":
                try:
                    snapshot = context.telescope_service.slew_to_planned_pointing()
                except ValueError as error:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "planned_pointing_unavailable", "message": str(error)},
                    )
                    return

                self._write_json(HTTPStatus.OK, snapshot.to_dict())
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

            if path.startswith("/api/mosaics/"):
                mosaic_id = unquote(path.removeprefix("/api/mosaics/")).split("/", 1)[0]
                try:
                    with context.open_database() as connection:
                        repository = MosaicRepository(connection)
                        repository.delete_mosaic_plan(mosaic_id)
                except Exception as error:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "mosaic_delete_failed", "message": str(error)},
                    )
                    return

                self._write_json(
                    HTTPStatus.OK,
                    {"deleted": True, "mosaic_id": mosaic_id},
                )
                return

            if path.startswith("/api/sites/"):
                site_id = unquote(path.removeprefix("/api/sites/")).split("/", 1)[0]
                try:
                    with context.open_database() as connection:
                        repository = PlanningRepository(connection)
                        repository.delete_site(site_id)
                except Exception as error:
                    self._write_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "site_delete_failed", "message": str(error)},
                    )
                    return

                if context.telescope_service.get_active_site_id() == site_id:
                    context.telescope_service.set_active_site(None)

                self._write_json(
                    HTTPStatus.OK,
                    {"deleted": True, "site_id": site_id},
                )
                return

            if path == "/api/telescope/planned-pointing":
                snapshot = context.telescope_service.clear_planned_pointing()
                self._write_json(
                    HTTPStatus.OK,
                    {"deleted": True, "planned_pointing": snapshot.to_dict().get("planned_pointing")},
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


def _site_to_dict(site: Any) -> dict[str, Any]:
    return {
        "id": site.id,
        "name": site.name,
        "latitude_deg": site.latitude_deg,
        "longitude_deg": site.longitude_deg,
        "elevation_m": site.elevation_m,
        "sqm_mag_arcsec2": site.sqm_mag_arcsec2,
        "bortle_class": site.bortle_class,
        "south_horizon_open": site.south_horizon_open,
        "notes": site.notes,
    }


def _default_core_content_path() -> Path:
    return Path(__file__).resolve().parents[2] / "docs" / "app" / "core-content.json"


def _read_core_content(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return {
            "current_version": "dev",
            "releases": [],
            "todo": [],
        }

    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    if not isinstance(payload, dict):
        raise ValueError("Core content file must contain a JSON object.")

    current_version = str(payload.get("current_version", "dev")).strip() or "dev"
    releases = payload.get("releases", [])
    todo = payload.get("todo", [])

    return {
        "current_version": current_version,
        "releases": releases if isinstance(releases, list) else [],
        "todo": todo if isinstance(todo, list) else [],
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
    """Serialize a processing run and append discoverable artifact images."""
    payload = asdict(snapshot)
    payload["artifact_images"] = _list_run_artifact_images(snapshot)
    return payload


def _telescope_adapter_descriptor_to_dict(descriptor: Any) -> dict[str, Any]:
    return {
        "adapter_id": descriptor.adapter_id,
        "label": descriptor.label,
        "source_kind": descriptor.source_kind,
        "is_simulated": descriptor.is_simulated,
        "capabilities": _telescope_adapter_capabilities_to_dict(descriptor.capabilities),
    }


def _telescope_adapter_capabilities_to_dict(capabilities: Any) -> dict[str, Any]:
    return {
        "can_connect": capabilities.can_connect,
        "can_disconnect": capabilities.can_disconnect,
        "can_manual_pointing": capabilities.can_manual_pointing,
        "can_slew_to_coordinates": capabilities.can_slew_to_coordinates,
        "can_park": capabilities.can_park,
        "can_set_tracking": capabilities.can_set_tracking,
        "can_stream_preview": capabilities.can_stream_preview,
        "can_start_stack": capabilities.can_start_stack,
        "can_run_observation_plans": capabilities.can_run_observation_plans,
    }


def _mosaic_plan_to_dict(plan: Any) -> dict[str, Any]:
    return {
        "id": plan.id,
        "project_slug": plan.project_slug,
        "name": plan.name,
        "target_name": plan.target_name,
        "observation_type": plan.observation_type,
        "filter": plan.filter,
        "imaging_profile_id": plan.imaging_profile_id,
        "imaging_profile_label": plan.imaging_profile_label,
        "fov_width_deg": plan.fov_width_deg,
        "fov_height_deg": plan.fov_height_deg,
        "center_ra_deg": plan.center_ra_deg,
        "center_dec_deg": plan.center_dec_deg,
        "region_width_deg": plan.region_width_deg,
        "region_height_deg": plan.region_height_deg,
        "rotation_deg": plan.rotation_deg,
        "overlap_percent": plan.overlap_percent,
        "status": plan.status,
        "selected_panel_id": plan.selected_panel_id,
        "panels": [_mosaic_panel_to_dict(panel) for panel in plan.panels],
    }


def _mosaic_panel_to_dict(panel: Any) -> dict[str, Any]:
    return {
        "id": panel.id,
        "mosaic_plan_id": panel.mosaic_plan_id,
        "panel_index": panel.panel_index,
        "panel_label": panel.panel_label,
        "center_ra_deg": panel.center_ra_deg,
        "center_dec_deg": panel.center_dec_deg,
        "fov_width_deg": panel.fov_width_deg,
        "fov_height_deg": panel.fov_height_deg,
        "rotation_deg": panel.rotation_deg,
        "row_index": panel.row_index,
        "column_index": panel.column_index,
        "status": panel.status,
        "target_integration_seconds": panel.target_integration_seconds,
        "acquired_integration_seconds": panel.acquired_integration_seconds,
    }


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


def _coerce_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_optional_query_float(value: str | None, *, fallback: float | None = None) -> float | None:
    if value is None or value == "":
        return fallback
    return float(value)


def _coerce_optional_query_int(value: str | None, *, fallback: int | None = None) -> int | None:
    if value is None or value == "":
        return fallback
    return int(value)


def _coerce_query_bool(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_astronomical_target_context(
    query_params: dict[str, list[str]],
    *,
    planning_repository: PlanningRepository,
    mosaic_repository: MosaicRepository,
    telescope_service: TelescopeStateService,
) -> AstronomicalTargetContext | None:
    explicit_ra_deg = _coerce_optional_query_float(_first_query_value(query_params, "target_ra_deg"))
    explicit_dec_deg = _coerce_optional_query_float(_first_query_value(query_params, "target_dec_deg"))
    explicit_target_name = _coerce_optional_string(_first_query_value(query_params, "target_name"))
    if explicit_ra_deg is not None or explicit_dec_deg is not None:
        if explicit_ra_deg is None or explicit_dec_deg is None:
            raise ValueError("Both target_ra_deg and target_dec_deg are required when explicit target coordinates are provided.")
        return AstronomicalTargetContext(
            target_name=explicit_target_name,
            ra_deg=explicit_ra_deg,
            dec_deg=explicit_dec_deg,
            source_kind=_coerce_optional_string(_first_query_value(query_params, "source_kind")) or "manual",
            source_id=_coerce_optional_string(_first_query_value(query_params, "source_id")),
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

    mosaic_panel_id = _coerce_optional_string(_first_query_value(query_params, "mosaic_panel_id"))
    if mosaic_panel_id:
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

    target_id = _coerce_optional_string(_first_query_value(query_params, "target_id"))
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

    if _coerce_query_bool(_first_query_value(query_params, "use_planned_pointing")):
        snapshot = telescope_service.get_snapshot()
        planned = snapshot.planned_pointing
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


def _first_query_value(query_params: dict[str, list[str]], key: str) -> str | None:
    values = query_params.get(key)
    if not values:
        return None
    return unquote(values[0])


def _site_from_payload(payload: dict[str, Any]) -> Any:
    site_id = _coerce_optional_string(payload.get("id")) or f"site:{uuid.uuid4().hex[:12]}"
    name = str(payload.get("name", "")).strip()
    if not name:
        raise ValueError("Field 'name' is required.")

    return Site(
        id=site_id,
        name=name,
        latitude_deg=_coerce_optional_float(payload.get("latitude_deg")),
        longitude_deg=_coerce_optional_float(payload.get("longitude_deg")),
        elevation_m=_coerce_optional_float(payload.get("elevation_m")),
        sqm_mag_arcsec2=_coerce_optional_float(payload.get("sqm_mag_arcsec2")),
        bortle_class=_coerce_optional_int(payload.get("bortle_class")),
        south_horizon_open=bool(payload.get("south_horizon_open", False)),
        notes=_coerce_optional_string(payload.get("notes")),
    )


def _merge_site_payload(current: Any, payload: dict[str, Any]) -> Any:
    return Site(
        id=current.id,
        name=str(payload.get("name", current.name)).strip() or current.name,
        latitude_deg=_coerce_optional_float(payload.get("latitude_deg"), fallback=current.latitude_deg),
        longitude_deg=_coerce_optional_float(payload.get("longitude_deg"), fallback=current.longitude_deg),
        elevation_m=_coerce_optional_float(payload.get("elevation_m"), fallback=current.elevation_m),
        sqm_mag_arcsec2=_coerce_optional_float(payload.get("sqm_mag_arcsec2"), fallback=current.sqm_mag_arcsec2),
        bortle_class=_coerce_optional_int(payload.get("bortle_class"), fallback=current.bortle_class),
        south_horizon_open=bool(payload.get("south_horizon_open", current.south_horizon_open)),
        notes=_coerce_optional_string(payload.get("notes")) if "notes" in payload else current.notes,
    )


def _mosaic_plan_from_payload(payload: dict[str, Any], *, fallback_profile: Any) -> Any:
    project_slug = str(payload.get("project_slug", "")).strip()
    name = str(payload.get("name", "")).strip()
    if not project_slug or not name:
        raise ValueError("Fields 'project_slug' and 'name' are required.")

    imaging_profile_id = str(payload.get("imaging_profile_id") or getattr(fallback_profile, "profile_id", "")).strip()
    imaging_profile_label = str(payload.get("imaging_profile_label") or getattr(fallback_profile, "label", "")).strip()
    fov_width_deg = _coerce_required_float(payload.get("fov_width_deg"), fallback=getattr(fallback_profile, "fov_width_deg", None))
    fov_height_deg = _coerce_required_float(payload.get("fov_height_deg"), fallback=getattr(fallback_profile, "fov_height_deg", None))
    center_ra_deg = _coerce_required_float(payload.get("center_ra_deg"))
    center_dec_deg = _coerce_required_float(payload.get("center_dec_deg"))
    region_width_deg = _coerce_required_float(payload.get("region_width_deg"))
    region_height_deg = _coerce_required_float(payload.get("region_height_deg"))
    rotation_deg = _coerce_required_float(payload.get("rotation_deg"), fallback=getattr(fallback_profile, "rotation_deg", 0.0) or 0.0)
    overlap_percent = _coerce_required_float(payload.get("overlap_percent"), fallback=10.0)
    status = str(payload.get("status", "draft")).strip() or "draft"
    target_name = _coerce_optional_string(payload.get("target_name"))
    observation_type = _coerce_optional_string(payload.get("observation_type"))
    filter_name = _coerce_optional_string(payload.get("filter"))
    selected_panel_id = _coerce_optional_string(payload.get("selected_panel_id"))
    provided_id = _coerce_optional_string(payload.get("id"))

    if not imaging_profile_id or not imaging_profile_label:
        raise ValueError("Imaging profile information is required for mosaic plans.")

    return MosaicPlan(
        id=provided_id or f"mosaic:{uuid.uuid4().hex[:12]}",
        project_slug=project_slug,
        name=name,
        target_name=target_name,
        observation_type=observation_type,
        filter=filter_name,
        imaging_profile_id=imaging_profile_id,
        imaging_profile_label=imaging_profile_label,
        fov_width_deg=fov_width_deg,
        fov_height_deg=fov_height_deg,
        center_ra_deg=center_ra_deg,
        center_dec_deg=center_dec_deg,
        region_width_deg=region_width_deg,
        region_height_deg=region_height_deg,
        rotation_deg=rotation_deg,
        overlap_percent=overlap_percent,
        status=status,
        selected_panel_id=selected_panel_id,
    )


def _merge_mosaic_plan_payload(current: Any, payload: dict[str, Any]) -> Any:
    return MosaicPlan(
        id=current.id,
        project_slug=str(payload.get("project_slug", current.project_slug)).strip() or current.project_slug,
        name=str(payload.get("name", current.name)).strip() or current.name,
        target_name=_coerce_optional_string(payload.get("target_name")) if "target_name" in payload else current.target_name,
        observation_type=_coerce_optional_string(payload.get("observation_type")) if "observation_type" in payload else current.observation_type,
        filter=_coerce_optional_string(payload.get("filter")) if "filter" in payload else current.filter,
        imaging_profile_id=str(payload.get("imaging_profile_id", current.imaging_profile_id)).strip() or current.imaging_profile_id,
        imaging_profile_label=str(payload.get("imaging_profile_label", current.imaging_profile_label)).strip() or current.imaging_profile_label,
        fov_width_deg=_coerce_required_float(payload.get("fov_width_deg"), fallback=current.fov_width_deg),
        fov_height_deg=_coerce_required_float(payload.get("fov_height_deg"), fallback=current.fov_height_deg),
        center_ra_deg=_coerce_required_float(payload.get("center_ra_deg"), fallback=current.center_ra_deg),
        center_dec_deg=_coerce_required_float(payload.get("center_dec_deg"), fallback=current.center_dec_deg),
        region_width_deg=_coerce_required_float(payload.get("region_width_deg"), fallback=current.region_width_deg),
        region_height_deg=_coerce_required_float(payload.get("region_height_deg"), fallback=current.region_height_deg),
        rotation_deg=_coerce_required_float(payload.get("rotation_deg"), fallback=current.rotation_deg),
        overlap_percent=_coerce_required_float(payload.get("overlap_percent"), fallback=current.overlap_percent),
        status=str(payload.get("status", current.status)).strip() or current.status,
        selected_panel_id=_coerce_optional_string(payload.get("selected_panel_id")) if "selected_panel_id" in payload else current.selected_panel_id,
        panels=current.panels,
    )


def _merge_mosaic_panel_payload(current: Any, payload: dict[str, Any]) -> Any:
    return MosaicPanel(
        id=current.id,
        mosaic_plan_id=current.mosaic_plan_id,
        panel_index=current.panel_index,
        panel_label=str(payload.get("panel_label", current.panel_label)).strip() or current.panel_label,
        center_ra_deg=_coerce_required_float(payload.get("center_ra_deg"), fallback=current.center_ra_deg),
        center_dec_deg=_coerce_required_float(payload.get("center_dec_deg"), fallback=current.center_dec_deg),
        fov_width_deg=_coerce_required_float(payload.get("fov_width_deg"), fallback=current.fov_width_deg),
        fov_height_deg=_coerce_required_float(payload.get("fov_height_deg"), fallback=current.fov_height_deg),
        rotation_deg=_coerce_required_float(payload.get("rotation_deg"), fallback=current.rotation_deg),
        row_index=_coerce_optional_int(payload.get("row_index"), fallback=current.row_index),
        column_index=_coerce_optional_int(payload.get("column_index"), fallback=current.column_index),
        status=str(payload.get("status", current.status)).strip() or current.status,
        target_integration_seconds=_coerce_optional_float(payload.get("target_integration_seconds"), fallback=current.target_integration_seconds),
        acquired_integration_seconds=_coerce_optional_float(payload.get("acquired_integration_seconds"), fallback=current.acquired_integration_seconds),
    )


def _coerce_required_float(value: Any, *, fallback: Any = None) -> float:
    candidate = fallback if value is None or value == "" else value
    if candidate is None or candidate == "":
        raise ValueError("Missing required numeric value.")
    return float(candidate)


def _coerce_optional_float(value: Any, *, fallback: float | None = None) -> float | None:
    if value is None or value == "":
        return fallback
    return float(value)


def _coerce_optional_int(value: Any, *, fallback: int | None = None) -> int | None:
    if value is None or value == "":
        return fallback
    return int(value)


def _ensure_capture_thumbnail(
    storage: ProjectStorage,
    *,
    project_slug: str,
    capture_name: str,
    source_path: Path,
    size: int,
) -> Path:
    """Generate or reuse a cached JPEG thumbnail for a previewable capture file."""
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
    parser.add_argument("--database-path", default=None)
    args = parser.parse_args(argv)

    run_server(host=args.host, port=args.port, projects_root=args.projects_root, database_path=args.database_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
