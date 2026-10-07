from __future__ import annotations

import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tsn_dss.domain.models import Frame, Observation, Session, Site, Target
from tsn_dss.engine.sqlite.captures import CaptureRepository
from tsn_dss.engine.sqlite.db import initialize_database
from tsn_dss.engine.sqlite.frames import FrameRepository
from tsn_dss.engine.sqlite.observation import ObservationRepository
from tsn_dss.engine.sqlite.planning import PlanningRepository, ValidationError
from tsn_dss.engine.sqlite.project_repository import ProjectRepository
from tsn_dss.engine.sqlite.sessions import SessionRepository, new_session_id


STARTED = "2026-10-05T20:00:00+00:00"
ENDED = "2026-10-05T23:00:00+00:00"


TEST_SESSION_ID = "session:test-observation"

class SessionRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.connection = initialize_database(Path(self.temp_dir.name) / "tsn.db")
        self.addCleanup(self.connection.close)
        self.sessions = SessionRepository(self.connection)

    def test_new_session_id_is_opaque_session_id(self) -> None:
        self.assertRegex(new_session_id(), r"^session:[0-9a-f]{12}$")

    def test_create_get_and_list_standalone_session(self) -> None:
        session = self.sessions.register_session(
            started_at=STARTED,
            title="Field night",
            operator_id="operator:ada",
            notes="first light",
        )

        self.assertTrue(re.match(r"^session:[0-9a-f]{12}$", session.id))
        self.assertEqual(session.state, "planned")
        self.assertEqual(session.started_at, STARTED)
        self.assertEqual(self.sessions.get_session(session.id), session)
        self.assertEqual(self.sessions.list_sessions(), [session])
        self.assertEqual(self.sessions.list_sessions(state="planned"), [session])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM observations;").fetchone()[0], 0)

    def test_site_reference_must_be_existing_site_when_present(self) -> None:
        planning = PlanningRepository(self.connection)
        planning.create_site(Site(id="site:field", name="Field"))

        session = self.sessions.register_session(started_at=STARTED, site_id="site:field")
        self.assertEqual(session.site_id, "site:field")

        with self.assertRaises(ValidationError):
            self.sessions.register_session(started_at=STARTED, site_id="site:missing")

    def test_duplicate_session_id_is_rejected(self) -> None:
        session = Session(id="session:fixed", started_at=STARTED)
        self.sessions.create_session(session)

        with self.assertRaises(ValidationError):
            self.sessions.create_session(session)

    def test_session_transition_lifecycle_preserves_operator(self) -> None:
        session = self.sessions.register_session(started_at=STARTED)
        session = self.sessions.transition_session(session.id, "preparing")
        self.assertEqual(session.state, "preparing")

        with self.assertRaises(ValidationError):
            self.sessions.transition_session(session.id, "active")

        session = self.sessions.transition_session(session.id, "active", operator_id="operator:ada")
        self.assertEqual(session.state, "active")
        self.assertEqual(session.operator_id, "operator:ada")

        with self.assertRaises(ValidationError):
            self.sessions.update_session_metadata(session.id, operator_id=None)
        with self.assertRaises(ValidationError):
            self.sessions.update_session_metadata(session.id, operator_id="operator:grace")

        session = self.sessions.transition_session(session.id, "closing")
        self.assertEqual(session.operator_id, "operator:ada")
        self.assertIsNone(session.ended_at)
        self.assertIsNone(session.final_state)

        session = self.sessions.transition_session(session.id, "completed", ended_at=ENDED)
        self.assertEqual(session.state, "completed")
        self.assertEqual(session.final_state, "completed")
        self.assertEqual(session.ended_at, ENDED)
        self.assertEqual(session.operator_id, "operator:ada")

        with self.assertRaises(ValidationError):
            self.sessions.transition_session(session.id, "aborted")

    def test_abort_sets_terminal_fields_consistently(self) -> None:
        session = self.sessions.register_session(started_at=STARTED, operator_id="operator:ada")
        self.sessions.transition_session(session.id, "preparing")
        self.sessions.transition_session(session.id, "active")

        aborted = self.sessions.transition_session(session.id, "aborted", ended_at=ENDED)

        self.assertEqual(aborted.state, "aborted")
        self.assertEqual(aborted.final_state, "aborted")
        self.assertEqual(aborted.ended_at, ENDED)

    def test_invalid_lifecycle_and_payload_shapes_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.sessions.create_session(Session(id="session:bad", started_at=STARTED, state="active"))
        with self.assertRaises(ValidationError):
            self.sessions.create_session(
                Session(
                    id="session:bad",
                    started_at=STARTED,
                    state="completed",
                    operator_id="operator:ada",
                    ended_at=ENDED,
                    final_state="aborted",
                )
            )
        with self.assertRaises(ValidationError):
            self.sessions.create_session(
                Session(
                    id="session:bad",
                    started_at=STARTED,
                    state="completed",
                    operator_id="operator:ada",
                    ended_at="2026-10-05T19:00:00+00:00",
                    final_state="completed",
                )
            )

        session = self.sessions.register_session(started_at=STARTED)
        with self.assertRaises(ValidationError):
            self.sessions.transition_session(session.id, "active", operator_id="operator:ada")

    def test_sessions_do_not_own_projects_captures_frames_or_observations(self) -> None:
        project = ProjectRepository(self.connection).register_project(dir_key="orion", target_label="M42")
        capture = CaptureRepository(self.connection).register_capture(
            project_id=project.id,
            name="lights",
            source_kind="folder_import",
            source_label="legacy folder",
        )
        frame = FrameRepository(self.connection).create_frame(
            Frame(
                project_id=project.id,
                capture_id=capture.id,
                rel_path="captures/lights/light001.fit",
                frame_type="light",
            )
        )
        PlanningRepository(self.connection).create_target(
            Target(id="target:m42", catalog="M", catalog_id="42", name="Orion Nebula", ra_deg=83.8, dec_deg=-5.4)
        )
        session = self.sessions.register_session(started_at=STARTED)
        ObservationRepository(self.connection).create_observation(
            Observation(session_id=session.id, id="obs:1", target_id="target:m42", status="planned")
        )


        self.assertEqual(self.sessions.get_session(session.id), session)
        self.assertEqual(FrameRepository(self.connection).get_frame(frame.id).capture_id, capture.id)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM observations WHERE id = 'obs:1';").fetchone()[0],
            1,
        )
        session_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(sessions);").fetchall()}
        project_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(projects);").fetchall()}
        capture_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(captures);").fetchall()}
        frame_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(frames);").fetchall()}
        observation_columns = {row[1] for row in self.connection.execute("PRAGMA table_info(observations);").fetchall()}
        project_session_columns = [
            row[1] for row in self.connection.execute("PRAGMA table_info(project_sessions);").fetchall()
        ]

        self.assertNotIn("project_id", session_columns)
        self.assertNotIn("session_id", project_columns)
        self.assertNotIn("session_id", capture_columns)
        self.assertNotIn("session_id", frame_columns)
        self.assertIn("session_id", observation_columns)
        self.assertEqual(project_session_columns, ["project_id", "session_id"])
        session_plan_columns = [
            row[1] for row in self.connection.execute("PRAGMA table_info(session_plans);").fetchall()
        ]
        session_plan_item_columns = [
            row[1] for row in self.connection.execute("PRAGMA table_info(session_plan_items);").fetchall()
        ]
        self.assertEqual(session_plan_columns, ["session_id"])
        self.assertEqual(
            session_plan_item_columns,
            ["id", "session_id", "item_order", "target_id", "acquisition_plan_id", "mosaic_panel_id"],
        )
        for table in ("session_members", "session_context"):
            self.assertIsNone(
                self.connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?;",
                    (table,),
                ).fetchone()
            )
        self.assertIsNotNone(
            self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'session_events';"
            ).fetchone()
        )

    def test_raw_database_constraints_match_repository_contract(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO sessions (id, state, started_at)
                VALUES ('session:raw-active', 'active', ?);
                """,
                (STARTED,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO sessions (id, state, started_at, operator_id, ended_at, final_state)
                VALUES ('session:raw-terminal', 'completed', ?, 'operator:ada', ?, 'aborted');
                """,
                (STARTED, ENDED),
            )


if __name__ == "__main__":
    unittest.main()
