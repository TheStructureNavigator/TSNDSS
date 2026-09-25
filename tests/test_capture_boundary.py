from __future__ import annotations

"""ARCH-4.2: a capture name identifies exactly one direct child directory of <project>/captures/.

Nothing that serves or processes a file may resolve outside the selected capture. Directory
redirects (symlinks, or NTFS junctions where symlinks need a privilege) are used to prove it.
"""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch
from urllib.parse import quote
from urllib.request import Request, urlopen

try:
    from test_project_storage_reads import StorageTestCase, make_raw_capture, snapshot
except ModuleNotFoundError:
    from tests.test_project_storage_reads import StorageTestCase, make_raw_capture, snapshot
from tsn_dss.engine.project_processing import ProjectRunManager
from tsn_dss.engine.projects import ProjectStorage, path_is_within, validate_capture_name, validate_dir_key
from tsn_dss.gui.http_api import _ensure_capture_thumbnail, create_http_server

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

VALID_NAMES = ["OrionNebula", "Night 1", "M31 \u2014 Andromeda", "Ni\u0119 \u00e9 \u017c\u00f3\u0142w 2026-09-01", "a.b", "-x", "\u5929\u6587"]
INVALID_NAMES = [
    "", "   ", ".", "..", "../x", "x/../y", "a/b", "a\\b", "x/", "./x",
    "/abs/posix/path", "C:\\Windows", "C:", "C:x", "\\rooted", "nul\x00byte",
]


def make_dir_redirect(link: Path, target: Path) -> bool:
    """A directory redirect: a symlink where allowed, else an NTFS junction (no privilege needed)."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
        return result.returncode == 0
    return False


def make_file_symlink(link: Path, target: Path) -> bool:
    try:
        os.symlink(target, link)
        return True
    except (OSError, NotImplementedError):
        return False


def _redirects_supported() -> bool:
    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)
        (base / "t").mkdir()
        supported = make_dir_redirect(base / "l", base / "t")
        if supported:
            os.rmdir(base / "l")
        return supported


CAN_REDIRECT = _redirects_supported()
needs_redirects = unittest.skipUnless(CAN_REDIRECT, "directory symlinks/junctions are unavailable on this platform")


class BoundaryTestCase(StorageTestCase):
    """A project with a valid capture, a sibling capture, a project-level decoy and an outside secret."""

    def setUp(self) -> None:
        super().setUp()
        self.layout = self.storage.ensure_project("P")
        self.captures = self.layout.captures_dir
        self.capture = self.captures / "Night 1"
        make_raw_capture(self.capture)
        (self.capture / "lights" / "nested").mkdir()
        (self.capture / "lights" / "nested" / "deep.CR2").write_bytes(b"deep")
        make_raw_capture(self.captures / "Other")
        (self.captures / "Other" / "lights" / "other.CR2").write_bytes(b"other")
        for folder in ("biases", "darks", "flats", "lights"):  # a project-level decoy ".." would expose
            (self.layout.project_root / folder).mkdir(exist_ok=True)
        (self.layout.project_root / "lights" / "PROJECT_LEVEL.CR2").write_bytes(b"project")
        self.secret = self.base / "secret"
        make_raw_capture(self.secret)
        (self.secret / "lights" / "stolen.fit").write_bytes(b"SECRET")

    def redirect(self, link: Path, target: Path) -> None:
        self.assertTrue(make_dir_redirect(link, target))
        self.addCleanup(self._remove_link, link)

    @staticmethod
    def _remove_link(link: Path) -> None:
        try:
            os.rmdir(link)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# The rule itself
# ---------------------------------------------------------------------------


class CaptureNameRuleTests(unittest.TestCase):
    def test_valid_names_including_unicode_and_spaces_are_returned_unchanged(self) -> None:
        for name in VALID_NAMES:
            with self.subTest(name):
                self.assertEqual(validate_capture_name(name), name)

    def test_invalid_names_are_rejected(self) -> None:
        for name in INVALID_NAMES + [None, 5]:
            with self.subTest(repr(name)), self.assertRaises(ValueError):
                validate_capture_name(name)

    def test_project_names_share_the_same_rule(self) -> None:
        for name in INVALID_NAMES:
            with self.subTest(name), self.assertRaises(ValueError):
                validate_dir_key(name)
        for name in VALID_NAMES:
            self.assertEqual(validate_dir_key(name), name)

    def test_path_is_within(self) -> None:
        root = Path("/a/b")
        self.assertTrue(path_is_within(Path("/a/b"), root))
        self.assertTrue(path_is_within(Path("/a/b/c/d"), root))
        self.assertFalse(path_is_within(Path("/a/bc"), root))  # a string-prefix check would accept this
        self.assertFalse(path_is_within(Path("/a"), root))


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


class CaptureReadBoundaryTests(BoundaryTestCase):
    def test_valid_unicode_and_space_names_work_for_every_read(self) -> None:
        for name in VALID_NAMES:
            with self.subTest(name):
                if not (self.captures / name).exists():  # "Night 1" is created by the fixture
                    make_raw_capture(self.captures / name)
                root = self.storage.resolve_capture_root("P", name)
                self.assertEqual(root.name, name)
                details = self.storage.describe_capture("P", name)
                self.assertEqual(details.capture_name, name)
                self.assertEqual({folder.name: folder.file_count for folder in details.folders},
                                 {"biases": 1, "darks": 1, "flats": 1, "lights": 1})
                _, file_path = self.storage.resolve_capture_file("P", name, "lights/lights_001.CR2")
                self.assertEqual(file_path.read_bytes(), b"raw")

    def test_invalid_names_are_not_found_and_touch_nothing(self) -> None:
        before = snapshot(self.base)
        for name in INVALID_NAMES:
            with self.subTest(repr(name)):
                for read in (
                    lambda: self.storage.describe_capture("P", name),
                    lambda: self.storage.resolve_capture_root("P", name),
                    lambda: self.storage.resolve_capture_file("P", name, "lights/lights_001.CR2"),
                ):
                    with self.assertRaises(FileNotFoundError):
                        read()
        self.assertEqual(snapshot(self.base), before)

    def test_dotdot_can_no_longer_expose_the_project_directory(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.storage.describe_capture("P", "..")
        with self.assertRaises(FileNotFoundError):
            self.storage.resolve_capture_file("P", "..", "lights/PROJECT_LEVEL.CR2")

    def test_a_nested_or_unknown_capture_is_not_found(self) -> None:
        for name in ("Night 1/lights", "Missing", "Night 1/../Other"):
            with self.subTest(name), self.assertRaises(FileNotFoundError):
                self.storage.describe_capture("P", name)

    # -- paths beneath a capture --------------------------------------------------------------

    def test_a_nested_file_inside_the_capture_is_served(self) -> None:
        capture_root, file_path = self.storage.resolve_capture_file("P", "Night 1", "lights/nested/deep.CR2")
        self.assertEqual(file_path.read_bytes(), b"deep")
        self.assertTrue(path_is_within(file_path, capture_root))

    def test_a_path_that_wanders_but_stays_inside_the_capture_is_allowed(self) -> None:
        _, file_path = self.storage.resolve_capture_file("P", "Night 1", "lights/../biases/biases_001.CR2")
        self.assertEqual(file_path.name, "biases_001.CR2")

    def test_file_paths_that_leave_the_capture_are_rejected(self) -> None:
        secret_file = self.secret / "lights" / "stolen.fit"
        before = snapshot(self.base)
        escaping = [
            "../../secret/lights/stolen.fit",
            "../../../secret/lights/stolen.fit",
            "lights/../../Other/lights/other.CR2",       # a sibling capture
            "../../lights/PROJECT_LEVEL.CR2",             # the project directory
            str(secret_file),                             # absolute
            "/etc/passwd",                                # absolute POSIX
            "C:\\Windows\\win.ini",                       # drive-qualified
            "\\rooted\\path.fit",                         # rooted
            "nul\x00byte",
        ]
        for relative in escaping:
            with self.subTest(repr(relative)), self.assertRaises(ValueError):
                self.storage.resolve_capture_file("P", "Night 1", relative)
        self.assertEqual(snapshot(self.base), before)

    def test_a_file_that_resolves_outside_the_capture_is_hidden_from_the_listing(self) -> None:
        # Simulates a file symlink (which some platforms cannot create): only "lights_001.CR2"
        # resolves somewhere else. It must not be listed, whatever the mechanism behind it.
        outside = (self.secret / "lights" / "stolen.fit").resolve()
        original = Path.resolve

        def resolve(path: Path, *args, **kwargs):
            return outside if path.name == "lights_001.CR2" else original(path, *args, **kwargs)

        with patch.object(Path, "resolve", resolve):
            details = self.storage.describe_capture("P", "Night 1")
        lights = next(folder for folder in details.folders if folder.name == "lights")
        self.assertNotIn("lights_001.CR2", [entry.name for entry in lights.files])
        self.assertIn("nested", [child.name for child in (self.capture / "lights").iterdir()])  # others unaffected
        self.assertEqual(sum(folder.file_count for folder in details.folders), 3)  # biases, darks, flats remain

    def test_directory_or_missing_paths_are_not_found(self) -> None:
        for relative in ("", ".", "lights", "lights/missing.CR2"):
            with self.subTest(relative), self.assertRaises(FileNotFoundError):
                self.storage.resolve_capture_file("P", "Night 1", relative)


# ---------------------------------------------------------------------------
# Links and junctions
# ---------------------------------------------------------------------------


@needs_redirects
class CaptureRedirectTests(BoundaryTestCase):
    def test_a_capture_that_redirects_outside_captures_is_not_a_capture(self) -> None:
        self.redirect(self.captures / "Escaped", self.secret)
        for read in (
            lambda: self.storage.describe_capture("P", "Escaped"),
            lambda: self.storage.resolve_capture_root("P", "Escaped"),
            lambda: self.storage.resolve_capture_file("P", "Escaped", "lights/stolen.fit"),
        ):
            with self.assertRaises(FileNotFoundError):
                read()

    def test_a_capture_that_redirects_to_the_project_directory_is_refused(self) -> None:
        self.redirect(self.captures / "ProjectAlias", self.layout.project_root)
        with self.assertRaises(FileNotFoundError):
            self.storage.describe_capture("P", "ProjectAlias")

    def test_a_capture_that_redirects_to_a_sibling_capture_stays_inside_captures(self) -> None:
        # Documented: the rule is "a direct child of captures/", so an alias of another capture
        # (which still resolves inside captures/) is accepted and serves that capture's files.
        self.redirect(self.captures / "Alias", self.captures / "Other")
        root = self.storage.resolve_capture_root("P", "Alias")
        self.assertEqual(root, (self.captures / "Other").resolve())

    def test_a_subfolder_that_redirects_outside_is_hidden_and_never_served(self) -> None:
        shutil.rmtree(self.capture / "flats")
        self.redirect(self.capture / "flats", self.secret / "lights")

        details = self.storage.describe_capture("P", "Night 1")
        flats = next(folder for folder in details.folders if folder.name == "flats")
        self.assertEqual((flats.file_count, flats.files), (0, ()))
        listed = [entry.name for folder in details.folders for entry in folder.files]
        self.assertNotIn("stolen.fit", listed)
        with self.assertRaises(ValueError):
            self.storage.resolve_capture_file("P", "Night 1", "flats/stolen.fit")

    def test_describing_a_capture_never_lists_a_directory_outside_it(self) -> None:
        shutil.rmtree(self.capture / "flats")
        self.redirect(self.capture / "flats", self.secret / "lights")
        listed: list[Path] = []
        original = Path.iterdir

        def spy(path: Path):
            listed.append(path.resolve())
            return original(path)

        capture_root = self.capture.resolve()
        with patch.object(Path, "iterdir", spy):
            self.storage.describe_capture("P", "Night 1")

        self.assertTrue(listed)
        self.assertTrue(all(path_is_within(path, capture_root) for path in listed), listed)

    def test_a_deeper_redirect_is_also_contained(self) -> None:
        self.redirect(self.capture / "lights" / "outside", self.secret / "lights")
        with self.assertRaises(ValueError):
            self.storage.resolve_capture_file("P", "Night 1", "lights/outside/stolen.fit")

    def test_a_file_symlink_that_leaves_the_capture_is_hidden_and_never_served(self) -> None:
        link = self.capture / "lights" / "linked.fit"
        if not make_file_symlink(link, self.secret / "lights" / "stolen.fit"):
            self.skipTest("file symlinks are unavailable on this platform")
        details = self.storage.describe_capture("P", "Night 1")
        self.assertNotIn("linked.fit", [entry.name for folder in details.folders for entry in folder.files])
        with self.assertRaises(ValueError):
            self.storage.resolve_capture_file("P", "Night 1", "lights/linked.fit")

    def test_reads_with_redirects_create_nothing(self) -> None:
        self.redirect(self.captures / "Escaped", self.secret)
        before = snapshot(self.captures)
        for name in ("Escaped", "Night 1"):
            for read in (lambda: self.storage.describe_capture("P", name),
                         lambda: self.storage.resolve_capture_file("P", name, "lights/stolen.fit")):
                try:
                    read()
                except (FileNotFoundError, ValueError):
                    pass
        self.assertEqual(snapshot(self.captures), before)


# ---------------------------------------------------------------------------
# Writes: import
# ---------------------------------------------------------------------------


class CaptureImportBoundaryTests(BoundaryTestCase):
    def source(self, name: str = "src") -> Path:
        path = self.base / name
        make_raw_capture(path)
        return path

    def test_invalid_names_fail_before_anything_is_created_or_moved(self) -> None:
        source = self.source()
        for name in INVALID_NAMES:
            for move in (False, True):
                before = snapshot(self.base)
                with self.subTest(repr(name), move=move), self.assertRaises(ValueError):
                    self.storage.import_capture("P", name, source, move=move)
                self.assertEqual(snapshot(self.base), before)  # source untouched, nothing copied

    def test_an_invalid_name_does_not_create_a_new_project_either(self) -> None:
        before = snapshot(self.base)
        with self.assertRaises(ValueError):
            self.storage.import_capture("Brand New", "..", self.source())
        self.assertFalse((self.root / "Brand New").exists())
        self.assertEqual({k: v for k, v in snapshot(self.base).items() if not k.startswith("src")},
                         {k: v for k, v in before.items() if not k.startswith("src")})

    def test_valid_unicode_names_import_to_exactly_that_direct_child(self) -> None:
        for index, name in enumerate(n for n in VALID_NAMES if n != "Night 1"):  # "Night 1" already exists
            with self.subTest(name):
                destination = self.storage.import_capture("P", name, self.source(f"src-{index}"))
                self.assertEqual(destination, self.captures / name)
                self.assertTrue((destination / "lights" / "lights_001.CR2").is_file())
                self.assertEqual(destination.resolve().parent, self.captures.resolve())

    def test_an_existing_capture_is_never_overwritten(self) -> None:
        before = snapshot(self.captures)
        with self.assertRaises(FileExistsError):
            self.storage.import_capture("P", "Night 1", self.source())
        self.assertEqual(snapshot(self.captures), before)

    @needs_redirects
    def test_a_dangling_redirect_at_the_destination_counts_as_existing(self) -> None:
        missing = self.base / "does-not-exist-yet"
        missing.mkdir()
        self.redirect(self.captures / "Dangling", missing)
        os.rmdir(missing)  # now the redirect points at nothing
        for move in (False, True):
            source = self.source(f"dangling-src-{move}")
            with self.subTest(move=move), self.assertRaisesRegex(FileExistsError, "Capture already exists"):
                self.storage.import_capture("P", "Dangling", source, move=move)
            self.assertTrue((source / "lights" / "lights_001.CR2").exists())  # nothing was moved or copied

    @needs_redirects
    def test_a_copy_import_materialises_linked_content_so_the_destination_has_no_links(self) -> None:
        source = self.source()
        self.redirect(source / "lights" / "linked_dir", self.secret / "lights")

        destination = self.storage.import_capture("P", "Copied", source, move=False)

        inner = destination / "lights" / "linked_dir"
        self.assertTrue(inner.is_dir())
        self.assertFalse(inner.is_symlink() or getattr(os.path, "isjunction", lambda p: False)(inner))
        self.assertEqual((inner / "stolen.fit").read_bytes(), b"SECRET")
        self.assertTrue(path_is_within(inner.resolve(), destination.resolve()))

    @needs_redirects
    def test_a_move_import_keeps_links_but_the_boundary_still_holds_when_reading(self) -> None:
        source = self.source()
        self.redirect(source / "lights" / "linked_dir", self.secret / "lights")

        destination = self.storage.import_capture("P", "Moved", source, move=True)

        inner = destination / "lights" / "linked_dir"
        is_link = inner.is_symlink() or getattr(os.path, "isjunction", lambda p: False)(inner)
        if not is_link:
            self.skipTest("this platform copied the tree instead of renaming it")
        self.assertFalse(path_is_within(inner.resolve(), destination.resolve()))  # the link survives the move
        with self.assertRaises(ValueError):
            self.storage.resolve_capture_file("P", "Moved", "lights/linked_dir/stolen.fit")  # ...but is never served


# ---------------------------------------------------------------------------
# Writes: run preparation and the run manager
# ---------------------------------------------------------------------------


class RunBoundaryTests(BoundaryTestCase):
    def test_the_run_manager_rejects_invalid_capture_names_before_touching_anything(self) -> None:
        manager = ProjectRunManager(project_storage=self.storage)
        before = snapshot(self.base)
        for name in INVALID_NAMES:
            with self.subTest(repr(name)), self.assertRaises(ValueError):
                manager.start_osc_preprocessing(project_slug="P", capture_name=name)
            self.assertEqual(snapshot(self.base), before)
        self.assertEqual(manager.list_runs(), [])

    def test_the_run_manager_still_reports_an_unknown_capture_as_not_found(self) -> None:
        manager = ProjectRunManager(project_storage=self.storage)
        with self.assertRaises(FileNotFoundError):
            manager.start_osc_preprocessing(project_slug="P", capture_name="Missing")

    def test_frame_collection_for_a_valid_capture_returns_all_four_frame_types(self) -> None:
        manager = ProjectRunManager(project_storage=self.storage)
        sources = manager._collect_capture_frame_sources(self.capture)
        self.assertEqual({frame_type for frame_type, _ in sources}, {"bias", "dark", "flat", "light"})
        self.assertTrue(all(path_is_within(path.resolve(), self.capture.resolve()) for _, path in sources))

    @needs_redirects
    def test_frame_collection_refuses_files_that_lie_outside_the_capture(self) -> None:
        manager = ProjectRunManager(project_storage=self.storage)
        shutil.rmtree(self.capture / "flats")
        self.redirect(self.capture / "flats", self.secret / "lights")
        with self.assertRaises(ValueError) as caught:
            manager._collect_capture_frame_sources(self.capture)
        self.assertIn("flats/stolen.fit", str(caught.exception))

    @needs_redirects
    def test_frame_collection_refuses_a_capture_that_resolves_outside_captures(self) -> None:
        manager = ProjectRunManager(project_storage=self.storage)
        self.redirect(self.captures / "Escaped", self.secret)
        with self.assertRaises(ValueError):
            manager._collect_capture_frame_sources(self.captures / "Escaped")

    @needs_redirects
    def test_starting_a_run_on_a_redirected_capture_creates_no_run(self) -> None:
        manager = ProjectRunManager(project_storage=self.storage)
        self.redirect(self.captures / "Escaped", self.secret)
        before = snapshot(self.layout.runs_dir)
        with self.assertRaises(ValueError):
            manager.start_osc_preprocessing(project_slug="P", capture_name="Escaped")
        self.assertEqual(snapshot(self.layout.runs_dir), before)

    def frame_sources(self) -> list[tuple[str, Path]]:
        return [(kind, self.capture / folder / f"{folder}_001.CR2")
                for kind, folder in (("bias", "biases"), ("dark", "darks"), ("flat", "flats"), ("light", "lights"))]

    def test_prepare_siril_run_keeps_the_run_directory_inside_runs_for_hostile_run_ids(self) -> None:
        for index, run_id in enumerate(["../../escape", "..", "a/../../b", "..\\..\\x", "C:\\evil", "/abs/run"]):
            with self.subTest(run_id):
                layout = self.storage.prepare_siril_run("P", run_id, frame_sources=self.frame_sources())
                self.assertEqual(layout.run_root.resolve().parent, self.layout.runs_dir.resolve())
                self.assertTrue(path_is_within(layout.workspace_dir.resolve(), self.layout.runs_dir.resolve()))
        self.assertFalse((self.base / "escape").exists())
        self.assertFalse((self.root / "escape").exists())

    def test_prepare_siril_run_with_an_invalid_project_name_creates_nothing(self) -> None:
        before = snapshot(self.base)
        for name in ("..", "a/b", "C:", ""):
            with self.subTest(repr(name)), self.assertRaises(ValueError):
                self.storage.prepare_siril_run(name, "run-1", frame_sources=self.frame_sources())
        self.assertEqual(snapshot(self.base), before)


# ---------------------------------------------------------------------------
# Thumbnail helper
# ---------------------------------------------------------------------------


@unittest.skipIf(Image is None, "Pillow is required for thumbnails")
class ThumbnailBoundaryTests(BoundaryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.png = self.capture / "lights" / "preview.png"
        Image.new("RGB", (16, 16), (10, 200, 10)).save(self.png)

    def thumbnail(self, *, capture_name: str = "Night 1", source: Path | None = None):
        return _ensure_capture_thumbnail(self.storage, project_slug="P", capture_name=capture_name,
                                         source_path=(source or self.png).resolve(), size=64)

    def test_a_valid_capture_gets_only_its_derived_cache(self) -> None:
        before = snapshot(self.base)
        thumbnail = self.thumbnail()
        self.assertTrue(thumbnail.is_file())
        created = {entry.replace("\\", "/") for entry in set(snapshot(self.base)) - set(before)}
        self.assertTrue(all(entry.startswith("projects/P/.cache") for entry in created), created)
        self.assertTrue(path_is_within(thumbnail.resolve(), self.layout.project_root.resolve()))

    def test_invalid_capture_names_create_nothing(self) -> None:
        before = snapshot(self.base)
        for name in INVALID_NAMES:
            with self.subTest(repr(name)), self.assertRaises(FileNotFoundError):
                self.thumbnail(capture_name=name)
        self.assertEqual(snapshot(self.base), before)
        self.assertFalse((self.layout.project_root / ".cache").exists())

    def test_a_source_outside_the_capture_creates_nothing(self) -> None:
        outside = self.base / "outside.png"
        Image.new("RGB", (8, 8)).save(outside)
        other = self.captures / "Other" / "lights" / "o.png"
        Image.new("RGB", (8, 8)).save(other)
        before = snapshot(self.base)
        for source in (outside, other, self.layout.project_root / "lights" / "PROJECT_LEVEL.CR2"):
            with self.subTest(str(source.name)), self.assertRaises(FileNotFoundError):
                self.thumbnail(source=source)
        self.assertEqual(snapshot(self.base), before)
        self.assertFalse((self.layout.project_root / ".cache").exists())

    def test_an_unknown_project_or_capture_creates_nothing(self) -> None:
        before = snapshot(self.base)
        with self.assertRaises(FileNotFoundError):
            _ensure_capture_thumbnail(self.storage, project_slug="Ghost", capture_name="Night 1",
                                      source_path=self.png.resolve(), size=64)
        with self.assertRaises(FileNotFoundError):
            self.thumbnail(capture_name="Missing")
        self.assertEqual(snapshot(self.base), before)

    @needs_redirects
    def test_a_cache_directory_that_redirects_outside_the_project_is_refused(self) -> None:
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        self.redirect(self.layout.project_root / ".cache", elsewhere)
        with self.assertRaises(OSError):
            self.thumbnail()
        self.assertEqual(list(elsewhere.iterdir()), [])  # nothing was written outside the project


# ---------------------------------------------------------------------------
# HTTP routes
# ---------------------------------------------------------------------------


class HttpBoundaryTests(BoundaryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.database = self.base / "tsn_dss.db"
        self.server = create_http_server(host="127.0.0.1", port=0, projects_root=self.root, database_path=self.database)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.stop)

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def call(self, method: str, path: str, payload: dict | None = None) -> tuple[int, bytes]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(f"{self.url}{path}", data=data, method=method,
                          headers={"Content-Type": "application/json"} if data else {})
        try:
            with urlopen(request) as response:
                return response.status, response.read()
        except HTTPError as error:
            return error.code, error.read()

    def error_code(self, body: bytes) -> str:
        return json.loads(body.decode("utf-8")).get("error", "")

    HOSTILE_URL_NAMES = ["..", "%2E%2E", quote("../x", safe=""), quote("x/../y", safe=""), quote("a/b", safe=""),
                         quote("a\\b", safe=""), quote("/abs/posix", safe=""), quote("C:", safe=""),
                         quote("C:\\Windows", safe=""), quote("\\rooted", safe=""), "."]

    def test_invalid_capture_names_are_404_on_every_read_route_and_create_nothing(self) -> None:
        before = snapshot(self.base)
        for name in self.HOSTILE_URL_NAMES:
            for route in (f"/api/projects/P/captures/{name}",
                          f"/api/projects/P/captures/{name}/files/lights/lights_001.CR2",
                          f"/api/projects/P/captures/{name}/thumbnails/lights/preview.png"):
                status, body = self.call("GET", route)
                self.assertEqual(status, 404, route)
        self.assertEqual(snapshot(self.base), before)

    def test_a_dotdot_capture_no_longer_exposes_project_level_files(self) -> None:
        status, body = self.call("GET", "/api/projects/P/captures/../files/lights/PROJECT_LEVEL.CR2")
        self.assertEqual(status, 404)
        self.assertNotIn(b"project", body.lower().replace(b"project_level", b"").replace(b"not", b""))  # no file bytes leaked

    def test_a_legitimate_nested_file_is_served(self) -> None:
        status, body = self.call("GET", "/api/projects/P/captures/Night%201/files/lights/nested/deep.CR2")
        self.assertEqual((status, body), (200, b"deep"))

    def test_escaping_file_paths_are_a_400_and_serve_nothing(self) -> None:
        secret = str(self.secret / "lights" / "stolen.fit")
        for relative in ("..%2F..%2Fsecret%2Flights%2Fstolen.fit", "..%2F..%2F..%2F..%2Fsecret%2Flights%2Fstolen.fit",
                         quote(secret, safe=""), quote("/etc/passwd", safe=""), quote("C:\\Windows\\win.ini", safe=""),
                         quote("lights/../../Other/lights/other.CR2", safe="/")):
            status, body = self.call("GET", f"/api/projects/P/captures/Night%201/files/{relative}")
            self.assertEqual((status, self.error_code(body)), (400, "invalid_capture_file_path"), relative)
            self.assertNotIn(b"SECRET", body)

    def test_escaping_thumbnail_paths_are_a_400(self) -> None:
        status, body = self.call("GET", "/api/projects/P/captures/Night%201/thumbnails/..%2F..%2Fsecret%2Flights%2Fstolen.fit")
        self.assertEqual((status, self.error_code(body)), (400, "invalid_capture_thumbnail_path"))

    def test_importing_with_an_invalid_capture_name_is_a_400_and_changes_nothing(self) -> None:
        source = self.base / "import-src"
        make_raw_capture(source)
        before = snapshot(self.base)
        for name in ("..", "../x", "a/b", "a\\b", ".", "C:", "/abs"):
            status, body = self.call("POST", "/api/import-capture",
                                     {"project_slug": "P", "capture_name": name, "source_dir": str(source)})
            self.assertEqual((status, self.error_code(body)), (400, "capture_import_failed"), name)
        self.assertEqual(snapshot(self.base), before)
        self.assertTrue((source / "lights" / "lights_001.CR2").exists())

    def test_importing_with_a_unicode_capture_name_works(self) -> None:
        source = self.base / "import-src"
        make_raw_capture(source)
        status, body = self.call("POST", "/api/import-capture",
                                 {"project_slug": "P", "capture_name": "M31 \u2014 Andromeda \u017c", "source_dir": str(source)})
        self.assertEqual(status, 201)
        self.assertTrue((self.captures / "M31 \u2014 Andromeda \u017c" / "lights" / "lights_001.CR2").is_file())

    def test_starting_a_run_with_an_invalid_capture_name_is_a_400_and_changes_nothing(self) -> None:
        before = snapshot(self.base)
        for name in ("..", "../x", "a/b", "a\\b", ".", "C:"):
            status, body = self.call("POST", "/api/project-runs", {"project_slug": "P", "capture_name": name})
            self.assertEqual((status, self.error_code(body)), (400, "run_start_failed"), name)
        self.assertEqual(snapshot(self.base), before)

    @needs_redirects
    def test_a_redirected_capture_is_404_over_http(self) -> None:
        self.redirect(self.captures / "Escaped", self.secret)
        for route in ("/api/projects/P/captures/Escaped", "/api/projects/P/captures/Escaped/files/lights/stolen.fit"):
            status, body = self.call("GET", route)
            self.assertEqual(status, 404, route)
            self.assertNotIn(b"SECRET", body)


if __name__ == "__main__":
    unittest.main()
