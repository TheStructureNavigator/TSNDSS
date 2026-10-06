from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tsn_dss.domain.models import (
    AcquisitionPlan,
    AcquisitionSequence,
    Equipment,
    Observation,
    ObservationEquipmentAssignment,
    Site,
    Target,
)
from tsn_dss.engine.sqlite.db import initialize_database
from tsn_dss.engine.sqlite.observation import ObservationRepository
from tsn_dss.engine.sqlite.planning import PlanningRepository, ValidationError


TEST_SESSION_ID = "session:test-observation"

class ObservationRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "observation.db"
        self.connection = initialize_database(self.db_path)
        self.connection.execute(
            """
            INSERT INTO sessions (id, title, state, started_at, notes)
            VALUES (?, 'Test Observation Session', 'planned', '2026-08-21T20:00:00+00:00', 'Test fixture Session.')
            ON CONFLICT(id) DO NOTHING;
            """,
            (TEST_SESSION_ID,),
        )
        self.connection.commit()
        self.planning = PlanningRepository(self.connection)
        self.observations = ObservationRepository(self.connection)
        self._seed_planning_graph()

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def test_create_observation_with_equipment_assignments(self) -> None:
        created = self.observations.create_observation(
            Observation(session_id=TEST_SESSION_ID,
                id="obs:1001",
                observation_number=1001,
                target_id="target:m42",
                site_id="site:field-01",
                acquisition_plan_id="plan:m42:first-light",
                status="planned",
                operator_notes="Waiting for clear skies",
                equipment_assignments=[
                    ObservationEquipmentAssignment("obs:1001", "mount:gti-001", "mount"),
                    ObservationEquipmentAssignment("obs:1001", "telescope:sw72ed-001", "main_telescope"),
                    ObservationEquipmentAssignment("obs:1001", "camera:canon600d-001", "main_camera"),
                ],
            )
        )

        self.assertEqual(created.id, "obs:1001")
        self.assertEqual(len(created.equipment_assignments), 3)

    def test_list_observations_can_filter_by_target_and_status(self) -> None:
        self.observations.create_observation(
            Observation(session_id=TEST_SESSION_ID, id="obs:m42", observation_number=1, target_id="target:m42", status="planned")
        )
        self.observations.create_observation(
            Observation(session_id=TEST_SESSION_ID, id="obs:m31", observation_number=2, target_id="target:m31", status="completed")
        )

        filtered = self.observations.list_observations(target_id="target:m42", status="planned")

        self.assertEqual([item.id for item in filtered], ["obs:m42"])

    def test_valid_status_lifecycle_progression(self) -> None:
        self.observations.create_observation(
            Observation(session_id=TEST_SESSION_ID, id="obs:lifecycle", observation_number=3, target_id="target:m42", status="planned")
        )

        preparing = self.observations.set_observation_status("obs:lifecycle", "preparing")
        running = self.observations.set_observation_status(
            "obs:lifecycle",
            "running",
            started_at="2026-08-21 20:15:00",
        )
        completed = self.observations.set_observation_status(
            "obs:lifecycle",
            "completed",
            finished_at="2026-08-21 23:05:00",
        )

        self.assertEqual(preparing.status, "preparing")
        self.assertEqual(running.started_at, "2026-08-21 20:15:00")
        self.assertEqual(completed.status, "completed")

    def test_invalid_status_transition_is_rejected(self) -> None:
        self.observations.create_observation(
            Observation(session_id=TEST_SESSION_ID, id="obs:invalid-transition", observation_number=4, target_id="target:m42")
        )

        with self.assertRaises(ValidationError):
            self.observations.set_observation_status("obs:invalid-transition", "completed")

    def test_observation_target_must_match_plan_target(self) -> None:
        with self.assertRaises(ValidationError):
            self.observations.create_observation(
                Observation(session_id=TEST_SESSION_ID,
                    id="obs:wrong-target",
                    observation_number=5,
                    target_id="target:m31",
                    acquisition_plan_id="plan:m42:first-light",
                )
            )

    def test_equipment_role_must_match_equipment_type(self) -> None:
        with self.assertRaises(ValidationError):
            self.observations.create_observation(
                Observation(session_id=TEST_SESSION_ID,
                    id="obs:wrong-role",
                    observation_number=6,
                    target_id="target:m42",
                    equipment_assignments=[
                        ObservationEquipmentAssignment("obs:wrong-role", "camera:canon600d-001", "mount")
                    ],
                )
            )

    def test_assign_equipment_upserts_same_role(self) -> None:
        self.planning.create_equipment(
            Equipment(
                id="camera:asi533mc-001",
                equipment_type="camera",
                manufacturer="ZWO",
                model="ASI533MC",
                properties={"sensor": "IMX533"},
            )
        )
        self.observations.create_observation(
            Observation(session_id=TEST_SESSION_ID, id="obs:equipment-upsert", observation_number=7, target_id="target:m42")
        )

        self.observations.assign_equipment("obs:equipment-upsert", "camera:canon600d-001", "main_camera")
        self.observations.assign_equipment("obs:equipment-upsert", "camera:asi533mc-001", "main_camera")

        observation = self.observations.get_observation("obs:equipment-upsert")

        self.assertEqual(len(observation.equipment_assignments), 1)
        self.assertEqual(observation.equipment_assignments[0].equipment_id, "camera:asi533mc-001")

    def test_update_observation_replaces_equipment_assignments(self) -> None:
        self.observations.create_observation(
            Observation(session_id=TEST_SESSION_ID,
                id="obs:update",
                observation_number=8,
                target_id="target:m42",
                equipment_assignments=[
                    ObservationEquipmentAssignment("obs:update", "mount:gti-001", "mount"),
                    ObservationEquipmentAssignment("obs:update", "camera:canon600d-001", "main_camera"),
                ],
            )
        )

        updated = self.observations.update_observation(
            Observation(session_id=TEST_SESSION_ID,
                id="obs:update",
                observation_number=8,
                target_id="target:m42",
                site_id="site:field-01",
                status="planned",
                equipment_assignments=[
                    ObservationEquipmentAssignment("obs:update", "telescope:sw72ed-001", "main_telescope")
                ],
            )
        )

        self.assertEqual(updated.site_id, "site:field-01")
        self.assertEqual([item.role for item in updated.equipment_assignments], ["main_telescope"])

    def test_remove_equipment_assignment(self) -> None:
        self.observations.create_observation(
            Observation(session_id=TEST_SESSION_ID,
                id="obs:remove-assignment",
                observation_number=9,
                target_id="target:m42",
                equipment_assignments=[
                    ObservationEquipmentAssignment("obs:remove-assignment", "mount:gti-001", "mount")
                ],
            )
        )

        self.observations.remove_equipment_assignment("obs:remove-assignment", "mount")
        observation = self.observations.get_observation("obs:remove-assignment")

        self.assertEqual(observation.equipment_assignments, [])

    def test_delete_observation_cascades_equipment_assignments(self) -> None:
        self.observations.create_observation(
            Observation(session_id=TEST_SESSION_ID,
                id="obs:delete",
                observation_number=10,
                target_id="target:m42",
                equipment_assignments=[
                    ObservationEquipmentAssignment("obs:delete", "mount:gti-001", "mount")
                ],
            )
        )

        self.observations.delete_observation("obs:delete")
        assignment_count = self.connection.execute(
            "SELECT COUNT(*) FROM observation_equipment WHERE observation_id = ?;",
            ("obs:delete",),
        ).fetchone()[0]

        self.assertIsNone(self.observations.get_observation("obs:delete"))
        self.assertEqual(assignment_count, 0)

    def test_finished_before_started_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.observations.create_observation(
                Observation(session_id=TEST_SESSION_ID,
                    id="obs:bad-time",
                    observation_number=11,
                    target_id="target:m42",
                    started_at="2026-08-21 23:00:00",
                    finished_at="2026-08-21 22:59:00",
                )
            )

    def _seed_planning_graph(self) -> None:
        self.planning.create_target(
            Target(
                id="target:m42",
                catalog="MESSIER",
                catalog_id="M42",
                name="Orion Nebula",
                ra_deg=83.8221,
                dec_deg=-5.3911,
            )
        )
        self.planning.create_target(
            Target(
                id="target:m31",
                catalog="MESSIER",
                catalog_id="M31",
                name="Andromeda Galaxy",
                ra_deg=10.6847,
                dec_deg=41.2692,
            )
        )
        self.planning.create_site(
            Site(
                id="site:field-01",
                name="Field Site",
                latitude_deg=50.0,
                longitude_deg=19.0,
                bortle_class=4,
            )
        )
        self.planning.create_equipment(
            Equipment(
                id="mount:gti-001",
                equipment_type="mount",
                manufacturer="Sky-Watcher",
                model="Star Adventurer GTi",
                properties={"payload_kg_nominal": 5},
            )
        )
        self.planning.create_equipment(
            Equipment(
                id="telescope:sw72ed-001",
                equipment_type="telescope",
                manufacturer="Sky-Watcher",
                model="Evostar 72ED",
                properties={"focal_length_mm": 420},
            )
        )
        self.planning.create_equipment(
            Equipment(
                id="camera:canon600d-001",
                equipment_type="camera",
                manufacturer="Canon",
                model="EOS 600D",
                properties={"sensor_type": "APS-C CMOS"},
            )
        )
        self.planning.save_acquisition_plan(
            AcquisitionPlan(
                id="plan:m42:first-light",
                target_id="target:m42",
                name="M42 First Light",
                status="ready",
                sequences=[
                    AcquisitionSequence(
                        sequence_order=10,
                        frame_type="light",
                        exposure_s=30.0,
                        frame_count=120,
                    )
                ],
            )
        )


if __name__ == "__main__":
    unittest.main()
