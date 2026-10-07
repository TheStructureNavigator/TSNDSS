from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from tsn_dss.domain.models import (
    AcquisitionPlan,
    AcquisitionSequence,
    MosaicPlan,
    Observation,
    Project,
    Session,
    SessionPlan,
    SessionPlanItem,
    Target,
)
from tsn_dss.engine.sqlite.db import DEFAULT_SCHEMA_PATH, foreign_key_violations, initialize_database
from tsn_dss.engine.sqlite.migrations import CURRENT_SCHEMA_VERSION, MIGRATIONS, get_user_version, initialize_schema
from tsn_dss.engine.sqlite.mosaics import MosaicRepository
from tsn_dss.engine.sqlite.observation import ObservationRepository
from tsn_dss.engine.sqlite.planning import PlanningRepository, ValidationError
from tsn_dss.engine.sqlite.project_repository import ProjectRepository
from tsn_dss.engine.sqlite.project_sessions import ProjectSessionRepository
from tsn_dss.engine.sqlite.session_plans import SessionPlanRepository
from tsn_dss.engine.sqlite.sessions import SessionRepository

STARTED = "2026-10-06T20:00:00+00:00"


class SessionPlanRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "session_plans.db"
        self.connection = initialize_database(self.db_path)
        self.sessions = SessionRepository(self.connection)
        self.plans = SessionPlanRepository(self.connection)
        self.planning = PlanningRepository(self.connection)
        self.observations = ObservationRepository(self.connection)
        self.mosaics = MosaicRepository(self.connection)
        self.projects = ProjectRepository(self.connection)
        self.project_sessions = ProjectSessionRepository(self.connection)
        self.session = self.sessions.create_session(Session(id="session:test", started_at=STARTED))
        self.target = self.planning.create_target(
            Target(id="target:m42", catalog="M", catalog_id="42", name="Orion Nebula", ra_deg=83.8, dec_deg=-5.4)
        )
        self.other_target = self.planning.create_target(
            Target(id="target:m31", catalog="M", catalog_id="31", name="Andromeda", ra_deg=10.7, dec_deg=41.3)
        )
        self.acquisition_plan = self.planning.save_acquisition_plan(
            AcquisitionPlan(
                id="plan:m42",
                target_id=self.target.id,
                name="M42 acquisition",
                sequences=[AcquisitionSequence(sequence_order=10, frame_type="light", frame_count=3, exposure_s=30.0)],
            )
        )
        self.other_acquisition_plan = self.planning.save_acquisition_plan(
            AcquisitionPlan(id="plan:m31", target_id=self.other_target.id, name="M31 acquisition")
        )
        self.mosaic_plan = self.mosaics.save_mosaic_plan(
            MosaicPlan(
                id="mosaic:m42",
                project_slug="m42_project",
                name="M42 Mosaic",
                imaging_profile_id="profile:test",
                imaging_profile_label="Profile",
                fov_width_deg=2.0,
                fov_height_deg=1.0,
                center_ra_deg=83.8,
                center_dec_deg=-5.4,
                region_width_deg=2.0,
                region_height_deg=1.0,
            )
        )
        self.mosaic_panel = self.mosaics.generate_panels(self.mosaic_plan.id)[0]

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def test_session_valid_with_zero_session_plan(self) -> None:
        self.assertIsNotNone(self.sessions.get_session(self.session.id))
        self.assertIsNone(self.plans.get_plan_for_session(self.session.id))

    def test_save_and_retrieve_session_plan_for_existing_session(self) -> None:
        saved = self.plans.save_plan(SessionPlan(session_id=self.session.id))
        reloaded = self.plans.get_plan_for_session(self.session.id)

        self.assertEqual(saved.session_id, self.session.id)
        assert reloaded is not None
        self.assertEqual(reloaded.session_id, self.session.id)
        self.assertEqual(reloaded.items, [])

    def test_exactly_zero_or_one_plan_per_session(self) -> None:
        self.plans.save_plan(SessionPlan(session_id=self.session.id, items=[SessionPlanItem(target_id=self.target.id)]))
        self.plans.save_plan(
            SessionPlan(session_id=self.session.id, items=[SessionPlanItem(acquisition_plan_id=self.acquisition_plan.id)])
        )

        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM session_plans;").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM session_plan_items;").fetchone()[0], 1)
        reloaded = self.plans.get_plan_for_session(self.session.id)
        assert reloaded is not None
        self.assertEqual(reloaded.items[0].acquisition_plan_id, self.acquisition_plan.id)

    def test_multiple_planned_activities_persist_with_supported_anchors(self) -> None:
        saved = self.plans.save_plan(
            SessionPlan(
                session_id=self.session.id,
                items=[
                    SessionPlanItem(item_order=None, target_id=self.target.id),
                    SessionPlanItem(item_order=10, acquisition_plan_id=self.acquisition_plan.id),
                    SessionPlanItem(item_order=20, mosaic_panel_id=self.mosaic_panel.id),
                    SessionPlanItem(
                        item_order=30,
                        target_id=self.target.id,
                        acquisition_plan_id=self.acquisition_plan.id,
                        mosaic_panel_id=self.mosaic_panel.id,
                    ),
                ],
            )
        )

        self.assertEqual(len(saved.items), 4)
        self.assertTrue(all(item.id is not None for item in saved.items))
        self.assertEqual(saved.items[0].item_order, 10)
        self.assertEqual(saved.items[1].item_order, 20)
        self.assertEqual(saved.items[2].item_order, 30)
        self.assertIsNone(saved.items[3].item_order)

    def test_item_order_accepts_zero_positive_null_and_duplicate_values(self) -> None:
        saved = self.plans.save_plan(
            SessionPlan(
                session_id=self.session.id,
                items=[
                    SessionPlanItem(item_order=0, target_id=self.target.id),
                    SessionPlanItem(item_order=10, target_id=self.target.id),
                    SessionPlanItem(item_order=10, acquisition_plan_id=self.acquisition_plan.id),
                    SessionPlanItem(item_order=None, mosaic_panel_id=self.mosaic_panel.id),
                ],
            )
        )

        self.assertEqual([item.item_order for item in saved.items], [0, 10, 10, None])

    def test_negative_item_order_is_rejected_without_partial_write(self) -> None:
        with self.assertRaisesRegex(ValidationError, "item_order"):
            self.plans.save_plan(
                SessionPlan(session_id=self.session.id, items=[SessionPlanItem(item_order=-1, target_id=self.target.id)])
            )

        self.assertIsNone(self.plans.get_plan_for_session(self.session.id))

    def test_item_without_anchor_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "canonical planning anchor"):
            self.plans.save_plan(SessionPlan(session_id=self.session.id, items=[SessionPlanItem()]))

    def test_nonexistent_references_are_rejected(self) -> None:
        cases = [
            SessionPlan(session_id="session:missing"),
            SessionPlan(session_id=self.session.id, items=[SessionPlanItem(target_id="target:missing")]),
            SessionPlan(session_id=self.session.id, items=[SessionPlanItem(acquisition_plan_id="plan:missing")]),
            SessionPlan(session_id=self.session.id, items=[SessionPlanItem(mosaic_panel_id="panel:missing")]),
        ]
        for plan in cases:
            with self.subTest(plan=plan):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.plans.save_plan(plan)

    def test_target_and_matching_acquisition_plan_are_accepted(self) -> None:
        saved = self.plans.save_plan(
            SessionPlan(
                session_id=self.session.id,
                items=[SessionPlanItem(target_id=self.target.id, acquisition_plan_id=self.acquisition_plan.id)],
            )
        )

        self.assertEqual(saved.items[0].target_id, self.target.id)
        self.assertEqual(saved.items[0].acquisition_plan_id, self.acquisition_plan.id)

    def test_target_and_mismatching_acquisition_plan_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValidationError, "must match"):
            self.plans.save_plan(
                SessionPlan(
                    session_id=self.session.id,
                    items=[SessionPlanItem(target_id=self.target.id, acquisition_plan_id=self.other_acquisition_plan.id)],
                )
            )

    def test_planning_lifecycle_creates_no_observation(self) -> None:
        self.plans.save_plan(SessionPlan(session_id=self.session.id, items=[SessionPlanItem(target_id=self.target.id)]))
        self.plans.save_plan(
            SessionPlan(session_id=self.session.id, items=[SessionPlanItem(acquisition_plan_id=self.acquisition_plan.id)])
        )
        self.plans.delete_plan_for_session(self.session.id)

        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM observations;").fetchone()[0], 0)

    def test_observation_may_exist_for_target_absent_from_plan(self) -> None:
        self.plans.save_plan(SessionPlan(session_id=self.session.id, items=[SessionPlanItem(target_id=self.target.id)]))
        self.observations.create_observation(
            Observation(id="obs:m31", session_id=self.session.id, target_id=self.other_target.id, status="planned")
        )

        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM observations;").fetchone()[0], 1)
        reloaded = self.plans.get_plan_for_session(self.session.id)
        assert reloaded is not None
        self.assertEqual(reloaded.items[0].target_id, self.target.id)

    def test_planned_item_may_exist_with_zero_observations(self) -> None:
        self.plans.save_plan(SessionPlan(session_id=self.session.id, items=[SessionPlanItem(target_id=self.target.id)]))

        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM observations;").fetchone()[0], 0)

    def test_delete_plan_removes_only_owned_items_and_leaves_independent_aggregates(self) -> None:
        project = self.projects.create_project(Project(id="project:m42", display_name="M42", dir_key="M42"))
        self.project_sessions.associate(project.id, self.session.id)
        self.observations.create_observation(
            Observation(id="obs:m42", session_id=self.session.id, target_id=self.target.id, status="planned")
        )
        self.plans.save_plan(
            SessionPlan(
                session_id=self.session.id,
                items=[
                    SessionPlanItem(target_id=self.target.id),
                    SessionPlanItem(acquisition_plan_id=self.acquisition_plan.id),
                    SessionPlanItem(mosaic_panel_id=self.mosaic_panel.id),
                ],
            )
        )

        self.plans.delete_plan_for_session(self.session.id)

        self.assertIsNone(self.plans.get_plan_for_session(self.session.id))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM session_plan_items;").fetchone()[0], 0)
        self.assertIsNotNone(self.sessions.get_session(self.session.id))
        self.assertIsNotNone(self.planning.get_target(self.target.id))
        self.assertIsNotNone(self.planning.get_acquisition_plan(self.acquisition_plan.id))
        self.assertIsNotNone(self.mosaics.get_mosaic_plan(self.mosaic_plan.id))
        self.assertIsNotNone(self.mosaics.get_mosaic_panel(self.mosaic_panel.id))
        self.assertIsNotNone(self.observations.get_observation("obs:m42"))
        self.assertTrue(self.project_sessions.has_association(project.id, self.session.id))

    def test_referenced_anchor_deletion_is_restricted(self) -> None:
        cases = [
            (SessionPlanItem(target_id=self.target.id), lambda: self.planning.delete_target(self.target.id)),
            (
                SessionPlanItem(acquisition_plan_id=self.acquisition_plan.id),
                lambda: self.planning.delete_acquisition_plan(self.acquisition_plan.id),
            ),
            (SessionPlanItem(mosaic_panel_id=self.mosaic_panel.id), lambda: self.mosaics.delete_mosaic_plan(self.mosaic_plan.id)),
        ]
        for item, delete_anchor in cases:
            with self.subTest(item=item):
                self.plans.save_plan(SessionPlan(session_id=self.session.id, items=[item]))
                with self.assertRaises(sqlite3.IntegrityError):
                    delete_anchor()
                self.plans.delete_plan_for_session(self.session.id)


class SessionPlanSchemaMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self._connections: list[sqlite3.Connection] = []

    def tearDown(self) -> None:
        for connection in self._connections:
            connection.close()
        self.temp_dir.cleanup()

    def _connect(self, name: str) -> sqlite3.Connection:
        connection = sqlite3.connect(self.root / name)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON;")
        self._connections.append(connection)
        return connection

    def _initialize(self, connection: sqlite3.Connection, *, migrations=MIGRATIONS):
        return initialize_schema(connection, baseline_path=DEFAULT_SCHEMA_PATH, migrations=migrations)

    def _session_plan_schema(self, connection: sqlite3.Connection) -> dict[str, list[tuple]]:
        return {
            "plan_columns": [tuple(row) for row in connection.execute("PRAGMA table_info(session_plans);").fetchall()],
            "item_columns": [tuple(row) for row in connection.execute("PRAGMA table_info(session_plan_items);").fetchall()],
            "plan_fks": [tuple(row) for row in connection.execute("PRAGMA foreign_key_list(session_plans);").fetchall()],
            "item_fks": [tuple(row) for row in connection.execute("PRAGMA foreign_key_list(session_plan_items);").fetchall()],
            "item_indexes": [tuple(row) for row in connection.execute("PRAGMA index_list(session_plan_items);").fetchall()],
        }

    def test_v7_to_v8_migration_creates_empty_session_plan_tables_and_preserves_data(self) -> None:
        connection = self._connect("upgrade.db")
        result = self._initialize(connection, migrations=MIGRATIONS[:-2])
        self.assertEqual(result.final_version, 7)
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
            INSERT INTO acquisition_plans (id, target_id, name)
            VALUES ('plan:m42', 'target:m42', 'M42 Plan');
            INSERT INTO mosaic_plans (
                id, project_slug, name, imaging_profile_id, imaging_profile_label,
                fov_width_deg, fov_height_deg, center_ra_deg, center_dec_deg,
                region_width_deg, region_height_deg, project_id
            ) VALUES (
                'mosaic:m42', 'M42', 'M42 Mosaic', 'profile:test', 'Profile',
                2.0, 1.0, 83.8, -5.4, 4.0, 2.0, 'project:m42'
            );
            INSERT INTO project_sessions (project_id, session_id)
            VALUES ('project:m42', 'session:m42');
            """
        )
        connection.commit()
        connection.close()

        connection = self._connect("upgrade.db")
        result = self._initialize(connection)

        self.assertEqual(result.applied_migrations, (8, 9))
        self.assertEqual(get_user_version(connection), CURRENT_SCHEMA_VERSION)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM session_plans;").fetchone()[0], 0)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM session_plan_items;").fetchone()[0], 0)
        for table in ("targets", "projects", "sessions", "observations", "acquisition_plans", "mosaic_plans", "project_sessions"):
            with self.subTest(table=table):
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table};").fetchone()[0], 1)
        self.assertEqual(foreign_key_violations(connection), [])

    def test_fresh_and_upgraded_v8_session_plan_schemas_converge(self) -> None:
        fresh = self._connect("fresh.db")
        upgraded = self._connect("upgraded.db")
        self._initialize(fresh)
        self._initialize(upgraded, migrations=MIGRATIONS[:-1])
        upgraded.close()
        self._connections.remove(upgraded)
        upgraded = self._connect("upgraded.db")
        self._initialize(upgraded)

        self.assertEqual(self._session_plan_schema(upgraded), self._session_plan_schema(fresh))
        self.assertEqual([column[1] for column in self._session_plan_schema(fresh)["plan_columns"]], ["session_id"])
        self.assertEqual(
            [column[1] for column in self._session_plan_schema(fresh)["item_columns"]],
            ["id", "session_id", "item_order", "target_id", "acquisition_plan_id", "mosaic_panel_id"],
        )
        self.assertIn(
            (0, 0, "sessions", "session_id", "id", "CASCADE", "CASCADE", "NONE"),
            self._session_plan_schema(fresh)["plan_fks"],
        )
        self.assertIn("idx_session_plan_items_session", {index[1] for index in self._session_plan_schema(fresh)["item_indexes"]})
