from __future__ import annotations

"""Stage S3: the read-only capture registrar, FITS/CR2 metadata, boundary safety and import dual-write."""

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np
from astropy.io import fits

try:
    from test_capture_boundary import CAN_REDIRECT, make_dir_redirect
    from test_project_storage_reads import StorageTestCase
except ModuleNotFoundError:
    from tests.test_capture_boundary import CAN_REDIRECT, make_dir_redirect
    from tests.test_project_storage_reads import StorageTestCase
from tsn_dss.engine import capture_registry
from tsn_dss.engine.capture_registry import CaptureRegistrar, main as registrar_main
from tsn_dss.engine.project_registry import ProjectRegistry
from tsn_dss.engine.sqlite.captures import CaptureRepository
from tsn_dss.engine.sqlite.db import DEFAULT_SCHEMA_PATH, connect_database, initialize_database
from tsn_dss.engine.sqlite.frames import FrameRepository
from tsn_dss.engine.sqlite.migrations import MIGRATIONS, get_user_version, initialize_schema
from tsn_dss.engine.sqlite.project_repository import ProjectRepository
from tsn_dss.gui.http_api import create_http_server

needs_redirects = unittest.skipUnless(CAN_REDIRECT, "directory symlinks/junctions are unavailable on this platform")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_state(root: Path) -> dict[str, tuple]:
    """Byte, size, and modification-time picture of a tree: proves nothing was written, renamed or touched."""
    state: dict[str, tuple] = {}
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.stat()
        relative = str(path.relative_to(root))
        if path.is_dir():
            state[relative] = ("dir",)
        else:
            state[relative] = ("file", info.st_size, info.st_mtime_ns, sha256(path))
    return state


def make_fits(path: Path, *, shape: tuple[int, int] = (4, 6), **cards) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = fits.Header()
    for keyword, value in cards.items():
        header[keyword.replace("_", "-") if keyword.startswith("CCD") or keyword.startswith("DATE") else keyword] = value
    fits.PrimaryHDU(data=np.zeros(shape, dtype=np.uint16), header=header).writeto(path)
    return path


class RegistrarTestCase(StorageTestCase):
    """A projects root with one canonical project ``M42`` and its connection (database kept outside the tree)."""

    def setUp(self) -> None:
        super().setUp()
        self.layout = self.storage.ensure_project("M42")
        self.night = self.layout.captures_dir / "Night1"
        self.db_path = self.base / "tsn.db"
        self.connection = initialize_database(self.db_path)
        self.addCleanup(self.connection.close)
        ProjectRegistry(self.storage, self.connection).register_existing()
        self.registrar = CaptureRegistrar(self.storage, self.connection)
        self.captures = CaptureRepository(self.connection)
        self.frames = FrameRepository(self.connection)
        self.project = ProjectRepository(self.connection).get_project_by_dir_key("M42")

    def write(self, capture: Path, relative: str, content: bytes) -> Path:
        path = capture / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def make_cr2_night(self) -> None:
        for folder, count in (("biases", 2), ("darks", 2), ("flats", 3), ("lights", 4)):
            for index in range(count):
                self.write(self.night, f"{folder}/{folder}_{index:03d}.CR2", f"{folder}-{index}".encode() * 50)

    def register(self, **kwargs):
        return self.registrar.register_all(**kwargs)

    def rows(self) -> list[tuple]:
        return [tuple(row) for row in self.connection.execute(
            "SELECT id, capture_id, rel_path, frame_type, origin, file_format, size_bytes, content_sha256, hashed_at, metadata_json "
            "FROM frames ORDER BY rel_path")]


# ---------------------------------------------------------------------------
# Core behaviour: CR2 trees, read-only, idempotent
# ---------------------------------------------------------------------------


class RegistrarCoreTests(RegistrarTestCase):
    def test_registers_a_cr2_capture_with_types_paths_hashes_and_no_invented_metadata(self) -> None:
        self.make_cr2_night()
        mtime_ns = 1_500_000_000_000_000_000
        for path in self.night.rglob("*.CR2"):
            os.utime(path, ns=(mtime_ns, mtime_ns))  # a file time that must never become an acquisition time

        report = self.register()

        self.assertEqual((report.frames_created, len(report.captures_created)), (11, 1))
        self.assertEqual(dict(report.frames_by_type), {"bias": 2, "dark": 2, "flat": 3, "light": 4})
        self.assertEqual(dict(report.frames_by_format), {"cr2": 11})
        capture = self.captures.get_capture_by_name(self.project.id, "Night1")
        self.assertEqual((capture.rel_path, capture.source_kind, capture.registrar_version),
                         ("captures/Night1", "legacy_registered", capture_registry.REGISTRAR_VERSION))
        frames = self.frames.list_frames(capture_id=capture.id)
        self.assertEqual(len(frames), 11)
        for frame in frames:
            absolute = self.layout.project_root / frame.rel_path
            self.assertTrue(frame.rel_path.startswith("captures/Night1/") and "\\" not in frame.rel_path)
            self.assertEqual((frame.content_sha256, frame.size_bytes), (sha256(absolute), absolute.stat().st_size))
            self.assertEqual((frame.file_format, frame.origin), ("cr2", "raw"))
            self.assertEqual(frame.metadata["registration"]["frame_type_basis"], f"directory:{frame.rel_path.split('/')[2]}")
            # CR2 carries no EXIF here: nothing is guessed, and the file time is not an acquisition time.
            for name in ("captured_at", "captured_at_source", "exposure_s", "gain", "iso", "width_px", "instrument_name"):
                self.assertIsNone(getattr(frame, name), name)
            self.assertNotIn("fits", frame.metadata)
            self.assertIsNone(frame.observation_id)

    def test_registration_never_modifies_creates_or_renames_anything_in_the_project_tree(self) -> None:
        self.make_cr2_night()
        make_fits(self.night / "lights" / "a.fit", EXPTIME=10.0)
        (self.night / "notes.txt").write_text("keep me", encoding="utf-8")
        before = tree_state(self.base / "projects")

        self.register()
        self.register(verify_hashes=True)
        self.register(dry_run=True)

        self.assertEqual(tree_state(self.base / "projects"), before)  # bytes, sizes, mtimes, no new/removed entries

    def test_registration_works_on_read_only_files(self) -> None:
        # A file that cannot be opened for writing proves the registrar only ever reads.
        self.make_cr2_night()
        for path in self.night.rglob("*.CR2"):
            path.chmod(stat.S_IREAD)
            self.addCleanup(path.chmod, stat.S_IREAD | stat.S_IWRITE)
        self.assertEqual(self.register().frames_created, 11)

    def test_a_second_run_creates_nothing_and_keeps_every_identity(self) -> None:
        self.make_cr2_night()
        self.register()
        before = self.rows()

        report = self.register()

        self.assertEqual((report.frames_created, report.frames_unchanged, report.hashes_recorded), (0, 11, 0))
        self.assertEqual((report.captures_created, report.captures_unchanged), ([], ["M42/Night1"]))
        self.assertEqual(self.rows(), before)  # ids, hashes and hashed_at all identical
        self.assertEqual(report.conflicts, [])

    def test_only_new_files_are_added_on_a_later_run(self) -> None:
        self.make_cr2_night()
        self.register()
        before = {row[2]: row for row in self.rows()}
        self.write(self.night, "lights/extra.CR2", b"more light")

        report = self.register()

        self.assertEqual((report.frames_created, report.frames_unchanged), (1, 11))
        after = {row[2]: row for row in self.rows()}
        self.assertEqual({path: after[path] for path in before}, before)
        self.assertEqual(after["captures/Night1/lights/extra.CR2"][3], "light")

    def test_dry_run_reports_but_writes_nothing(self) -> None:
        self.make_cr2_night()

        report = self.register(dry_run=True)

        self.assertTrue(report.dry_run)
        self.assertEqual((report.frames_created, report.filesystem_only_captures), (11, ["M42/Night1"]))
        self.assertEqual(report.captures_created, [])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM captures").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM frames").fetchone()[0], 0)

    def test_dry_run_duplicate_reports_are_stable_across_repeated_dry_runs(self) -> None:
        self.write(self.night, "lights/one.CR2", b"same")
        self.write(self.night, "lights/two.CR2", b"same")
        first = self.register(dry_run=True)
        second = self.registrar.register_all(dry_run=True)
        self.assertEqual(first.duplicate_hashes, second.duplicate_hashes)
        (digest, paths), = first.duplicate_hashes.items()
        self.assertEqual(paths, ["M42/captures/Night1/lights/one.CR2", "M42/captures/Night1/lights/two.CR2"])

    def test_duplicate_content_is_registered_reported_and_never_removed(self) -> None:
        self.write(self.night, "lights/one.CR2", b"same bytes")
        self.write(self.night, "flats/two.CR2", b"same bytes")
        self.write(self.night, "lights/unique.CR2", b"different")

        report = self.register()

        self.assertEqual(report.frames_created, 3)
        (paths,) = report.duplicate_hashes.values()
        self.assertEqual(len(paths), 2)
        self.assertTrue((self.night / "lights" / "one.CR2").exists() and (self.night / "flats" / "two.CR2").exists())
        self.assertEqual(len(self.frames.list_duplicate_hashes(self.project.id)), 1)

    def test_layouts_are_not_assumed_lights_only_calibration_only_nested_and_root_files(self) -> None:
        lights_only = self.layout.captures_dir / "LightsOnly"
        calibration_only = self.layout.captures_dir / "CalibrationOnly"
        self.write(lights_only, "lights/deep/er/a.CR2", b"a")
        self.write(lights_only, "loose.CR2", b"loose")
        self.write(calibration_only, "Darks/d1.CR2", b"d")  # folder names are case-insensitive
        self.write(calibration_only, "flats/f1.FIT", b"not really fits")  # an unreadable header still registers
        self.layout.captures_dir.joinpath("Empty").mkdir()

        report = self.register()

        self.assertEqual(sorted(report.captures_created), ["M42/CalibrationOnly", "M42/Empty", "M42/LightsOnly"])
        types = {row[2]: row[3] for row in self.rows()}
        self.assertEqual(types["captures/LightsOnly/lights/deep/er/a.CR2"], "light")
        self.assertIsNone(types["captures/LightsOnly/loose.CR2"])  # unknown stays unknown
        self.assertEqual(types["captures/CalibrationOnly/Darks/d1.CR2"], "dark")
        self.assertEqual(types["captures/CalibrationOnly/flats/f1.FIT"], "flat")
        loose = self.frames.get_frame_by_path(self.project.id, "captures/LightsOnly/loose.CR2")
        self.assertIsNone(loose.metadata["registration"]["frame_type_basis"])
        self.assertEqual(self.frames.list_frames(capture_id=self.captures.get_capture_by_name(self.project.id, "Empty").id), [])

    def test_unicode_and_space_names_register_with_posix_project_relative_paths(self) -> None:
        name = "M31 — Andromeda"
        self.write(self.layout.captures_dir / name, "lights/żółw 1.CR2", b"x")
        self.register()
        self.assertEqual([row[2] for row in self.rows()], [f"captures/{name}/lights/żółw 1.CR2"])

    def test_only_observational_formats_become_frames_and_the_rest_is_reported_by_suffix(self) -> None:
        for name in ("a.CR2", "b.fit", "c.FITS", "d.Fts", "e.fit.bak"):
            self.write(self.night, f"lights/{name}", b"data")
        for name in ("thumb.jpg", "notes.txt", "meta.json", "noext"):
            self.write(self.night, f"lights/{name}", b"other")

        report = self.register()

        self.assertEqual(sorted(row[2].rsplit("/", 1)[1] for row in self.rows()), ["a.CR2", "b.fit", "c.FITS", "d.Fts"])
        self.assertEqual(dict(report.ignored_suffixes), {".bak": 1, ".jpg": 1, ".txt": 1, ".json": 1, "(none)": 1})

    def test_a_legacy_frame_row_without_a_hash_receives_it_exactly_once(self) -> None:
        capture = self.captures.register_capture(project_id=self.project.id, name="Night1", source_kind="legacy_registered")
        self.write(self.night, "lights/a.CR2", b"payload")
        from tsn_dss.domain.models import Frame
        legacy = self.frames.create_frame(Frame(project_id=self.project.id, capture_id=capture.id,
                                                rel_path="captures/Night1/lights/a.CR2", frame_type="light"))
        report = self.register()
        self.assertEqual((report.frames_created, report.hashes_recorded), (0, 1))
        recorded = self.frames.get_frame(legacy.id)
        self.assertEqual((recorded.content_sha256, recorded.size_bytes), (sha256(self.night / "lights" / "a.CR2"), 7))
        self.assertEqual(self.register().hashes_recorded, 0)

    def test_registered_files_that_disappear_are_reported_and_their_records_are_kept(self) -> None:
        self.make_cr2_night()
        self.register()
        (self.night / "lights" / "lights_000.CR2").unlink()

        report = self.register()

        self.assertEqual(report.missing_files, ["M42/captures/Night1/lights/lights_000.CR2"])
        self.assertEqual(len(self.rows()), 11)  # provenance is never deleted by the registrar

    def test_captures_without_a_directory_and_projects_without_a_directory_are_reported_not_deleted(self) -> None:
        self.make_cr2_night()
        self.register()
        import shutil
        shutil.rmtree(self.night)
        report = self.register()
        self.assertEqual(report.db_only_captures, ["M42/Night1"])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM captures").fetchone()[0], 1)

        shutil.rmtree(self.layout.project_root)
        report = self.register()
        self.assertEqual((report.projects_skipped, report.db_only_captures), (["M42"], ["M42/Night1"]))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM frames").fetchone()[0], 11)

    def test_a_directory_without_a_canonical_project_is_reported_and_not_registered(self) -> None:
        self.storage.ensure_project("Stray")
        self.write(self.layout.projects_root if hasattr(self.layout, "projects_root") else self.root / "Stray" / "captures" / "C", "lights/x.CR2", b"x")
        report = self.register()
        self.assertEqual(report.filesystem_only_projects, ["Stray"])
        self.assertIsNone(ProjectRepository(self.connection).get_project_by_dir_key("Stray"))

    def test_registering_an_unknown_project_is_an_error(self) -> None:
        with self.assertRaises(KeyError):
            self.registrar.register_project("Nobody")
        self.assertEqual(self.registrar.register_project("M42").projects_seen, ["M42"])


# ---------------------------------------------------------------------------
# Changed bytes
# ---------------------------------------------------------------------------


class RegistrarChangedFileTests(RegistrarTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.path = self.write(self.night, "lights/a.CR2", b"original bytes")
        self.register()
        self.recorded = self.rows()

    def test_a_changed_file_is_a_conflict_and_the_recorded_identity_is_kept(self) -> None:
        self.path.write_bytes(b"tampered bytes!!")

        report = self.register()

        (conflict,) = report.conflicts
        self.assertEqual((conflict.rel_path, conflict.recorded_sha256), ("captures/Night1/lights/a.CR2", self.recorded[0][7]))
        self.assertEqual(conflict.current_sha256, sha256(self.path))
        self.assertEqual(self.rows(), self.recorded)  # nothing updated, not the hash, size or time
        self.assertEqual(self.path.read_bytes(), b"tampered bytes!!")  # and the file is left exactly as found
        self.assertEqual(len(self.register().conflicts), 1)  # it stays a conflict until a human decides

    def test_same_size_same_time_changes_are_only_found_by_verify_hashes(self) -> None:
        info = self.path.stat()
        self.path.write_bytes(b"ORIGINAL BYTES")  # same length, different content
        os.utime(self.path, ns=(info.st_atime_ns, info.st_mtime_ns))

        self.assertEqual(self.register().conflicts, [])  # the cheap check trusts size and mtime
        report = self.register(verify_hashes=True)
        self.assertEqual(len(report.conflicts), 1)
        self.assertEqual(self.rows(), self.recorded)

    def test_touching_a_file_without_changing_bytes_is_not_a_conflict(self) -> None:
        os.utime(self.path, ns=(1_600_000_000_000_000_000,) * 2)
        report = self.register()
        self.assertEqual((report.conflicts, report.frames_unchanged), ([], 1))
        self.assertEqual(self.rows(), self.recorded)

    def test_the_command_line_exits_non_zero_on_a_conflict(self) -> None:
        self.path.write_bytes(b"tampered bytes!!")
        self.connection.close()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = registrar_main(["--projects-root", str(self.root), "--database-path", str(self.db_path)])
        self.assertEqual(code, 1)
        self.assertIn("CONFLICT", err.getvalue())
        self.assertEqual(self.path.read_bytes(), b"tampered bytes!!")

    def test_a_file_that_changes_while_being_read_is_skipped_this_run(self) -> None:
        self.write(self.night, "lights/b.CR2", b"unstable")
        real = capture_registry._sha256_stable
        with patch.object(capture_registry, "_sha256_stable",
                          side_effect=lambda p: (real(p)[0], real(p)[1], False) if p.name == "b.CR2" else real(p)):
            report = self.register()
        self.assertEqual(report.frames_created, 0)
        self.assertTrue(any("changed while being read" in text for text in report.warnings))
        self.assertEqual(self.register().frames_created, 1)  # registered on the next, stable run

    def test_an_unreadable_file_is_reported_without_stopping_the_run(self) -> None:
        self.write(self.night, "lights/b.CR2", b"locked")
        self.write(self.night, "lights/c.CR2", b"fine")
        real = capture_registry._sha256_stable

        def flaky(path: Path):
            if path.name == "b.CR2":
                raise PermissionError("locked by another process")
            return real(path)

        with patch.object(capture_registry, "_sha256_stable", side_effect=flaky):
            report = self.register()
        self.assertEqual(report.frames_created, 1)
        self.assertEqual(len(report.unreadable_files), 1)
        self.assertIn("b.CR2", report.unreadable_files[0])


# ---------------------------------------------------------------------------
# FITS
# ---------------------------------------------------------------------------


class RegistrarFitsTests(RegistrarTestCase):
    def frame_for(self, relative: str):
        return self.frames.get_frame_by_path(self.project.id, f"captures/Night1/{relative}")

    def test_conservative_normalization_of_standard_keywords_and_raw_header_evidence(self) -> None:
        path = make_fits(
            self.night / "lights" / "a.fit", shape=(4, 6), **{
                "DATE-OBS": "2026-09-01T22:15:30.250", "EXPTIME": 10.5, "GAIN": 100, "OFFSET": 50,
                "CCD-TEMP": -10.5, "XBINNING": 2, "YBINNING": 2, "FILTER": " L-eXtreme ", "INSTRUME": "ZWO ASI533MC Pro",
                "IMAGETYP": "Light Frame", "RA": 83.8, "DEC": -5.4, "OBJCTRA": "05 35 17", "EGAIN": 0.25, "SET-TEMP": -10.0,
            })
        before = tree_state(self.night)

        report = self.register()

        frame = self.frame_for("lights/a.fit")
        self.assertEqual((frame.file_format, frame.origin, frame.frame_type), ("fits", "unknown", "light"))
        self.assertEqual((frame.width_px, frame.height_px), (6, 4))
        self.assertEqual((frame.captured_at, frame.captured_at_source), ("2026-09-01T22:15:30.250Z", "fits_header"))
        self.assertEqual((frame.exposure_s, frame.gain, frame.offset_value, frame.camera_temp_c), (10.5, 100.0, 50.0, -10.5))
        self.assertEqual((frame.binning_x, frame.binning_y), (2, 2))
        self.assertEqual((frame.filter_name, frame.instrument_name), ("L-eXtreme", "ZWO ASI533MC Pro"))
        # Ambiguous keywords are evidence only, never normalized.
        for name in ("mount_ra_deg", "mount_dec_deg", "planned_ra_deg", "planned_dec_deg", "iso", "stack_count"):
            self.assertIsNone(getattr(frame, name), name)
        cards = frame.metadata["fits"]["cards"]
        self.assertEqual((cards["RA"], cards["EGAIN"], cards["SET-TEMP"], cards["DATE-OBS"]), (83.8, 0.25, -10.0, "2026-09-01T22:15:30.250"))
        self.assertFalse(frame.metadata["fits"]["truncated"])
        self.assertEqual(report.fits_frames, 1)
        self.assertEqual(report.fits_field_coverage["captured_at"], 1)
        self.assertEqual(tree_state(self.night), before)

    def test_absent_and_unusable_values_stay_none_and_are_warned_about(self) -> None:
        make_fits(self.night / "lights" / "sparse.fit")
        make_fits(self.night / "lights" / "bad.fit", **{"DATE-OBS": "yesterday", "EXPTIME": "abc", "GAIN": "n/a", "XBINNING": 0})
        make_fits(self.night / "lights" / "dateonly.fit", **{"DATE-OBS": "2026-09-01", "EXPTIME": -3.0})

        report = self.register()

        sparse, bad, dateonly = (self.frame_for(f"lights/{n}.fit") for n in ("sparse", "bad", "dateonly"))
        for frame in (sparse, bad, dateonly):
            for name in ("captured_at", "gain", "offset_value", "camera_temp_c", "filter_name", "instrument_name"):
                self.assertIsNone(getattr(frame, name), (frame.rel_path, name))
        self.assertIsNone(sparse.exposure_s)
        self.assertIsNone(bad.exposure_s)
        self.assertIsNone(bad.binning_x)
        self.assertIsNone(dateonly.exposure_s)  # negative exposure is not usable
        joined = "\n".join(report.warnings)
        for fragment in ("DATE-OBS is not an ISO-8601", "not a usable exposure", "GAIN is not numeric", "XBINNING", "date but no time"):
            self.assertIn(fragment, joined)
        self.assertEqual(report.fits_field_coverage["captured_at"], 0)

    def test_timestamps_are_normalized_to_utc(self) -> None:
        for name, value in (("naive", "2026-09-01T22:15:30"), ("zulu", "2026-09-01T22:15:30Z"), ("offset", "2026-09-02T00:15:30+02:00")):
            make_fits(self.night / "lights" / f"{name}.fit", **{"DATE-OBS": value})
        self.register()
        for name in ("naive", "zulu", "offset"):
            self.assertEqual(self.frame_for(f"lights/{name}.fit").captured_at, "2026-09-01T22:15:30Z", name)

    def test_zero_exposure_is_valid_for_bias_and_exposure_is_the_fallback_keyword(self) -> None:
        make_fits(self.night / "biases" / "b.fit", EXPTIME=0.0)
        make_fits(self.night / "darks" / "d.fit", EXPOSURE=30.0)
        self.register()
        self.assertEqual(self.frame_for("biases/b.fit").exposure_s, 0.0)
        self.assertEqual(self.frame_for("darks/d.fit").exposure_s, 30.0)

    def test_frame_type_prefers_the_folder_records_conflicts_and_falls_back_to_imagetyp(self) -> None:
        make_fits(self.night / "lights" / "mislabeled.fit", IMAGETYP="Dark Frame")
        make_fits(self.night / "loose_flat.fit", IMAGETYP="FLAT")
        make_fits(self.night / "loose_odd.fit", IMAGETYP="Science")
        make_fits(self.night / "loose_none.fit")

        report = self.register()

        mislabeled = self.frame_for("lights/mislabeled.fit")
        self.assertEqual(mislabeled.frame_type, "light")
        self.assertEqual(mislabeled.metadata["registration"]["frame_type_conflict"], {"directory": "light", "fits_imagetyp": "Dark Frame"})
        self.assertTrue(any("kept the folder-based type" in text for text in report.warnings))
        flat = self.frame_for("loose_flat.fit")
        self.assertEqual((flat.frame_type, flat.metadata["registration"]["frame_type_basis"]), ("flat", "fits:IMAGETYP"))
        for name in ("loose_odd.fit", "loose_none.fit"):
            self.assertIsNone(self.frame_for(name).frame_type)

    def test_unreadable_fits_still_registers_a_hashed_frame_and_never_breaks_the_run(self) -> None:
        self.write(self.night, "lights/garbage.fit", b"this is not a fits file at all" * 10)
        self.write(self.night, "lights/empty.fits", b"")
        make_fits(self.night / "lights" / "good.fit", EXPTIME=5.0)

        report = self.register()

        self.assertEqual(report.frames_created, 3)
        garbage = self.frame_for("lights/garbage.fit")
        self.assertEqual(garbage.content_sha256, sha256(self.night / "lights" / "garbage.fit"))
        self.assertIn("parse_error", garbage.metadata["registration"])
        self.assertNotIn("fits", garbage.metadata)
        self.assertIsNone(garbage.exposure_s)
        self.assertEqual(self.frame_for("lights/good.fit").exposure_s, 5.0)
        self.assertGreaterEqual(sum("could not read FITS metadata" in text for text in report.warnings), 2)
        self.assertEqual(self.frame_for("lights/empty.fits").size_bytes, 0)

    def test_long_headers_are_bounded_and_marked_truncated(self) -> None:
        cards = {f"KEY{index:04d}": index for index in range(700)}
        cards["LONGSTR"] = "x" * 1000
        path = self.night / "lights" / "long.fit"
        path.parent.mkdir(parents=True)
        header = fits.Header()
        for key, value in cards.items():
            header[key] = value
        fits.PrimaryHDU(data=np.zeros((2, 2), dtype=np.uint16), header=header).writeto(path)
        self.register()
        metadata = self.frame_for("lights/long.fit").metadata["fits"]
        self.assertTrue(metadata["truncated"])
        self.assertLessEqual(len(metadata["cards"]), 500)
        json.dumps(metadata)  # stored as JSON

    def test_fits_files_are_opened_read_only(self) -> None:
        path = make_fits(self.night / "lights" / "ro.fit", EXPTIME=1.0)
        path.chmod(stat.S_IREAD)
        self.addCleanup(path.chmod, stat.S_IREAD | stat.S_IWRITE)
        self.assertEqual(self.register().frames_created, 1)
        self.assertEqual(self.frame_for("lights/ro.fit").exposure_s, 1.0)


# ---------------------------------------------------------------------------
# Boundary safety
# ---------------------------------------------------------------------------


@needs_redirects
class RegistrarBoundaryTests(RegistrarTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.secret = self.base / "secret"
        self.write(self.secret, "lights/stolen.CR2", b"SECRET")
        self.write(self.night, "lights/mine.CR2", b"mine")
        self.hashed: list[Path] = []
        real = capture_registry._sha256_stable

        def spy(path: Path):
            self.hashed.append(path)
            return real(path)

        patcher = patch.object(capture_registry, "_sha256_stable", side_effect=spy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def link(self, link: Path, target: Path) -> None:
        self.assertTrue(make_dir_redirect(link, target))
        self.addCleanup(lambda: os.path.isdir(link) and os.rmdir(link))

    def test_a_subfolder_redirected_outside_the_capture_is_reported_and_never_read(self) -> None:
        self.link(self.night / "darks", self.secret)

        report = self.register()

        self.assertEqual([row[2] for row in self.rows()], ["captures/Night1/lights/mine.CR2"])
        self.assertTrue(any("darks" in item for item in report.outside_boundary))
        self.assertTrue(all("secret" not in str(path) for path in self.hashed))
        self.assertTrue((self.secret / "lights" / "stolen.CR2").exists())

    def test_a_subfolder_redirected_to_a_sibling_capture_is_outside_this_capture(self) -> None:
        other = self.layout.captures_dir / "Other"
        self.write(other, "lights/o.CR2", b"other")
        self.link(self.night / "flats", other)

        report = self.register()

        night_paths = [row[2] for row in self.rows() if row[2].startswith("captures/Night1/")]
        self.assertEqual(night_paths, ["captures/Night1/lights/mine.CR2"])
        self.assertTrue(any("flats" in item for item in report.outside_boundary))

    def test_a_capture_that_redirects_outside_captures_is_skipped_entirely(self) -> None:
        self.link(self.layout.captures_dir / "Escaped", self.secret)

        report = self.register()

        self.assertIn("M42/Escaped", report.outside_boundary)
        self.assertIsNone(self.captures.get_capture_by_name(self.project.id, "Escaped"))
        self.assertTrue(all("secret" not in str(path) for path in self.hashed))
        self.assertEqual([row[2] for row in self.rows()], ["captures/Night1/lights/mine.CR2"])

    def test_a_capture_that_redirects_to_the_project_directory_is_skipped(self) -> None:
        self.link(self.layout.captures_dir / "Loop", self.layout.project_root)
        report = self.register()
        self.assertIn("M42/Loop", report.outside_boundary)
        self.assertIsNone(self.captures.get_capture_by_name(self.project.id, "Loop"))

    def test_a_redirect_cycle_inside_a_capture_terminates(self) -> None:
        self.link(self.night / "lights" / "again", self.night)
        report = self.register()
        self.assertEqual(report.frames_created, 1)  # the file is registered once, not once per cycle


# ---------------------------------------------------------------------------
# Import dual-write through the HTTP API
# ---------------------------------------------------------------------------


class ImportDualWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.base = Path(self.temp_dir.name)
        self.root = self.base / "projects"
        (self.root / "M42" / "captures").mkdir(parents=True)
        (self.root / "M42" / "runs").mkdir(parents=True)
        self.server = create_http_server(host="127.0.0.1", port=0, projects_root=self.root)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.stop)

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def call(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(f"{self.url}{path}", data=data, method=method, headers={"Content-Type": "application/json"} if data else {})
        try:
            with urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def db(self) -> sqlite3.Connection:
        connection = connect_database(self.root / "tsn_dss.db")
        self.addCleanup(connection.close)
        return connection

    def make_source(self, name: str = "src") -> Path:
        source = self.base / name
        for folder, count in (("biases", 1), ("darks", 1), ("flats", 2), ("lights", 3)):
            for index in range(count):
                path = source / folder / f"{folder}_{index}.CR2"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"{folder}{index}".encode() * 20)
        make_fits(source / "lights" / "l.fit", EXPTIME=30.0, **{"DATE-OBS": "2026-09-01T22:00:00"})
        (source / "notes.txt").write_text("ignored", encoding="utf-8")
        return source

    def test_a_copy_import_registers_the_capture_and_frames_and_leaves_the_source_alone(self) -> None:
        source = self.make_source()
        before = tree_state(source)

        status, body = self.call("POST", "/api/import-capture", {"project_slug": "M42", "capture_name": "Night1", "source_dir": str(source)})

        self.assertEqual(status, 201)
        registration = body["capture_registration"]
        self.assertEqual((registration["frames_registered"], registration["ignored_files"]), (8, 1))
        self.assertEqual(tree_state(source), before)  # copy: the original is untouched
        connection = self.db()
        project = ProjectRepository(connection).get_project_by_dir_key("M42")
        capture = CaptureRepository(connection).get_capture_by_name(project.id, "Night1")
        self.assertEqual(registration["capture_id"], capture.id)
        self.assertEqual((capture.source_kind, capture.import_mode, capture.source_path), ("folder_import", "copy", str(source)))
        self.assertTrue(capture.imported_at)
        frames = FrameRepository(connection).list_frames(capture_id=capture.id)
        self.assertEqual(len(frames), 8)
        for frame in frames:
            destination = self.root / "M42" / frame.rel_path
            self.assertEqual(frame.content_sha256, sha256(destination))  # hashes describe the files in the project
        fits_frame = next(frame for frame in frames if frame.file_format == "fits")
        self.assertEqual((fits_frame.frame_type, fits_frame.exposure_s), ("light", 30.0))
        # the legacy filesystem view still shows the capture exactly as before
        _, detail = self.call("GET", "/api/projects/M42/captures/Night1")
        self.assertEqual(detail["capture"]["capture_name"], "Night1")

    def test_a_move_import_records_the_mode_and_registers_from_the_destination(self) -> None:
        source = self.make_source("moved")
        status, body = self.call("POST", "/api/import-capture",
                                 {"project_slug": "M42", "capture_name": "Night2", "source_dir": str(source), "move": True})
        self.assertEqual(status, 201)
        self.assertFalse(source.exists())
        connection = self.db()
        project = ProjectRepository(connection).get_project_by_dir_key("M42")
        capture = CaptureRepository(connection).get_capture_by_name(project.id, "Night2")
        self.assertEqual(capture.import_mode, "move")
        self.assertEqual(body["capture_registration"]["frames_registered"], 8)

    def test_a_registration_failure_is_a_clear_500_that_keeps_the_imported_files_and_is_repairable(self) -> None:
        source = self.make_source()
        with patch.object(CaptureRegistrar, "register_imported_capture", side_effect=RuntimeError("disk on fire")):
            status, body = self.call("POST", "/api/import-capture", {"project_slug": "M42", "capture_name": "Night1", "source_dir": str(source)})
        self.assertEqual((status, body["error"]), (500, "capture_registration_failed"))
        self.assertIn("disk on fire", body["message"])
        self.assertIn("capture_registry", body["message"])
        self.assertTrue((self.root / "M42" / "captures" / "Night1" / "lights" / "lights_0.CR2").is_file())  # never rolled back
        connection = self.db()
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM frames").fetchone()[0], 0)

        code = registrar_main(["--projects-root", str(self.root)])  # the documented repair path
        self.assertEqual(code, 0)
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM frames").fetchone()[0], 8)

    def test_importing_a_capture_with_no_observational_files_registers_an_empty_capture(self) -> None:
        source = self.base / "docs_only"
        for folder in ("biases", "darks", "flats", "lights"):
            (source / folder).mkdir(parents=True)
        (source / "lights" / "readme.txt").write_text("x", encoding="utf-8")
        status, body = self.call("POST", "/api/import-capture", {"project_slug": "M42", "capture_name": "Docs", "source_dir": str(source)})
        self.assertEqual(status, 201)
        self.assertEqual((body["capture_registration"]["frames_registered"], body["capture_registration"]["ignored_files"]), (0, 1))

    def test_importing_an_existing_capture_name_is_still_refused_and_registers_nothing_new(self) -> None:
        source = self.make_source()
        self.call("POST", "/api/import-capture", {"project_slug": "M42", "capture_name": "Night1", "source_dir": str(source)})
        before = self.db().execute("SELECT COUNT(*) FROM frames").fetchone()[0]
        status, body = self.call("POST", "/api/import-capture", {"project_slug": "M42", "capture_name": "Night1", "source_dir": str(source)})
        self.assertEqual((status, body["error"]), (400, "capture_import_failed"))
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM frames").fetchone()[0], before)

    def test_project_deletion_is_refused_while_captures_and_frames_are_registered(self) -> None:
        source = self.make_source()
        self.call("POST", "/api/import-capture", {"project_slug": "M42", "capture_name": "Night1", "source_dir": str(source)})
        status, body = self.call("DELETE", "/api/projects/M42")
        self.assertEqual((status, body["error"]), (409, "project_has_dependents"))
        self.assertEqual((body["capture_count"], body["frame_count"]), (1, 8))
        self.assertIn("registered capture", body["message"])
        self.assertTrue((self.root / "M42" / "captures" / "Night1").is_dir())  # nothing was deleted from disk
        self.assertIsNotNone(ProjectRepository(self.db()).get_project_by_dir_key("M42"))

    def test_server_startup_does_not_hash_or_register_captures(self) -> None:
        # Registration is explicit (import or the standalone command), never a hidden startup cost.
        capture = self.root / "M42" / "captures" / "Pre"
        capture.mkdir()
        (capture / "a.CR2").write_bytes(b"x")
        self.stop()
        self.server = create_http_server(host="127.0.0.1", port=0, projects_root=self.root)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM captures").fetchone()[0], 0)


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


class RegistrarCommandLineTests(RegistrarTestCase):
    def run_cli(self, *args: str) -> tuple[int, str, str]:
        self.connection.close()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = registrar_main(["--projects-root", str(self.root), "--database-path", str(self.db_path), *args])
        return code, out.getvalue(), err.getvalue()

    def count(self, table: str) -> int:
        connection = connect_database(self.db_path)
        try:
            return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            connection.close()

    def test_dry_run_prints_a_summary_and_writes_no_captures_or_frames(self) -> None:
        self.make_cr2_night()
        code, out, _ = self.run_cli("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would create 1", out)
        self.assertEqual((self.count("captures"), self.count("frames")), (0, 0))

    def test_a_real_run_then_a_rerun_is_idempotent_and_json_output_is_valid(self) -> None:
        self.make_cr2_night()
        self.assertEqual(self.run_cli()[0], 0)
        self.assertEqual((self.count("captures"), self.count("frames")), (1, 11))
        code, out, _ = self.run_cli("--json")
        report = json.loads(out)
        self.assertEqual((code, report["frames_created"], report["frames_unchanged"]), (0, 0, 11))
        self.assertEqual(self.count("frames"), 11)


class RegistrarDryRunClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.base = Path(self.temp_dir.name)
        self.root = self.base / "projects"
        self.capture = self.root / "M42" / "captures" / "Night1"
        self.db_path = self.base / "pre_s3.db"

        for relative, content in (
            ("lights/light_001.CR2", b"light"),
            ("darks/dark_001.CR2", b"dark"),
            ("notes.txt", b"ignored"),
        ):
            path = self.capture / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        connection = sqlite3.connect(self.db_path)
        try:
            initialize_schema(connection, baseline_path=DEFAULT_SCHEMA_PATH, migrations=MIGRATIONS[:1])
        finally:
            connection.close()

    def user_version(self) -> int:
        connection = sqlite3.connect(self.db_path)
        try:
            return get_user_version(connection)
        finally:
            connection.close()

    def table_exists(self, name: str) -> bool:
        connection = sqlite3.connect(self.db_path)
        try:
            return connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?;",
                (name,),
            ).fetchone() is not None
        finally:
            connection.close()

    def backup_files(self) -> list[Path]:
        return sorted(self.base.glob("pre_s3.db.v*.bak"))

    def run_dry_run(self) -> dict:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = registrar_main([
                "--projects-root", str(self.root),
                "--database-path", str(self.db_path),
                "--dry-run",
                "--json",
            ])
        self.assertEqual(code, 0)
        return json.loads(out.getvalue())

    def test_dry_run_on_pre_s3_unregistered_database_does_not_migrate_or_register_anything(self) -> None:
        before_tree = tree_state(self.root)
        before_version = self.user_version()
        before_backups = self.backup_files()
        self.assertEqual(before_version, 2)
        self.assertFalse(self.table_exists("captures"))

        report = self.run_dry_run()

        self.assertEqual(self.user_version(), before_version)
        self.assertFalse(self.table_exists("captures"))
        self.assertEqual(self.backup_files(), before_backups)
        self.assertEqual(tree_state(self.root), before_tree)
        self.assertIn("M42", report["filesystem_only_projects"])
        self.assertIn("M42/Night1", report["filesystem_only_captures"])
        self.assertGreater(report["frames_created"], 0)
        self.assertGreater(sum(report["ignored_suffixes"].values()), 0)

    def test_module_help_does_not_emit_runpy_warning(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "tsn_dss.engine.capture_registry", "--help"],
            cwd=Path(__file__).resolve().parent.parent,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--dry-run", result.stdout)
        self.assertNotIn("RuntimeWarning", result.stderr)
        self.assertNotIn("found in sys.modules", result.stderr)


if __name__ == "__main__":
    unittest.main()
