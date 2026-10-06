from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from tsn_dss.domain.models import AcquisitionPlan, AcquisitionSequence, Frame, Observation, Session, Target
from tsn_dss.engine.capture_registry import CaptureRegistrar
from tsn_dss.engine.project_registry import ProjectRegistry
from tsn_dss.engine.projects import ProjectStorage
from tsn_dss.engine.sqlite.captures import CaptureRepository
from tsn_dss.engine.sqlite.db import initialize_database
from tsn_dss.engine.sqlite.frames import FrameRepository
from tsn_dss.engine.sqlite.observation import ObservationRepository
from tsn_dss.engine.sqlite.planning import PlanningRepository, ValidationError
from tsn_dss.engine.sqlite.project_repository import ProjectRepository
from tsn_dss.engine.sqlite.sessions import SessionRepository


SESSION_ID = "session:contract-test"
STARTED = "2026-10-06T20:00:00+00:00"


class ObservationSessionContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.connection = initialize_database(self.root / "tsn.db")
        self.addCleanup(self.connection.close)
        self.planning = PlanningRepository(self.connection)
        self.sessions = SessionRepository(self.connection)
        self.observations = ObservationRepository(self.connection)
        self.frames = FrameRepository(self.connection)
        self._seed_session_and_target()

    def _seed_session_and_target(self) -> None:
        self.sessions.create_session(
            Session(
                id=SESSION_ID,
                started_at=STARTED,
                title="Contract test session",
            )
        )
        self.planning.create_target(
            Target(id="target:m42", catalog="M", catalog_id="42", name="Orion", ra_deg=83.8, dec_deg=-5.4)
        )

    def test_observation_without_session_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.observations.create_observation(Observation(id="obs:no-session", session_id="", target_id="target:m42"))

    def test_blank_and_nonexistent_session_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.observations.create_observation(Observation(id="obs:blank", session_id="", target_id="target:m42"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.observations.create_observation(
                Observation(id="obs:missing-session", session_id="session:missing", target_id="target:m42")
            )

    def test_observation_without_target_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.observations.create_observation(Observation(id="obs:no-target", session_id=SESSION_ID, target_id=""))
        with self.assertRaises(sqlite3.IntegrityError):
            self.observations.create_observation(
                Observation(id="obs:missing-target", session_id=SESSION_ID, target_id="target:missing")
            )

    def test_valid_session_is_persisted_returned_and_listed(self) -> None:
        created = self.observations.create_observation(
            Observation(id="obs:valid", session_id=SESSION_ID, target_id="target:m42", status="planned")
        )

        self.assertEqual(created.session_id, SESSION_ID)
        self.assertEqual(self.observations.get_observation("obs:valid").session_id, SESSION_ID)
        self.assertEqual([item.session_id for item in self.observations.list_observations()], [SESSION_ID])

    def test_update_and_status_transition_preserve_session_id(self) -> None:
        self.observations.create_observation(
            Observation(id="obs:lifecycle", session_id=SESSION_ID, target_id="target:m42", status="planned")
        )

        preparing = self.observations.set_observation_status("obs:lifecycle", "preparing")
        running = self.observations.set_observation_status("obs:lifecycle", "running", started_at=STARTED)
        paused = self.observations.set_observation_status("obs:lifecycle", "paused")

        self.assertEqual(preparing.session_id, SESSION_ID)
        self.assertEqual(running.session_id, SESSION_ID)
        self.assertEqual(paused.session_id, SESSION_ID)
        self.assertEqual(
            {
                "planned": "PLANNED",
                "preparing": "READY",
                "running": "ACTIVE",
                "paused": "ACTIVE implementation substate",
                "completed": "COMPLETED",
                "aborted": "ABORTED",
                "failed": "FAILED",
            }[paused.status],
            "ACTIVE implementation substate",
        )

    def test_optional_acquisition_plan_is_accepted_when_target_matches(self) -> None:
        plan = self.planning.save_acquisition_plan(
            AcquisitionPlan(
                id="plan:m42",
                target_id="target:m42",
                name="M42 plan",
                sequences=[AcquisitionSequence(sequence_order=0, frame_type="light", frame_count=1)],
            )
        )

        created = self.observations.create_observation(
            Observation(
                id="obs:plan",
                session_id=SESSION_ID,
                target_id="target:m42",
                acquisition_plan_id=plan.id,
            )
        )

        self.assertEqual(created.acquisition_plan_id, "plan:m42")

    def test_frame_link_remains_association_only(self) -> None:
        project = ProjectRepository(self.connection).register_project(dir_key="M42")
        capture = CaptureRepository(self.connection).register_capture(
            project_id=project.id,
            name="Night1",
            source_kind="legacy_registered",
        )
        self.observations.create_observation(
            Observation(id="obs:frame", session_id=SESSION_ID, target_id="target:m42")
        )

        linked = self.frames.create_frame(
            Frame(
                project_id=project.id,
                capture_id=capture.id,
                observation_id="obs:frame",
                rel_path="captures/Night1/light.fit",
                frame_type="light",
            )
        )
        unlinked = self.frames.create_frame(
            Frame(
                project_id=project.id,
                capture_id=capture.id,
                rel_path="captures/Night1/dark.fit",
                frame_type="dark",
            )
        )

        self.assertEqual(self.frames.get_frame(linked.id).observation_id, "obs:frame")
        self.assertIsNone(self.frames.get_frame(unlinked.id).observation_id)
        self.assertEqual(self.frames.get_frame(linked.id).capture_id, capture.id)

    def test_import_registration_fabricates_neither_observation_nor_session(self) -> None:
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
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM observations;").fetchone()[0], 0)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM sessions;").fetchone()[0], 0)

    def test_database_rejects_null_and_invalid_session_id(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO observations (id, session_id, target_id) VALUES ('obs:null', NULL, 'target:m42');"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO observations (id, session_id, target_id) VALUES ('obs:bad-fk', 'session:missing', 'target:m42');"
            )


if __name__ == "__main__":
    unittest.main()
