from __future__ import annotations

"""ARCH-4.1: reading must never create filesystem state.

A ProjectStorage READ may not create a project directory, captures/, runs/, project.json or
anything else. Only explicit WRITE operations create structure.
"""

import hashlib
import inspect
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from tsn_dss.engine.project_processing import ProjectRunManager
from tsn_dss.engine.project_registry import ProjectRegistry
from tsn_dss.engine.project_registry import validate_dir_key as registry_validate_dir_key
from tsn_dss.engine.projects import ProjectStorage, validate_dir_key
from tsn_dss.engine.sqlite.db import connect_database
from tsn_dss.engine.sqlite.project_repository import ProjectRepository
from tsn_dss.gui.http_api import create_http_server

try:  # thumbnails need Pillow, exactly like the server
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

# Every public ProjectStorage method must be listed here. Adding a method forces a decision.
READ_METHODS = {"list_projects", "get_project", "describe_capture", "resolve_capture_file",
                "locate_project", "project_layout"}
WRITE_METHODS = {"ensure_project", "create_project", "delete_project", "set_project_sky_target",
                 "import_capture", "prepare_siril_run"}


def snapshot(root: Path) -> dict[str, tuple]:
    """Entry-for-entry and byte-for-byte picture of a directory tree."""
    entries: dict[str, tuple] = {}
    for path in [root, *sorted(root.rglob("*"))]:
        relative = str(path.relative_to(root))
        if path.is_dir():
            entries[relative] = ("dir",)
        else:
            entries[relative] = ("file", path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())
    return entries


def make_raw_capture(root: Path) -> None:
    for folder in ("biases", "darks", "flats", "lights"):
        (root / folder).mkdir(parents=True)
        (root / folder / f"{folder}_001.CR2").write_bytes(b"raw")


class StorageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.base = Path(self.temp_dir.name)
        self.root = self.base / "projects"
        self.root.mkdir()
        self.storage = ProjectStorage(self.root)

    def make_mixed_tree(self) -> None:
        """An incomplete project, a captures-only project and a complete project."""
        (self.root / "Incomplete").mkdir()
        (self.root / "Incomplete" / "project.json").write_text('{"sky_target": "M31"}', encoding="utf-8")
        (self.root / "CapturesOnly" / "captures" / "Night1").mkdir(parents=True)
        (self.root / "Complete" / "runs").mkdir(parents=True)
        make_raw_capture(self.root / "Complete" / "captures" / "OrionNebula")


class StorageMethodClassificationTests(StorageTestCase):
    def test_every_public_method_is_classified_as_read_or_write(self) -> None:
        public = {name for name, _ in inspect.getmembers(ProjectStorage, inspect.isfunction) if not name.startswith("_")}
        self.assertEqual(
            public,
            READ_METHODS | WRITE_METHODS,
            "Classify every new public ProjectStorage method as READ or WRITE, and give READ methods a "
            "behavioural case in StorageReadTests.read_cases.",
        )
        self.assertFalse(READ_METHODS & WRITE_METHODS)


class StorageReadTests(StorageTestCase):
    def read_cases(self, project: str, capture: str = "Cap") -> dict:
        """One call per READ method, aimed at the given project."""
        return {
            "list_projects": lambda: self.storage.list_projects(),
            "get_project": lambda: self.storage.get_project(project),
            "describe_capture": lambda: self.storage.describe_capture(project, capture),
            "resolve_capture_file": lambda: self.storage.resolve_capture_file(project, capture, "lights/a.CR2"),
            "locate_project": lambda: self.storage.locate_project(project),
            "project_layout": lambda: self.storage.project_layout(project),
        }

    def run_all_reads(self, project: str, capture: str = "Cap") -> dict[str, object]:
        results: dict[str, object] = {}
        for name, call in self.read_cases(project, capture).items():
            try:
                results[name] = call()
            except (FileNotFoundError, ValueError) as error:
                results[name] = error
        return results

    def test_every_read_method_has_a_behavioural_case(self) -> None:
        self.assertEqual(set(self.read_cases("x")), READ_METHODS)

    def test_reads_of_an_unknown_project_create_nothing_and_report_not_found(self) -> None:
        before = snapshot(self.base)
        results = self.run_all_reads("Ghost")

        self.assertEqual(snapshot(self.base), before)
        self.assertFalse((self.root / "Ghost").exists())
        self.assertEqual(results["list_projects"], [])
        self.assertIsNone(results["get_project"])
        for name in ("describe_capture", "resolve_capture_file", "locate_project"):
            self.assertIsInstance(results[name], FileNotFoundError, name)

    def test_reads_of_an_existing_incomplete_project_do_not_repair_it(self) -> None:
        self.make_mixed_tree()
        before = snapshot(self.base)

        results = self.run_all_reads("Incomplete")

        self.assertEqual(snapshot(self.base), before)
        self.assertFalse((self.root / "Incomplete" / "captures").exists())
        self.assertFalse((self.root / "Incomplete" / "runs").exists())
        self.assertFalse((self.root / "CapturesOnly" / "runs").exists())
        self.assertFalse((self.root / "Complete" / "captures" / "OrionNebula" / "extra").exists())

        summary = results["get_project"]
        self.assertEqual((summary.slug, summary.capture_names, summary.run_names, summary.sky_target),
                         ("Incomplete", (), (), "M31"))
        listing = {project.slug: project for project in results["list_projects"]}
        self.assertEqual(set(listing), {"Incomplete", "CapturesOnly", "Complete"})
        self.assertEqual(listing["CapturesOnly"].capture_names, ("Night1",))
        self.assertEqual(listing["CapturesOnly"].run_names, ())
        self.assertEqual(listing["Complete"].capture_names, ("OrionNebula",))
        self.assertEqual(results["locate_project"].project_root, self.root / "Incomplete")
        self.assertFalse(results["locate_project"].captures_dir.exists())  # path only, nothing created
        self.assertIsInstance(results["describe_capture"], FileNotFoundError)
        self.assertIsInstance(results["resolve_capture_file"], FileNotFoundError)

    def test_reads_of_an_existing_capture_are_truthful_and_side_effect_free(self) -> None:
        self.make_mixed_tree()
        before = snapshot(self.base)
        details = self.storage.describe_capture("Complete", "OrionNebula")
        capture_root, file_path = self.storage.resolve_capture_file("Complete", "OrionNebula", "lights/lights_001.CR2")
        self.assertEqual({folder.name: folder.file_count for folder in details.folders},
                         {"biases": 1, "darks": 1, "flats": 1, "lights": 1})
        self.assertTrue(file_path.is_file())
        self.assertEqual(snapshot(self.base), before)

    def test_repeated_reads_leave_the_tree_unchanged(self) -> None:
        self.make_mixed_tree()
        before = snapshot(self.base)
        for _ in range(4):
            for project in ("Ghost", "Incomplete", "CapturesOnly", "Complete"):
                self.run_all_reads(project, "Night1" if project == "CapturesOnly" else "OrionNebula")
                self.assertEqual(snapshot(self.base), before, project)

    def test_reads_with_invalid_names_create_nothing_anywhere(self) -> None:
        self.make_mixed_tree()
        before = snapshot(self.base)
        for name in ("", "  ", ".", "..", "../escape", "a/b", "a\\b", "/abs", "C:\\temp", "C:", "nul\x00byte"):
            with self.subTest(name):
                results = self.run_all_reads(name)
                self.assertIsInstance(results["locate_project"], FileNotFoundError)
                self.assertIsInstance(results["describe_capture"], FileNotFoundError)
                self.assertIsInstance(results["resolve_capture_file"], FileNotFoundError)
                self.assertIsInstance(results["project_layout"], ValueError)
                self.assertIsNone(results["get_project"])
        self.assertEqual(snapshot(self.base), before)

    def test_locating_and_layout_are_pure(self) -> None:
        layout = self.storage.project_layout("Anything")
        self.assertEqual(layout.captures_dir, self.root / "Anything" / "captures")
        self.assertFalse(layout.project_root.exists())
        self.assertEqual(list(self.root.iterdir()), [])


class StorageWriteTests(StorageTestCase):
    """Explicit writes still create exactly the structure they always did."""

    def test_ensure_project_creates_the_structure(self) -> None:
        layout = self.storage.ensure_project("New Project")
        self.assertTrue(layout.captures_dir.is_dir())
        self.assertTrue(layout.runs_dir.is_dir())

    def test_ensure_project_is_the_explicit_repair_for_an_incomplete_project(self) -> None:
        (self.root / "Incomplete").mkdir()
        self.storage.ensure_project("Incomplete")
        self.assertTrue((self.root / "Incomplete" / "captures").is_dir())
        self.assertTrue((self.root / "Incomplete" / "runs").is_dir())

    def test_create_project_creates_the_structure_and_lists_it(self) -> None:
        summary = self.storage.create_project("M42")
        self.assertEqual((summary.slug, summary.capture_names, summary.run_names), ("M42", (), ()))
        self.assertTrue((self.root / "M42" / "captures").is_dir())
        self.assertTrue((self.root / "M42" / "runs").is_dir())

    def test_import_capture_creates_the_project_and_the_capture(self) -> None:
        source = self.base / "source"
        make_raw_capture(source)
        destination = self.storage.import_capture("Fresh", "Night1", source)
        self.assertEqual(destination, self.root / "Fresh" / "captures" / "Night1")
        self.assertTrue((destination / "lights" / "lights_001.CR2").is_file())
        self.assertTrue((self.root / "Fresh" / "runs").is_dir())

    def test_invalid_import_source_creates_no_project(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.storage.import_capture("Fresh", "Night1", self.base / "missing")
        self.assertFalse((self.root / "Fresh").exists())

    def test_set_project_sky_target_creates_a_missing_project_like_before(self) -> None:
        summary = self.storage.set_project_sky_target("Brand New", "M13")
        self.assertEqual(summary.sky_target, "M13")
        self.assertTrue((self.root / "Brand New" / "captures").is_dir())
        self.assertEqual(json.loads((self.root / "Brand New" / "project.json").read_text(encoding="utf-8")),
                         {"sky_target": "M13"})

    def test_prepare_siril_run_creates_the_run_workspace(self) -> None:
        sources = []
        for frame_type, folder in (("bias", "biases"), ("dark", "darks"), ("flat", "flats"), ("light", "lights")):
            path = self.base / f"{folder}_001.CR2"
            path.write_bytes(b"raw")
            sources.append((frame_type, path))
        layout = self.storage.prepare_siril_run("M42", "run-1", frame_sources=sources)
        self.assertTrue(layout.workspace_dir.is_dir())
        self.assertTrue(layout.artifacts_dir.is_dir())
        self.assertTrue(layout.logs_dir.is_dir())
        self.assertTrue((self.root / "M42" / "captures").is_dir())

    def test_delete_project_removes_the_directory(self) -> None:
        self.storage.create_project("Doomed")
        self.storage.delete_project("Doomed")
        self.assertFalse((self.root / "Doomed").exists())

    def test_writes_keep_the_path_safety_of_the_canonical_name_rules(self) -> None:
        before = snapshot(self.base)
        for name in ("", "..", "../escape", "a/b", "a\\b", "/abs", "C:\\temp", "C:"):
            with self.subTest(name):
                for write in (self.storage.ensure_project, self.storage.create_project):
                    with self.assertRaises(ValueError):
                        write(name)
                with self.assertRaises(ValueError):
                    self.storage.set_project_sky_target(name, "x")
        self.assertEqual(snapshot(self.base), before)

    def test_the_validator_is_shared_and_still_exported_by_the_registry(self) -> None:
        self.assertIs(validate_dir_key, registry_validate_dir_key)
        self.assertEqual(validate_dir_key("M27 \u2014 Dumbbell Nebula"), "M27 \u2014 Dumbbell Nebula")


class RunManagerReadSideTests(StorageTestCase):
    def test_starting_a_run_for_an_unknown_project_fails_without_creating_it(self) -> None:
        manager = ProjectRunManager(project_storage=self.storage)
        before = snapshot(self.base)
        with self.assertRaises(FileNotFoundError):
            manager.start_osc_preprocessing(project_slug="Ghost", capture_name="Cap")
        self.assertEqual(snapshot(self.base), before)
        self.assertFalse((self.root / "Ghost").exists())

    def test_starting_a_run_for_an_incomplete_project_does_not_repair_it(self) -> None:
        (self.root / "Incomplete").mkdir()
        manager = ProjectRunManager(project_storage=self.storage)
        before = snapshot(self.base)
        with self.assertRaises(FileNotFoundError):
            manager.start_osc_preprocessing(project_slug="Incomplete", capture_name="Cap")
        self.assertEqual(snapshot(self.base), before)

    def test_starting_a_run_with_an_invalid_project_name_creates_nothing(self) -> None:
        manager = ProjectRunManager(project_storage=self.storage)
        before = snapshot(self.base)
        for name in ("..", "../escape", "a/b", "C:"):
            with self.subTest(name), self.assertRaises(ValueError):
                manager.start_osc_preprocessing(project_slug=name, capture_name="Cap")
        self.assertEqual(snapshot(self.base), before)


@unittest.skipIf(Image is None, "Pillow is required for thumbnails")
class ThumbnailHelperTests(StorageTestCase):
    """The thumbnail helper is a read helper: it may write its derived cache, never project structure."""

    def helper(self):
        from tsn_dss.gui.http_api import _ensure_capture_thumbnail

        return _ensure_capture_thumbnail

    def test_unknown_project_raises_and_creates_nothing(self) -> None:
        source = self.base / "elsewhere.png"
        Image.new("RGB", (8, 8)).save(source)
        before = snapshot(self.base)
        with self.assertRaises(FileNotFoundError):
            self.helper()(self.storage, project_slug="Ghost", capture_name="Cap",
                          source_path=source.resolve(), size=64)
        self.assertEqual(snapshot(self.base), before)
        self.assertFalse((self.root / "Ghost").exists())

    def test_a_project_without_runs_is_not_given_a_runs_directory(self) -> None:
        capture = self.root / "CapturesOnly" / "captures" / "Night1" / "lights"
        capture.mkdir(parents=True)
        source = capture / "frame.png"
        Image.new("RGB", (8, 8)).save(source)
        before = snapshot(self.base)

        thumbnail = self.helper()(self.storage, project_slug="CapturesOnly", capture_name="Night1",
                                  source_path=source.resolve(), size=64)

        self.assertTrue(thumbnail.is_file())
        created = {entry.replace("\\", "/") for entry in set(snapshot(self.base)) - set(before)}
        self.assertTrue(created)
        self.assertTrue(all(entry.startswith("projects/CapturesOnly/.cache") for entry in created), created)
        self.assertFalse((self.root / "CapturesOnly" / "runs").exists())


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------


class ReadRouteTests(StorageTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_mixed_tree()
        if Image is not None:
            Image.new("RGB", (16, 16), (200, 10, 10)).save(self.root / "Complete" / "captures" / "OrionNebula" / "lights" / "preview.png")
        self.database = self.base / "tsn_dss.db"  # outside the projects root, so the tree stays pure
        self.start_server()

    def start_server(self) -> None:
        self.server = create_http_server(host="127.0.0.1", port=0, projects_root=self.root, database_path=self.database)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.stop_server)

    def stop_server(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)
            self.server = None

    def call(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict | None]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(f"{self.base_url}{path}", data=data, method=method,
                          headers={"Content-Type": "application/json"} if data else {})
        try:
            with urlopen(request) as response:
                body = response.read()
                return response.status, (json.loads(body.decode("utf-8")) if response.headers.get_content_type() == "application/json" else None)
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def unknown_project_reads(self, slug: str) -> list[tuple[str, str]]:
        return [
            (f"/api/projects/{slug}", "project_not_found"),
            (f"/api/projects/{slug}/captures/Cap", "capture_not_found"),
            (f"/api/projects/{slug}/captures/Cap/files/lights/a.CR2", "file_missing"),
            (f"/api/projects/{slug}/captures/Cap/thumbnails/lights/a.png", "file_missing"),
        ]

    def test_unknown_project_reads_are_404_and_create_nothing(self) -> None:
        before = snapshot(self.base)
        for path, error in self.unknown_project_reads("Ghost"):
            status, body = self.call("GET", path)
            self.assertEqual((status, body["error"]), (404, error), path)
        self.assertEqual(snapshot(self.base), before)
        self.assertFalse((self.root / "Ghost").exists())
        self.assertNotIn("Ghost", [project["slug"] for project in self.call("GET", "/api/projects")[1]["projects"]])

    def test_invalid_project_names_in_read_urls_are_404_and_create_nothing(self) -> None:
        before = snapshot(self.base)
        for slug in ("..", "C:", quote("a/b", safe=""), quote("..\\x", safe="")):
            for path, _ in self.unknown_project_reads(slug)[1:]:
                status, _ = self.call("GET", path)
                self.assertEqual(status, 404, path)
        self.assertEqual(snapshot(self.base), before)

    def test_reads_of_an_incomplete_project_do_not_repair_it(self) -> None:
        before = snapshot(self.base)
        status, listing = self.call("GET", "/api/projects")
        by_slug = {project["slug"]: project for project in listing["projects"]}
        self.assertEqual(status, 200)
        self.assertEqual((by_slug["Incomplete"]["capture_count"], by_slug["Incomplete"]["run_count"],
                          by_slug["Incomplete"]["capture_names"], by_slug["Incomplete"]["sky_target"]),
                         (0, 0, [], "M31"))
        self.assertEqual(self.call("GET", "/api/projects/Incomplete")[0], 200)
        self.assertEqual(self.call("GET", "/api/projects/Incomplete/captures/Cap")[0], 404)
        self.assertEqual(self.call("GET", "/api/projects/Incomplete/captures/Cap/files/lights/a.CR2")[0], 404)
        self.assertEqual(self.call("GET", "/api/projects/Incomplete/captures/Cap/thumbnails/lights/a.png")[0], 404)
        self.assertEqual(snapshot(self.base), before)
        self.assertFalse((self.root / "Incomplete" / "captures").exists())
        self.assertFalse((self.root / "Incomplete" / "runs").exists())

    def test_repeated_reads_leave_the_tree_unchanged(self) -> None:
        before = snapshot(self.base)
        for _ in range(3):
            self.call("GET", "/api/projects")
            self.call("GET", "/api/projects/Complete")
            self.call("GET", "/api/projects/Complete/captures/OrionNebula")
            self.call("GET", "/api/projects/Complete/captures/OrionNebula/files/lights/lights_001.CR2")
            for path, _ in self.unknown_project_reads("Ghost") + self.unknown_project_reads("Incomplete"):
                self.call("GET", path)
            self.assertEqual(snapshot(self.base), before)

    @unittest.skipIf(Image is None, "Pillow is required for thumbnails")
    def test_a_real_thumbnail_creates_only_its_derived_cache(self) -> None:
        before = snapshot(self.base)
        path = "/api/projects/Complete/captures/OrionNebula/thumbnails/lights/preview.png?size=64"
        self.assertEqual(self.call("GET", path)[0], 200)
        after = snapshot(self.base)

        created = set(after) - set(before)
        self.assertTrue(created)
        cache_prefix = "projects/Complete/.cache"
        self.assertTrue(
            all(entry.replace("\\", "/") == cache_prefix or entry.replace("\\", "/").startswith(cache_prefix + "/")
                for entry in created),
            created,
        )
        self.assertEqual({key: value for key, value in after.items() if key in before}, before)  # nothing else changed

        self.call("GET", path)  # cached: a second read changes nothing
        self.assertEqual(snapshot(self.base), after)

    def test_registrar_after_unknown_reads_never_finds_a_phantom_project(self) -> None:
        for path, _ in self.unknown_project_reads("Ghost"):
            self.call("GET", path)
        self.call("GET", "/api/projects")

        self.stop_server()
        self.start_server()  # restart: the startup registrar scans the projects root

        report = self.server.project_registration_report
        self.assertEqual(report.created, [])
        self.assertEqual(sorted(report.unchanged), ["CapturesOnly", "Complete", "Incomplete"])
        connection = connect_database(self.database)
        self.addCleanup(connection.close)
        self.assertNotIn("Ghost", ProjectRepository(connection).dir_key_to_id())
        self.assertEqual(ProjectRegistry(self.server_storage(), connection).inspect().filesystem_only, [])
        self.assertFalse((self.root / "Ghost").exists())

    def server_storage(self) -> ProjectStorage:
        return ProjectStorage(self.root)

    # -- writes through the API keep working and keep the S2 dual-write ---------------------------

    def test_creating_a_project_still_creates_the_structure_and_the_canonical_record(self) -> None:
        status, body = self.call("POST", "/api/projects", {"slug": "M42"})
        self.assertEqual(status, 201)
        self.assertTrue((self.root / "M42" / "captures").is_dir())
        self.assertTrue((self.root / "M42" / "runs").is_dir())
        connection = connect_database(self.database)
        self.addCleanup(connection.close)
        self.assertEqual(ProjectRepository(connection).get_project_by_dir_key("M42").id, body["project"]["project_id"])

    def test_importing_a_capture_still_creates_what_it_needs(self) -> None:
        source = self.base / "source"
        make_raw_capture(source)
        status, body = self.call("POST", "/api/import-capture",
                                 {"project_slug": "Fresh", "capture_name": "Night1", "source_dir": str(source)})
        self.assertEqual(status, 201)
        self.assertTrue((self.root / "Fresh" / "captures" / "Night1" / "lights").is_dir())
        self.assertTrue((self.root / "Fresh" / "runs").is_dir())
        self.assertTrue(body["project"]["project_id"])

    def test_updating_the_target_still_writes_the_record_and_the_mirror(self) -> None:
        status, body = self.call("POST", "/api/projects/Incomplete/sky-target", {"sky_target": "NGC 224"})
        self.assertEqual((status, body["project"]["sky_target"]), (200, "NGC 224"))
        self.assertEqual(json.loads((self.root / "Incomplete" / "project.json").read_text(encoding="utf-8")),
                         {"sky_target": "NGC 224"})
        # the explicit write path is allowed to (re)create the standard structure, as before
        self.assertTrue((self.root / "Incomplete" / "captures").is_dir())

    def test_deleting_a_project_still_works(self) -> None:
        self.call("POST", "/api/projects", {"slug": "Doomed"})
        status, body = self.call("DELETE", "/api/projects/Doomed")
        self.assertEqual((status, body["deleted"]), (200, True))
        self.assertFalse((self.root / "Doomed").exists())

    def test_starting_a_run_for_an_unknown_project_creates_nothing(self) -> None:
        before = snapshot(self.base)
        status, body = self.call("POST", "/api/project-runs", {"project_slug": "Ghost", "capture_name": "Cap"})
        self.assertEqual((status, body["error"]), (400, "run_start_failed"))
        self.assertEqual(snapshot(self.base), before)
        self.assertFalse((self.root / "Ghost").exists())


if __name__ == "__main__":
    unittest.main()
