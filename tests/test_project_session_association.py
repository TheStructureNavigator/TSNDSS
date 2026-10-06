from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from tsn_dss.domain.models import Dataset, Frame, Observation, ProcessingRun, Session, Target
from tsn_dss.engine.capture_registry import CaptureRegistrar
from tsn_dss.engine.project_registry import ProjectRegistry
from tsn_dss.engine.projects import ProjectStorage
from tsn_dss.engine.sqlite.captures import CaptureRepository
from tsn_dss.engine.sqlite.datasets import DatasetRepository
from tsn_dss.engine.sqlite.db import DEFAULT_SCHEMA_PATH, foreign_key_violations, initialize_database
from tsn_dss.engine.sqlite.frames import FrameRepository
from tsn_dss.engine.sqlite.migrations import CURRENT_SCHEMA_VERSION, MIGRATIONS, get_user_version, initialize_schema
from tsn_dss.engine.sqlite.observation import ObservationRepository
from tsn_dss.engine.sqlite.planning import PlanningRepository, ValidationError
from tsn_dss.engine.sqlite.processing import ProcessingRunRepository
from tsn_dss.engine.sqlite.project_repository import ProjectRepository
from tsn_dss.engine.sqlite.project_sessions import ProjectSessionRepository
from tsn_dss.engine.sqlite.sessions import SessionRepository


STARTED = "2026-10-06T20:00:00+00:00"


class ProjectSessionAssociationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.connection = initialize_database(self.root / "tsn.db")
        self.addCleanup(self.connection.close)
        self.projects = ProjectRepository(self.connection)
        self.sessions = SessionRepository(self.connection)
        self.project_sessions = ProjectSessionRepository(self.connection)
        self.project = self.projects.register_project(dir_key="M42", target_label="M42")
        self.session = self.sessions.create_session(
            Session(id="session:test", started_at=STARTED, title="Test Session")
        )

    def _target(self, target_id: str = "target:m42") -> None:
        PlanningRepository(self.connection).create_target(
            Target(id=target_id, catalog="M", catalog_id="42", name="Orion", ra_deg=83.8, dec_deg=-5.4)
        )

    def _observation(self, observation_id: str = "obs:1") -> Observation:
        self._target()
        return ObservationRepository(self.connection).create_observation(
            Observation(id=observation_id, session_id=self.session.id, target_id="target:m42")
        )

    def test_project_and_session_may_exist_without_association(self) -> None:
        self.assertEqual(self.project_sessions.list_sessions_for_project(self.project.id), [])
        self.assertEqual(self.project_sessions.list_projects_for_session(self.session.id), [])
        self.assertFalse(self.project_sessions.has_association(self.project.id, self.session.id))

    def test_project_may_associate_with_session_and_is_queryable_from_both_sides(self) -> None:
        self.project_sessions.associate(self.project.id, self.session.id)

        self.assertTrue(self.project_sessions.has_association(self.project.id, self.session.id))
        self.assertEqual(self.project_sessions.list_sessions_for_project(self.project.id), [self.session])
        self.assertEqual(self.project_sessions.list_projects_for_session(self.session.id), [self.project])

    def test_one_project_may_associate_with_multiple_sessions(self) -> None:
        second = self.sessions.create_session(Session(id="session:second", started_at="2026-10-07T20:00:00+00:00"))

        self.project_sessions.associate(self.project.id, self.session.id)
        self.project_sessions.associate(self.project.id, second.id)

        self.assertEqual(
            [session.id for session in self.project_sessions.list_sessions_for_project(self.project.id)],
            [self.session.id, second.id],
        )

    def test_one_session_may_associate_with_multiple_projects(self) -> None:
        second = self.projects.register_project(dir_key="M31", target_label="M31")

        self.project_sessions.associate(self.project.id, self.session.id)
        self.project_sessions.associate(second.id, self.session.id)

        self.assertEqual(
            [project.id for project in self.project_sessions.list_projects_for_session(self.session.id)],
            [second.id, self.project.id],
        )

    def test_duplicate_association_is_idempotent_and_cannot_create_duplicate_rows(self) -> None:
        self.project_sessions.associate(self.project.id, self.session.id)
        self.project_sessions.associate(self.project.id, self.session.id)

        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM project_sessions;").fetchone()[0],
            1,
        )

    def test_association_requires_existing_nonblank_endpoints(self) -> None:
        with self.assertRaises(ValidationError):
            self.project_sessions.associate("", self.session.id)
        with self.assertRaises(ValidationError):
            self.project_sessions.associate(self.project.id, "")
        with self.assertRaises(sqlite3.IntegrityError):
            self.project_sessions.associate("project:missing", self.session.id)
        with self.assertRaises(sqlite3.IntegrityError):
            self.project_sessions.associate(self.project.id, "session:missing")

        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM project_sessions;").fetchone()[0], 0)

    def test_removing_association_removes_only_relation(self) -> None:
        observation = self._observation()
        self.project_sessions.associate(self.project.id, self.session.id)

        self.project_sessions.remove(self.project.id, self.session.id)

        self.assertFalse(self.project_sessions.has_association(self.project.id, self.session.id))
        self.assertEqual(self.projects.get_project(self.project.id), self.project)
        self.assertEqual(self.sessions.get_session(self.session.id), self.session)
        self.assertEqual(ObservationRepository(self.connection).get_observation(observation.id), observation)

    def test_project_deletion_cascades_only_association_rows_when_otherwise_legal(self) -> None:
        self.project_sessions.associate(self.project.id, self.session.id)

        self.projects.delete_project(self.project.id)

        self.assertIsNone(self.projects.get_project(self.project.id))
        self.assertEqual(self.sessions.get_session(self.session.id), self.session)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM project_sessions;").fetchone()[0], 0)

    def test_direct_session_deletion_cascades_only_association_rows_when_otherwise_legal(self) -> None:
        self.project_sessions.associate(self.project.id, self.session.id)

        self.connection.execute("DELETE FROM sessions WHERE id = ?;", (self.session.id,))
        self.connection.commit()

        self.assertEqual(self.projects.get_project(self.project.id), self.project)
        self.assertIsNone(self.sessions.get_session(self.session.id))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM project_sessions;").fetchone()[0], 0)

    def test_session_observations_remain_session_owned_after_project_association(self) -> None:
        observation = self._observation()
        other_project = self.projects.register_project(dir_key="M31", target_label="M31")

        self.project_sessions.associate(self.project.id, self.session.id)
        self.project_sessions.associate(other_project.id, self.session.id)

        stored = ObservationRepository(self.connection).get_observation(observation.id)
        self.assertEqual(stored.session_id, self.session.id)
        observation_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(observations);").fetchall()}
        self.assertNotIn("project_id", observation_columns)

    def test_project_capture_frame_provenance_remains_project_capture_owned_after_session_association(self) -> None:
        self._observation("obs:frame")
        capture = CaptureRepository(self.connection).register_capture(
            project_id=self.project.id,
            name="Night1",
            source_kind="legacy_registered",
        )
        frame = FrameRepository(self.connection).create_frame(
            Frame(
                project_id=self.project.id,
                capture_id=capture.id,
                observation_id="obs:frame",
                rel_path="captures/Night1/light.fit",
                frame_type="light",
            )
        )

        self.project_sessions.associate(self.project.id, self.session.id)

        stored_frame = FrameRepository(self.connection).get_frame(frame.id)
        self.assertEqual(stored_frame.project_id, self.project.id)
        self.assertEqual(stored_frame.capture_id, capture.id)
        self.assertEqual(stored_frame.observation_id, "obs:frame")
        self.assertNotIn("session_id", {row[1] for row in self.connection.execute("PRAGMA table_info(frames);").fetchall()})
        self.assertNotIn("session_id", {row[1] for row in self.connection.execute("PRAGMA table_info(captures);").fetchall()})

    def test_imported_capture_frames_fabricate_neither_session_nor_association(self) -> None:
        projects_root = self.root / "projects"
        storage = ProjectStorage(projects_root)
        layout = storage.ensure_project("ImportOnly")
        capture_dir = layout.captures_dir / "Night1"
        capture_dir.mkdir(parents=True)
        (capture_dir / "lights").mkdir()
        (capture_dir / "lights" / "light.CR2").write_bytes(b"raw-data")
        connection = initialize_database(self.root / "import.db")
        self.addCleanup(connection.close)
        ProjectRegistry(storage, connection).register_existing()

        report = CaptureRegistrar(storage, connection).register_all()

        self.assertEqual(report.frames_created, 1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM sessions;").fetchone()[0], 0)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM observations;").fetchone()[0], 0)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM project_sessions;").fetchone()[0], 0)

    def test_no_project_is_inferred_from_observation_and_no_session_from_capture_or_frame(self) -> None:
        self._observation()
        capture = CaptureRepository(self.connection).register_capture(
            project_id=self.project.id,
            name="Imported",
            source_kind="legacy_registered",
        )
        FrameRepository(self.connection).create_frame(
            Frame(
                project_id=self.project.id,
                capture_id=capture.id,
                rel_path="captures/Imported/dark.fit",
                frame_type="dark",
            )
        )

        self.assertEqual(self.project_sessions.list_sessions_for_project(self.project.id), [])
        self.assertEqual(self.project_sessions.list_projects_for_session(self.session.id), [])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM project_sessions;").fetchone()[0], 0)


class ProjectSessionMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def _connect(self, name: str) -> sqlite3.Connection:
        connection = sqlite3.connect(self.root / name)
        connection.execute("PRAGMA foreign_keys = ON;")
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self, connection: sqlite3.Connection, *, migrations=MIGRATIONS):
        return initialize_schema(connection, baseline_path=DEFAULT_SCHEMA_PATH, migrations=migrations)

    def _relation_schema(self, connection: sqlite3.Connection) -> tuple[list[tuple], list[tuple], list[tuple]]:
        columns = [tuple(row) for row in connection.execute("PRAGMA table_info(project_sessions);").fetchall()]
        fks = [tuple(row) for row in connection.execute("PRAGMA foreign_key_list(project_sessions);").fetchall()]
        indexes = [tuple(row) for row in connection.execute("PRAGMA index_list(project_sessions);").fetchall()]
        return columns, fks, indexes

    def test_fresh_current_database_has_exact_project_session_relation_schema(self) -> None:
        connection = self._connect("fresh.db")
        self.addCleanup(connection.close)
        result = self._initialize(connection)

        self.assertEqual(result.final_version, CURRENT_SCHEMA_VERSION)
        self.assertEqual(CURRENT_SCHEMA_VERSION, 7)
        columns, fks, indexes = self._relation_schema(connection)
        self.assertEqual([column[1] for column in columns], ["project_id", "session_id"])
        self.assertEqual([column[5] for column in columns], [1, 2])
        self.assertIn((0, 0, "sessions", "session_id", "id", "CASCADE", "CASCADE", "NONE"), fks)
        self.assertIn((1, 0, "projects", "project_id", "id", "CASCADE", "CASCADE", "NONE"), fks)
        self.assertIn("idx_project_sessions_session", {index[1] for index in indexes})
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM project_sessions;").fetchone()[0], 0)

    def test_v6_to_v7_preserves_existing_data_and_creates_no_associations(self) -> None:
        connection = self._connect("upgrade.db")
        result = self._initialize(connection, migrations=MIGRATIONS[:-1])
        self.assertEqual(result.final_version, 6)
        connection.executescript(
            """
            INSERT INTO targets (id, catalog, catalog_id, name, ra_deg, dec_deg)
            VALUES ('target:m42', 'M', '42', 'Orion Nebula', 83.8, -5.4);
            INSERT INTO projects (id, display_name, dir_key, target_label)
            VALUES ('project:m42', 'M42', 'M42', 'M42');
            INSERT INTO sessions (id, title, state, started_at)
            VALUES ('session:m42', 'M42 Session', 'planned', '2026-10-06T20:00:00+00:00');
            INSERT INTO observations (id, session_id, target_id, status)
            VALUES ('obs:m42', 'session:m42', 'target:m42', 'planned');
            INSERT INTO captures (id, project_id, name, rel_path, source_kind)
            VALUES ('capture:m42', 'project:m42', 'Night1', 'captures/Night1', 'legacy_registered');
            INSERT INTO frames (id, project_id, capture_id, observation_id, rel_path, frame_type, origin, metadata_json)
            VALUES (1, 'project:m42', 'capture:m42', 'obs:m42', 'captures/Night1/light.fit', 'light', 'raw', '{}');
            INSERT INTO datasets (id, target_id, name, status)
            VALUES ('dataset:m42', 'target:m42', 'Dataset', 'open');
            INSERT INTO dataset_observations (dataset_id, observation_id)
            VALUES ('dataset:m42', 'obs:m42');
            INSERT INTO dataset_frames (dataset_id, frame_id)
            VALUES ('dataset:m42', 1);
            INSERT INTO processing_runs (id, dataset_id, version_label, engine_name, pipeline_json, parameters_json, status)
            VALUES ('processing:m42', 'dataset:m42', 'v1', 'test-engine', '[]', '{}', 'planned');
            INSERT INTO mosaic_plans (
                id, project_slug, name, imaging_profile_id, imaging_profile_label,
                fov_width_deg, fov_height_deg, center_ra_deg, center_dec_deg,
                region_width_deg, region_height_deg, project_id
            ) VALUES (
                'mosaic:m42', 'M42', 'M42 Mosaic', 'profile:test', 'Profile',
                2.0, 1.0, 83.8, -5.4, 4.0, 2.0, 'project:m42'
            );
            """
        )
        connection.commit()
        connection.close()

        connection = self._connect("upgrade.db")
        self.addCleanup(connection.close)
        result = self._initialize(connection)

        self.assertEqual(result.applied_migrations, (7,))
        self.assertEqual(get_user_version(connection), 7)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM project_sessions;").fetchone()[0], 0)
        for table in (
            "projects", "sessions", "observations", "captures", "frames", "datasets",
            "dataset_observations", "dataset_frames", "processing_runs", "mosaic_plans",
        ):
            with self.subTest(table):
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table};").fetchone()[0], 1)
        self.assertEqual(foreign_key_violations(connection), [])

    def test_fresh_and_upgraded_v7_project_session_schemas_converge(self) -> None:
        fresh = self._connect("fresh.db")
        upgraded = self._connect("upgraded.db")
        self.addCleanup(fresh.close)
        self.addCleanup(upgraded.close)
        self._initialize(fresh)
        self._initialize(upgraded, migrations=MIGRATIONS[:-1])
        upgraded.close()
        upgraded = self._connect("upgraded.db")
        self.addCleanup(upgraded.close)
        self._initialize(upgraded)

        self.assertEqual(self._relation_schema(upgraded), self._relation_schema(fresh))


if __name__ == "__main__":
    unittest.main()
