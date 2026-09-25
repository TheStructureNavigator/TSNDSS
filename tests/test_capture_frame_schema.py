from __future__ import annotations

"""Stage S3: the v2 -> v3 migration, canonical Capture and Frame, and the acquisition-plan fix."""

import sqlite3
import unittest

try:
    from test_schema_migrations import (
        TempDirTestCase,
        describe_schema,
        populate_legacy_data,
        rows_for_columns,
        schema_diff,
        snapshot_rows,
    )
    from domain_seed import seed_project_and_capture
except ModuleNotFoundError:
    from tests.test_schema_migrations import (
        TempDirTestCase,
        describe_schema,
        populate_legacy_data,
        rows_for_columns,
        schema_diff,
        snapshot_rows,
    )
    from tests.domain_seed import seed_project_and_capture
from tsn_dss.domain.models import (
    AcquisitionPlan,
    AcquisitionSequence,
    Capture,
    Dataset,
    Frame,
    LocalHorizonPoint,
    Observation,
    Site,
    Target,
)
from tsn_dss.engine.sqlite.captures import CaptureRepository, new_capture_id
from tsn_dss.engine.sqlite.datasets import DatasetRepository
from tsn_dss.engine.sqlite.db import DEFAULT_SCHEMA_PATH
from tsn_dss.engine.sqlite.frames import ALLOWED_FRAME_TYPES, FrameRepository
from tsn_dss.engine.sqlite.migrations import (
    _MIGRATION_3_CAPTURES_FRAMES_SQL,
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    Migration,
    MigrationError,
    get_user_version,
    initialize_schema,
)
from tsn_dss.engine.sqlite.observation import ObservationRepository
from tsn_dss.engine.sqlite.planning import PlanningRepository, ValidationError
from tsn_dss.engine.sqlite.project_repository import ProjectInUseError, ProjectRepository

V2_ONLY = MIGRATIONS[:1]
SHA_A = "a" * 64
SHA_B = "b" * 64


# ---------------------------------------------------------------------------
# Migration v2 -> v3
# ---------------------------------------------------------------------------


class CaptureFrameMigrationTests(TempDirTestCase):
    def build_v2(self, *, legacy_frame: bool = False) -> None:
        """A realistic v2 database: Sites with horizon, mosaic plans linked to a project, and observation-domain rows."""
        connection = self.track(sqlite3.connect(self.db_path))
        connection.execute("PRAGMA foreign_keys = ON")
        initialize_schema(connection, baseline_path=DEFAULT_SCHEMA_PATH, migrations=V2_ONLY)
        connection.close()
        populate_legacy_data(self.db_path, "2026-09-01")
        raw = self.raw()
        raw.executescript(
            """
            INSERT INTO site_horizon_profile_points (site_id, azimuth_deg, min_altitude_deg)
            VALUES ('site:legacy', 0.0, 5.0), ('site:legacy', 90.0, 22.5);
            INSERT INTO projects (id, display_name, dir_key, target_label)
            VALUES ('project:one', 'Cygnus Loop', 'Cygnus Loop', 'NGC 6960');
            UPDATE mosaic_plans SET project_id = 'project:one' WHERE id = 'mosaic:legacy';
            INSERT INTO targets (id, catalog, catalog_id, name, ra_deg, dec_deg)
            VALUES ('target:m42', 'M', '42', 'Orion', 83.8, -5.4);
            INSERT INTO acquisition_plans (id, target_id, name) VALUES ('plan:1', 'target:m42', 'First light');
            INSERT INTO acquisition_sequences (plan_id, sequence_order, frame_type, exposure_s, frame_count)
            VALUES ('plan:1', 10, 'light', 30, 4);
            INSERT INTO observations (id, observation_number, target_id, acquisition_plan_id, status)
            VALUES ('obs:1', 1, 'target:m42', 'plan:1', 'completed');
            """
        )
        if legacy_frame:
            raw.execute(
                "INSERT INTO frames (observation_id, sequence_id, frame_type, file_path, exposure_s) "
                "VALUES ('obs:1', 1, 'light', 'data/m42/legacy.fit', 30)"
            )
        raw.commit()
        raw.close()

    def test_upgrade_reaches_version_three_and_preserves_existing_data(self) -> None:
        self.build_v2()
        before_connection = self.raw()
        self.assertEqual(get_user_version(before_connection), 2)
        before = snapshot_rows(before_connection)
        before_connection.close()

        connection, result = self.initialize()
        self.assertEqual((result.initial_version, result.final_version), (2, 3))
        self.assertEqual(result.applied_migrations, (3,))
        self.assertIsNotNone(result.backup_path)
        self.assertEqual(get_user_version(connection), 3)

        for table, (columns, rows) in before.items():
            if table == "frames":
                continue  # replaced (it was empty)
            self.assertEqual(rows_for_columns(connection, table, columns), rows, f"{table} changed")
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM frames").fetchone()[0], 0)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM captures").fetchone()[0], 0)
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM v_observation_summary").fetchone()[0], 1)

    def test_sites_horizon_mosaic_and_project_data_survive(self) -> None:
        self.build_v2()
        connection, _ = self.initialize()
        connection.row_factory = sqlite3.Row
        site = PlanningRepository(connection).get_site("site:legacy")
        self.assertEqual([(p.azimuth_deg, p.min_altitude_deg) for p in site.horizon_profile], [(0.0, 5.0), (90.0, 22.5)])
        project = ProjectRepository(connection).get_project("project:one")
        self.assertEqual((project.dir_key, project.target_label), ("Cygnus Loop", "NGC 6960"))
        self.assertEqual(connection.execute("SELECT project_id FROM mosaic_plans").fetchone()[0], "project:one")

    def test_migrated_database_equals_a_fresh_v3_database_semantically(self) -> None:
        self.build_v2()
        migrated, _ = self.initialize()
        migrated_schema = describe_schema(migrated)
        migrated.close()

        fresh = self.track(sqlite3.connect(self.dir / "fresh.db"))
        initialize_schema(fresh, baseline_path=DEFAULT_SCHEMA_PATH)
        self.assertEqual(get_user_version(fresh), CURRENT_SCHEMA_VERSION)
        self.assertEqual(schema_diff(migrated_schema, describe_schema(fresh)), [])
        self.assertEqual(migrated_schema, describe_schema(fresh))

    def test_a_failing_v3_migration_rolls_back_and_leaves_v2_untouched(self) -> None:
        self.build_v2()
        before_connection = self.raw()
        before_schema = describe_schema(before_connection)
        before_rows = snapshot_rows(before_connection)
        before_connection.close()

        failing = Migration(
            3,
            "v3 then fail",
            sql=_MIGRATION_3_CAPTURES_FRAMES_SQL + "INSERT INTO no_such_table VALUES (1);",
            foreign_keys_off=True,
            precondition=MIGRATIONS[1].precondition,
        )
        with self.assertRaises(MigrationError):
            self.initialize(migrations=[MIGRATIONS[0], failing])

        after = self.raw()
        self.assertEqual(get_user_version(after), 2)
        self.assertEqual(describe_schema(after), before_schema)
        self.assertNotIn("captures", describe_schema(after)["tables"])
        self.assertIn("file_path", describe_schema(after)["tables"]["frames"]["columns"])  # the old frames table survived
        self.assertEqual(snapshot_rows(after), before_rows)

    def test_non_empty_legacy_frames_make_the_upgrade_fail_clearly_and_change_nothing(self) -> None:
        self.build_v2(legacy_frame=True)
        before_connection = self.raw()
        before_rows = snapshot_rows(before_connection)
        before_schema = describe_schema(before_connection)
        before_connection.close()

        with self.assertRaises(MigrationError) as caught:
            self.initialize()

        message = str(caught.exception)
        self.assertIn("legacy frames table contains 1 row", message)
        self.assertIn("does not guess", message)
        self.assertIn("Nothing has been changed", message)
        after = self.raw()
        self.assertEqual(get_user_version(after), 2)
        self.assertEqual(describe_schema(after), before_schema)
        self.assertEqual(snapshot_rows(after), before_rows)  # the legacy frame is still there
        self.assertEqual(len(self.backups()), 1)  # the backup was taken before the refusal

    def test_the_precondition_runs_before_any_sql_and_can_veto_a_migration(self) -> None:
        self.build_v2()
        marker: list[str] = []

        def refuse(connection: sqlite3.Connection) -> None:
            marker.append("precondition")
            raise MigrationError("not today")

        migration = Migration(3, "vetoed", sql="CREATE TABLE should_not_exist (x INTEGER);", precondition=refuse)
        with self.assertRaises(MigrationError):
            self.initialize(migrations=[MIGRATIONS[0], migration])
        self.assertEqual(marker, ["precondition"])
        self.assertNotIn("should_not_exist", describe_schema(self.raw())["tables"])

    def test_empty_legacy_observation_domain_tables_are_compatible(self) -> None:
        # Observations, plans and datasets do not block the upgrade; only legacy frame rows would.
        self.build_v2()
        connection, result = self.initialize()
        self.assertEqual(result.final_version, 3)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 1)

    def test_schema_constraints_and_foreign_key_actions(self) -> None:
        schema = describe_schema(self.open_initialized())
        frames, captures = schema["tables"]["frames"], schema["tables"]["captures"]
        self.assertIn(("project_id", "projects", "id", "CASCADE", "RESTRICT"), frames["foreign_keys"])
        self.assertIn(("capture_id", "captures", "id", "CASCADE", "RESTRICT"), frames["foreign_keys"])
        self.assertIn(("observation_id", "observations", "id", "CASCADE", "SET NULL"), frames["foreign_keys"])
        self.assertIn(("sequence_id", "acquisition_sequences", "id", "CASCADE", "SET NULL"), frames["foreign_keys"])
        self.assertIn(("filter_id", "equipment", "id", "CASCADE", "SET NULL"), frames["foreign_keys"])
        self.assertIn(("project_id", "projects", "id", "CASCADE", "RESTRICT"), captures["foreign_keys"])
        self.assertIn(("u", True, ("project_id", "rel_path")), frames["indexes"])
        self.assertIn(("u", True, ("project_id", "name")), captures["indexes"])
        self.assertIn(("u", True, ("project_id", "rel_path")), captures["indexes"])
        self.assertFalse(frames["columns"]["observation_id"]["notnull"])  # calibration/legacy frames need none
        self.assertFalse(frames["columns"]["frame_type"]["notnull"])      # unknown stays NULL
        self.assertTrue(frames["columns"]["project_id"]["notnull"])
        self.assertTrue(frames["columns"]["capture_id"]["notnull"])
        # dataset_frames still points at frames(id), unchanged
        self.assertIn(("frame_id", "frames", "id", "CASCADE", "RESTRICT"), schema["tables"]["dataset_frames"]["foreign_keys"])

    def test_the_database_itself_refuses_to_change_a_recorded_hash(self) -> None:
        connection = self.open_initialized()
        project_id, capture_id = seed_project_and_capture(connection)
        connection.execute(
            "INSERT INTO frames (project_id, capture_id, rel_path, content_sha256, size_bytes, hashed_at) "
            "VALUES (?, ?, 'captures/Night1/a.fit', ?, 10, '2026-09-21T10:00:00+00:00')",
            (project_id, capture_id, SHA_A),
        )
        connection.commit()
        for column, value in (("content_sha256", SHA_B), ("size_bytes", 11), ("hashed_at", "2027-01-01")):
            with self.subTest(column), self.assertRaises(sqlite3.IntegrityError) as caught:
                connection.execute(f"UPDATE frames SET {column} = ? WHERE rel_path = 'captures/Night1/a.fit'", (value,))
            self.assertIn("immutable", str(caught.exception))
        connection.rollback()
        # other columns stay editable, and a frame without a hash can still receive one
        connection.execute("UPDATE frames SET accepted = 1 WHERE rel_path = 'captures/Night1/a.fit'")
        connection.execute(
            "INSERT INTO frames (project_id, capture_id, rel_path) VALUES (?, ?, 'captures/Night1/b.fit')",
            (project_id, capture_id),
        )
        connection.execute("UPDATE frames SET content_sha256 = ?, size_bytes = 3 WHERE rel_path = 'captures/Night1/b.fit'", (SHA_B,))
        connection.commit()

    def test_database_level_checks(self) -> None:
        connection = self.open_initialized()
        project_id, capture_id = seed_project_and_capture(connection)
        bad_rows = {
            "absolute path": "INSERT INTO frames (project_id, capture_id, rel_path) VALUES (?, ?, '/abs/x.fit')",
            "backslash": "INSERT INTO frames (project_id, capture_id, rel_path) VALUES (?, ?, 'captures\\x.fit')",
            "empty path": "INSERT INTO frames (project_id, capture_id, rel_path) VALUES (?, ?, '')",
            "bad frame type": "INSERT INTO frames (project_id, capture_id, rel_path, frame_type) VALUES (?, ?, 'a', 'science')",
            "bad origin": "INSERT INTO frames (project_id, capture_id, rel_path, origin) VALUES (?, ?, 'a', 'derived')",
            "short hash": "INSERT INTO frames (project_id, capture_id, rel_path, content_sha256) VALUES (?, ?, 'a', 'abc')",
            "uppercase hash": "INSERT INTO frames (project_id, capture_id, rel_path, content_sha256) VALUES (?, ?, 'a', '" + "A" * 64 + "')",
            "negative exposure": "INSERT INTO frames (project_id, capture_id, rel_path, exposure_s) VALUES (?, ?, 'a', -1)",
            "bad timestamp source": "INSERT INTO frames (project_id, capture_id, rel_path, captured_at_source) VALUES (?, ?, 'a', 'mtime')",
        }
        for label, sql in bad_rows.items():
            with self.subTest(label), self.assertRaises(sqlite3.IntegrityError):
                connection.execute(sql, (project_id, capture_id))
        connection.execute("INSERT INTO frames (project_id, capture_id, rel_path, exposure_s) VALUES (?, ?, 'zero', 0)",
                           (project_id, capture_id))  # a bias frame has a zero-second exposure
        connection.commit()


# ---------------------------------------------------------------------------
# Capture repository
# ---------------------------------------------------------------------------


class CaptureRepositoryTests(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.connection = self.open_initialized()
        self.projects = ProjectRepository(self.connection)
        self.captures = CaptureRepository(self.connection)
        self.project = self.projects.register_project(dir_key="M42")

    def test_identity_is_opaque_and_independent_of_name_and_path(self) -> None:
        first = self.captures.register_capture(project_id=self.project.id, name="OrionNebula", source_kind="legacy_registered")
        second = self.captures.register_capture(project_id=self.project.id, name="M31 \u2014 Andromeda", source_kind="folder_import")
        for capture in (first, second):
            self.assertRegex(capture.id, r"^capture:[0-9a-f]{12}$")
            self.assertNotIn(capture.name.lower(), capture.id.lower())
            self.assertEqual(capture.rel_path, f"captures/{capture.name}")
        self.assertNotEqual(first.id, second.id)
        self.assertRegex(new_capture_id(), r"^capture:[0-9a-f]{12}$")
        self.assertNotEqual(new_capture_id(), new_capture_id())

    def test_name_and_path_are_unique_per_project_but_not_across_projects(self) -> None:
        self.captures.register_capture(project_id=self.project.id, name="Night1", source_kind="legacy_registered")
        with self.assertRaises(ValidationError):
            self.captures.register_capture(project_id=self.project.id, name="Night1", source_kind="folder_import")
        other = self.projects.register_project(dir_key="M31")
        self.captures.register_capture(project_id=other.id, name="Night1", source_kind="legacy_registered")  # allowed
        self.assertEqual(len(self.captures.list_captures()), 2)
        self.assertEqual(len(self.captures.list_captures(project_id=self.project.id)), 1)

    def test_the_directory_layout_is_not_part_of_a_capture(self) -> None:
        # Lights-only and calibration-only are ordinary captures: nothing in the model mentions folders.
        for name in ("LightsOnly", "CalibrationOnly", "Mixed"):
            capture = self.captures.register_capture(project_id=self.project.id, name=name, source_kind="legacy_registered")
            self.assertIsNone(capture.imported_at)
        columns = describe_schema(self.connection)["tables"]["captures"]["columns"]
        self.assertFalse({"layout", "folders", "biases", "darks", "flats", "lights"} & set(columns))

    def test_source_metadata_round_trips(self) -> None:
        capture = self.captures.register_capture(
            project_id=self.project.id, name="Import1", source_kind="folder_import", source_label="Canon EOS",
            source_path="D:\\Astro\\Import1", import_mode="move", imported_at="2026-09-21T20:00:00+00:00",
            registrar_version="capture-registrar/1",
        )
        self.assertEqual(self.captures.get_capture(capture.id), capture)
        self.assertEqual(self.captures.get_capture_by_name(self.project.id, "Import1"), capture)
        self.assertTrue(capture.registered_at)

    def test_validation(self) -> None:
        cases = {
            "traversal name": dict(name="..", rel_path="captures/.."),
            "nested name": dict(name="a/b", rel_path="captures/a/b"),
            "backslash name": dict(name="a\\b", rel_path="captures/a\\b"),
            "rel_path must be captures/<name>": dict(name="Night1", rel_path="captures/Other"),
            "absolute rel_path": dict(name="Night1", rel_path="/captures/Night1"),
            "bad source kind": dict(name="Night1", rel_path="captures/Night1", source_kind="scanner"),
            "bad import mode": dict(name="Night1", rel_path="captures/Night1", import_mode="symlink"),
        }
        for label, kwargs in cases.items():
            with self.subTest(label), self.assertRaises(ValidationError):
                self.captures.create_capture(Capture(
                    id=new_capture_id(), project_id=self.project.id,
                    **({"source_kind": "legacy_registered"} | kwargs),
                ))
        for kind in ("legacy_registered", "folder_import", "device_import", "acquisition"):
            self.captures.register_capture(project_id=self.project.id, name=f"ok-{kind}", source_kind=kind)
        for mode in (None, "copy", "move", "acquired"):
            self.captures.register_capture(project_id=self.project.id, name=f"mode-{mode}", source_kind="folder_import", import_mode=mode)

    def test_unknown_project_is_refused(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.captures.register_capture(project_id="project:ghost", name="x", source_kind="legacy_registered")

    def test_a_project_with_captures_cannot_be_deleted(self) -> None:
        capture = self.captures.register_capture(project_id=self.project.id, name="Night1", source_kind="legacy_registered")
        with self.assertRaises(ProjectInUseError) as caught:
            self.projects.delete_project(self.project.id)
        self.assertEqual(caught.exception.capture_count, 1)
        self.assertIsNotNone(self.projects.get_project(self.project.id))
        self.assertIsNotNone(self.captures.get_capture(capture.id))


# ---------------------------------------------------------------------------
# Frame repository
# ---------------------------------------------------------------------------


class FrameRepositoryS3Tests(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.connection = self.open_initialized()
        self.project_id, self.capture_id = seed_project_and_capture(self.connection)
        self.frames = FrameRepository(self.connection)

    def frame(self, name: str = "a.fit", **overrides) -> Frame:
        return Frame(project_id=self.project_id, capture_id=self.capture_id, rel_path=f"captures/Night1/{name}", **overrides)

    def test_paths_are_project_relative_and_validated(self) -> None:
        created = self.frames.create_frame(self.frame("lights/a.fit"))
        self.assertEqual(created.rel_path, "captures/Night1/lights/a.fit")
        self.assertEqual(self.frames.get_frame_by_path(self.project_id, "captures/Night1/lights/a.fit"), created)
        bad_paths = ["", "/abs/x.fit", "C:/x.fit", "C:\\x.fit", "\\rooted", "captures\\Night1\\a.fit", "captures/../x.fit",
                     "captures/Night1/../../x.fit", "captures//Night1/a.fit", "captures/Night1/./a.fit", "captures/Night1/", "nul\x00.fit"]
        for path in bad_paths:
            with self.subTest(repr(path)), self.assertRaises(ValidationError):
                self.frames.create_frame(Frame(project_id=self.project_id, capture_id=self.capture_id, rel_path=path))

    def test_a_frame_must_lie_inside_its_capture_and_project(self) -> None:
        with self.assertRaises(ValidationError):
            self.frames.create_frame(Frame(project_id=self.project_id, capture_id=self.capture_id, rel_path="captures/Other/a.fit"))
        other_project = ProjectRepository(self.connection).register_project(dir_key="M31")
        with self.assertRaises(ValidationError):
            self.frames.create_frame(Frame(project_id=other_project.id, capture_id=self.capture_id, rel_path="captures/Night1/a.fit"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.frames.create_frame(Frame(project_id=self.project_id, capture_id="capture:ghost", rel_path="captures/Night1/a.fit"))

    def test_a_path_is_unique_within_a_project(self) -> None:
        self.frames.create_frame(self.frame("a.fit"))
        with self.assertRaises(ValidationError):
            self.frames.create_frame(self.frame("a.fit"))

    def test_every_frame_type_including_dark_flat_and_unknown(self) -> None:
        self.assertEqual(ALLOWED_FRAME_TYPES, {"light", "dark", "flat", "bias", "dark_flat"})
        for frame_type in (*sorted(ALLOWED_FRAME_TYPES), None):
            created = self.frames.create_frame(self.frame(f"type-{frame_type}.fit", frame_type=frame_type))
            self.assertEqual(created.frame_type, frame_type)
        with self.assertRaises(ValidationError):
            self.frames.create_frame(self.frame("bad.fit", frame_type="science"))

    def test_origin_vocabulary_and_defaults(self) -> None:
        self.assertEqual(self.frames.create_frame(self.frame("d.fit")).origin, "unknown")
        for origin in ("raw", "device_stack", "master", "unknown"):
            self.assertEqual(self.frames.create_frame(self.frame(f"o-{origin}.fit", origin=origin)).origin, origin)
        with self.assertRaises(ValidationError):
            self.frames.create_frame(self.frame("o-bad.fit", origin="derived"))
        self.assertEqual(self.frames.create_frame(self.frame("stack.fit", origin="device_stack", stack_count=120)).stack_count, 120)
        with self.assertRaises(ValidationError):
            self.frames.create_frame(self.frame("stack0.fit", stack_count=0))

    def test_calibration_frames_need_no_observation_and_no_target(self) -> None:
        for kind in ("dark", "flat", "bias", "dark_flat"):
            created = self.frames.create_frame(self.frame(f"{kind}.fit", frame_type=kind, exposure_s=0.0 if kind == "bias" else 5.0))
            self.assertIsNone(created.observation_id)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM targets").fetchone()[0], 0)  # no fake Target
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0], 0)
        self.assertEqual(len(self.frames.list_frames(capture_id=self.capture_id, frame_type="dark")), 1)

    def test_a_sequence_requires_an_observation(self) -> None:
        planning = PlanningRepository(self.connection)
        planning.create_target(Target(id="target:m42", catalog="M", catalog_id="42", name="Orion", ra_deg=83.8, dec_deg=-5.4))
        plan = planning.save_acquisition_plan(AcquisitionPlan(
            id="plan:1", target_id="target:m42", name="p",
            sequences=[AcquisitionSequence(sequence_order=1, frame_type="light", exposure_s=30, frame_count=3)]))
        with self.assertRaises(ValidationError):
            self.frames.create_frame(self.frame("s.fit", frame_type="light", sequence_id=plan.sequences[0].id))

    def test_a_recorded_hash_is_immutable(self) -> None:
        created = self.frames.create_frame(self.frame("h.fit", content_sha256=SHA_A, size_bytes=10, hashed_at="2026-09-21T10:00:00+00:00"))
        for change in (dict(content_sha256=SHA_B), dict(size_bytes=11), dict(hashed_at="2027-01-01T00:00:00+00:00")):
            with self.subTest(change), self.assertRaisesRegex(ValidationError, "immutable"):
                for name, value in change.items():
                    setattr(created, name, value)
                self.frames.update_frame(created)
            created = self.frames.get_frame(created.id)
        with self.assertRaisesRegex(ValidationError, "immutable"):
            self.frames.record_content_hash(created.id, content_sha256=SHA_B, size_bytes=10)
        self.assertEqual(self.frames.record_content_hash(created.id, content_sha256=SHA_A, size_bytes=10).content_sha256, SHA_A)  # same value: no-op
        created.accepted = True  # everything else stays editable
        self.assertTrue(self.frames.update_frame(created).accepted)

    def test_a_hash_can_be_recorded_once_on_a_frame_that_has_none(self) -> None:
        created = self.frames.create_frame(self.frame("nohash.fit"))
        recorded = self.frames.record_content_hash(created.id, content_sha256=SHA_A, size_bytes=42)
        self.assertEqual((recorded.content_sha256, recorded.size_bytes), (SHA_A, 42))
        self.assertTrue(recorded.hashed_at)
        with self.assertRaises(ValidationError):
            self.frames.record_content_hash(created.id, content_sha256="xyz", size_bytes=1)

    def test_hash_and_size_rules(self) -> None:
        for kwargs in (dict(content_sha256="abc", size_bytes=1), dict(content_sha256=SHA_A), dict(content_sha256="A" * 64, size_bytes=1),
                       dict(size_bytes=-1), dict(exposure_s=-0.1), dict(width_px=0), dict(binning_x=0), dict(iso=0),
                       dict(planned_ra_deg=360.0), dict(planned_dec_deg=91.0), dict(captured_at_source="mtime")):
            with self.subTest(kwargs), self.assertRaises(ValidationError):
                self.frames.create_frame(self.frame("bad.fit", **kwargs))
        self.assertEqual(self.frames.create_frame(self.frame("bias.fit", exposure_s=0.0)).exposure_s, 0.0)

    def test_duplicate_content_is_allowed_and_reportable(self) -> None:
        first = self.frames.create_frame(self.frame("copy1.fit", content_sha256=SHA_A, size_bytes=5))
        second = self.frames.create_frame(self.frame("copy2.fit", content_sha256=SHA_A, size_bytes=5))
        self.frames.create_frame(self.frame("other.fit", content_sha256=SHA_B, size_bytes=5))
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(self.frames.list_duplicate_hashes(self.project_id),
                         {SHA_A: ["captures/Night1/copy1.fit", "captures/Night1/copy2.fit"]})

    def test_full_metadata_round_trips(self) -> None:
        created = self.frames.create_frame(self.frame(
            "full.fit", frame_type="light", origin="raw", file_format="fits", size_bytes=100, content_sha256=SHA_A,
            hashed_at="2026-09-21T10:00:00+00:00", width_px=3008, height_px=3008, instrument_name="ZWO ASI533MC Pro",
            captured_at="2026-09-21T01:02:03.500Z", captured_at_source="fits_header", exposure_s=10.0, gain=100.0,
            offset_value=50.0, iso=800, binning_x=1, binning_y=1, camera_temp_c=-10.5, filter_name="L-eXtreme",
            mount_ra_deg=83.8, mount_dec_deg=-5.4, pointing_source_kind="mosaic_panel", pointing_source_id="mosaic_panel:x",
            pointing_label="P01", planned_ra_deg=83.0, planned_dec_deg=-5.0,
            metadata={"fits": {"cards": {"DATE-OBS": "2026-09-21T01:02:03.500"}}, "registration": {"frame_type_basis": "directory:lights"}},
        ))
        self.assertEqual(self.frames.get_frame(created.id), created)
        self.assertEqual(created.metadata["registration"]["frame_type_basis"], "directory:lights")

    def test_datasets_accept_frames_that_have_no_observation(self) -> None:
        # The minimum downstream adaptation: a calibration/legacy frame simply skips the target checks.
        PlanningRepository(self.connection).create_target(
            Target(id="target:m42", catalog="M", catalog_id="42", name="Orion", ra_deg=83.8, dec_deg=-5.4))
        dark = self.frames.create_frame(self.frame("dark.fit", frame_type="dark", exposure_s=30.0))
        dataset = DatasetRepository(self.connection).create_dataset(
            Dataset(id="dataset:1", target_id="target:m42", name="With dark", frame_ids=[dark.id]))
        self.assertEqual(dataset.frame_ids, [dark.id])

    def test_a_project_with_frames_cannot_be_deleted(self) -> None:
        self.frames.create_frame(self.frame("a.fit"))
        with self.assertRaises(ProjectInUseError) as caught:
            ProjectRepository(self.connection).delete_project(self.project_id)
        self.assertEqual((caught.exception.capture_count, caught.exception.frame_count), (1, 1))

    def test_deleting_an_observation_keeps_its_frames(self) -> None:
        planning = PlanningRepository(self.connection)
        planning.create_target(Target(id="target:m42", catalog="M", catalog_id="42", name="Orion", ra_deg=83.8, dec_deg=-5.4))
        ObservationRepository(self.connection).create_observation(Observation(id="obs:1", target_id="target:m42"))
        frame = self.frames.create_frame(self.frame("lit.fit", frame_type="light", observation_id="obs:1"))
        ObservationRepository(self.connection).delete_observation("obs:1")
        self.assertIsNone(self.frames.get_frame(frame.id).observation_id)  # SET NULL, the frame record survives


# ---------------------------------------------------------------------------
# AcquisitionPlan resave no longer erases Frame provenance
# ---------------------------------------------------------------------------


class AcquisitionPlanResaveTests(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.connection = self.open_initialized()
        self.project_id, self.capture_id = seed_project_and_capture(self.connection)
        self.planning = PlanningRepository(self.connection)
        self.frames = FrameRepository(self.connection)
        self.planning.create_target(Target(id="target:m42", catalog="M", catalog_id="42", name="Orion", ra_deg=83.8, dec_deg=-5.4))
        self.plan = self.planning.save_acquisition_plan(AcquisitionPlan(
            id="plan:1", target_id="target:m42", name="First light",
            sequences=[
                AcquisitionSequence(sequence_order=10, frame_type="light", exposure_s=30, frame_count=4, name="lights"),
                AcquisitionSequence(sequence_order=20, frame_type="dark", exposure_s=30, frame_count=4, name="darks"),
            ]))
        self.light_id, self.dark_id = (sequence.id for sequence in self.plan.sequences)
        ObservationRepository(self.connection).create_observation(
            Observation(id="obs:1", target_id="target:m42", acquisition_plan_id="plan:1"))
        self.frame = self.frames.create_frame(Frame(
            project_id=self.project_id, capture_id=self.capture_id, rel_path="captures/Night1/l.fit",
            observation_id="obs:1", sequence_id=self.light_id, frame_type="light"))

    def sequences(self) -> list[tuple[int, int, str | None]]:
        return [(s.id, s.sequence_order, s.name) for s in self.planning.get_acquisition_plan("plan:1").sequences]

    def test_resaving_the_same_plan_keeps_sequence_ids_and_frame_links(self) -> None:
        # The original bug: this used to delete and re-insert the sequences and null frames.sequence_id.
        resaved = self.planning.save_acquisition_plan(AcquisitionPlan(
            id="plan:1", target_id="target:m42", name="First light (renamed)",
            sequences=[
                AcquisitionSequence(sequence_order=10, frame_type="light", exposure_s=30, frame_count=4, name="lights"),
                AcquisitionSequence(sequence_order=20, frame_type="dark", exposure_s=30, frame_count=4, name="darks"),
            ]))
        self.assertEqual([s.id for s in resaved.sequences], [self.light_id, self.dark_id])
        self.assertEqual(self.frames.get_frame(self.frame.id).sequence_id, self.light_id)

    def test_editing_a_sequence_in_place_keeps_its_id(self) -> None:
        plan = self.planning.get_acquisition_plan("plan:1")
        plan.sequences[0].exposure_s = 60
        plan.sequences[0].frame_count = 10
        plan.description = "edited"
        self.planning.save_acquisition_plan(plan)
        saved = self.planning.get_acquisition_plan("plan:1")
        self.assertEqual((saved.sequences[0].id, saved.sequences[0].exposure_s, saved.sequences[0].frame_count), (self.light_id, 60, 10))
        self.assertEqual(self.frames.get_frame(self.frame.id).sequence_id, self.light_id)

    def test_reordering_by_id_keeps_ids_and_never_collides(self) -> None:
        plan = self.planning.get_acquisition_plan("plan:1")
        plan.sequences[0].sequence_order, plan.sequences[1].sequence_order = 20, 10  # swap
        self.planning.save_acquisition_plan(plan)
        self.assertEqual(self.sequences(), [(self.dark_id, 10, "darks"), (self.light_id, 20, "lights")])
        self.assertEqual(self.frames.get_frame(self.frame.id).sequence_id, self.light_id)

    def test_a_new_sequence_gets_a_new_id_and_others_are_untouched(self) -> None:
        plan = self.planning.get_acquisition_plan("plan:1")
        plan.sequences.append(AcquisitionSequence(sequence_order=30, frame_type="flat", exposure_s=1, frame_count=5, name="flats"))
        self.planning.save_acquisition_plan(plan)
        ids = [item[0] for item in self.sequences()]
        self.assertEqual(ids[:2], [self.light_id, self.dark_id])
        self.assertNotIn(ids[2], (self.light_id, self.dark_id))

    def test_removing_an_unreferenced_sequence_is_allowed(self) -> None:
        plan = self.planning.get_acquisition_plan("plan:1")
        plan.sequences = [plan.sequences[0]]
        self.planning.save_acquisition_plan(plan)
        self.assertEqual(self.sequences(), [(self.light_id, 10, "lights")])

    def test_removing_a_sequence_that_frames_use_is_refused_and_changes_nothing(self) -> None:
        plan = self.planning.get_acquisition_plan("plan:1")
        plan.sequences = [plan.sequences[1]]  # drop the "lights" sequence the frame references
        plan.name = "should not be saved"
        before = self.sequences()
        with self.assertRaisesRegex(ValidationError, "frame"):
            self.planning.save_acquisition_plan(plan)
        self.assertEqual(self.sequences(), before)
        self.assertEqual(self.planning.get_acquisition_plan("plan:1").name, "First light")  # the whole save rolled back
        self.assertEqual(self.frames.get_frame(self.frame.id).sequence_id, self.light_id)

    def test_changing_the_frame_type_of_a_used_sequence_is_refused(self) -> None:
        plan = self.planning.get_acquisition_plan("plan:1")
        plan.sequences[0].frame_type = "dark"
        with self.assertRaisesRegex(ValidationError, "frame_type"):
            self.planning.save_acquisition_plan(plan)
        self.assertEqual(self.frames.get_frame(self.frame.id).sequence_id, self.light_id)

    def test_id_validation(self) -> None:
        plan = self.planning.get_acquisition_plan("plan:1")
        plan.sequences[0].id = 99999
        with self.assertRaises(ValidationError):
            self.planning.save_acquisition_plan(plan)
        plan = self.planning.get_acquisition_plan("plan:1")
        plan.sequences[1].id = plan.sequences[0].id
        with self.assertRaises(ValidationError):
            self.planning.save_acquisition_plan(plan)

    def test_order_matching_for_sequences_without_ids(self) -> None:
        resaved = self.planning.save_acquisition_plan(AcquisitionPlan(
            id="plan:1", target_id="target:m42", name="x",
            sequences=[AcquisitionSequence(sequence_order=10, frame_type="light", exposure_s=45, frame_count=2)]))
        self.assertEqual(resaved.sequences[0].id, self.light_id)  # matched by sequence_order


if __name__ == "__main__":
    unittest.main()
