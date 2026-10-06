from __future__ import annotations

import sqlite3

from ...domain.models import Observation, ObservationEquipmentAssignment
from .db import transaction
from .planning import PlanningRepository, ValidationError

ALLOWED_OBSERVATION_STATUSES = {
    "planned",
    "preparing",
    "running",
    "paused",
    "completed",
    "aborted",
    "failed",
}

ALLOWED_OBSERVATION_ROLES = {
    "mount",
    "main_telescope",
    "main_camera",
    "guide_scope",
    "guide_camera",
    "focuser",
    "filter_wheel",
    "reducer_flattener",
    "barlow",
    "power",
    "computer",
    "other",
}

ROLE_TO_EQUIPMENT_TYPES = {
    "mount": {"mount"},
    "main_telescope": {"telescope"},
    "main_camera": {"camera"},
    "guide_scope": {"guide_scope", "telescope"},
    "guide_camera": {"guide_camera", "camera"},
    "focuser": {"focuser"},
    "filter_wheel": {"filter_wheel"},
    "reducer_flattener": {"reducer_flattener"},
    "barlow": {"barlow"},
    "power": {"power"},
    "computer": {"computer"},
    "other": {"other"},
}

ALLOWED_STATUS_TRANSITIONS = {
    "planned": {"planned", "preparing", "aborted", "failed"},
    "preparing": {"preparing", "running", "paused", "aborted", "failed"},
    "running": {"running", "paused", "completed", "aborted", "failed"},
    "paused": {"paused", "running", "aborted", "failed"},
    "completed": {"completed"},
    "aborted": {"aborted"},
    "failed": {"failed"},
}


class ObservationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.planning_repository = PlanningRepository(connection)

    def create_observation(self, observation: Observation) -> Observation:
        _validate_observation_payload(observation)
        self._validate_observation_domain_consistency(observation)

        with transaction(self.connection):
            self.connection.execute(
                """
                INSERT INTO observations (
                    id,
                    observation_number,
                    session_id,
                    target_id,
                    site_id,
                    acquisition_plan_id,
                    status,
                    started_at,
                    finished_at,
                    operator_notes,
                    weather_notes,
                    moon_illumination_pct
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    observation.id,
                    observation.observation_number,
                    observation.session_id,
                    observation.target_id,
                    observation.site_id,
                    observation.acquisition_plan_id,
                    observation.status,
                    observation.started_at,
                    observation.finished_at,
                    observation.operator_notes,
                    observation.weather_notes,
                    observation.moon_illumination_pct,
                ),
            )

            for assignment in observation.equipment_assignments:
                self._insert_equipment_assignment(assignment)

        return self.get_observation(observation.id)

    def get_observation(self, observation_id: str) -> Observation | None:
        row = self.connection.execute(
            """
            SELECT
                id,
                observation_number,
                session_id,
                target_id,
                site_id,
                acquisition_plan_id,
                status,
                started_at,
                finished_at,
                operator_notes,
                weather_notes,
                moon_illumination_pct
            FROM observations
            WHERE id = ?;
            """,
            (observation_id,),
        ).fetchone()
        if not row:
            return None

        assignments = self._list_equipment_assignments(observation_id)
        return _row_to_observation(row, assignments)

    def list_observations(self, *, target_id: str | None = None, status: str | None = None) -> list[Observation]:
        query = """
            SELECT
                id,
                observation_number,
                session_id,
                target_id,
                site_id,
                acquisition_plan_id,
                status,
                started_at,
                finished_at,
                operator_notes,
                weather_notes,
                moon_illumination_pct
            FROM observations
        """
        params: list[object] = []
        clauses: list[str] = []

        if target_id is not None:
            clauses.append("target_id = ?")
            params.append(target_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)

        if clauses:
            query += " WHERE " + " AND ".join(clauses)

        query += " ORDER BY observation_number, id;"

        rows = self.connection.execute(query, params).fetchall()
        observations: list[Observation] = []
        for row in rows:
            observations.append(_row_to_observation(row, self._list_equipment_assignments(row["id"])))
        return observations

    def update_observation(self, observation: Observation) -> Observation:
        _validate_observation_payload(observation)
        current = self.get_observation(observation.id)
        if current is None:
            raise KeyError(f"Observation not found: {observation.id}")

        self._validate_status_transition(current.status, observation.status)
        self._validate_observation_domain_consistency(observation)

        with transaction(self.connection):
            self.connection.execute(
                """
                UPDATE observations
                SET
                    observation_number = ?,
                    session_id = ?,
                    target_id = ?,
                    site_id = ?,
                    acquisition_plan_id = ?,
                    status = ?,
                    started_at = ?,
                    finished_at = ?,
                    operator_notes = ?,
                    weather_notes = ?,
                    moon_illumination_pct = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?;
                """,
                (
                    observation.observation_number,
                    observation.session_id,
                    observation.target_id,
                    observation.site_id,
                    observation.acquisition_plan_id,
                    observation.status,
                    observation.started_at,
                    observation.finished_at,
                    observation.operator_notes,
                    observation.weather_notes,
                    observation.moon_illumination_pct,
                    observation.id,
                ),
            )

            self.connection.execute(
                "DELETE FROM observation_equipment WHERE observation_id = ?;",
                (observation.id,),
            )
            for assignment in observation.equipment_assignments:
                self._insert_equipment_assignment(assignment)

        return self.get_observation(observation.id)

    def set_observation_status(
        self,
        observation_id: str,
        new_status: str,
        *,
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> Observation:
        current = self.get_observation(observation_id)
        if current is None:
            raise KeyError(f"Observation not found: {observation_id}")

        self._validate_status_transition(current.status, new_status)

        updated = Observation(
            id=current.id,
            observation_number=current.observation_number,
            session_id=current.session_id,
            target_id=current.target_id,
            site_id=current.site_id,
            acquisition_plan_id=current.acquisition_plan_id,
            status=new_status,
            started_at=started_at if started_at is not None else current.started_at,
            finished_at=finished_at if finished_at is not None else current.finished_at,
            operator_notes=current.operator_notes,
            weather_notes=current.weather_notes,
            moon_illumination_pct=current.moon_illumination_pct,
            equipment_assignments=current.equipment_assignments,
        )
        return self.update_observation(updated)

    def assign_equipment(
        self,
        observation_id: str,
        equipment_id: str,
        role: str,
    ) -> ObservationEquipmentAssignment:
        observation = self.get_observation(observation_id)
        if observation is None:
            raise KeyError(f"Observation not found: {observation_id}")

        assignment = ObservationEquipmentAssignment(
            observation_id=observation_id,
            equipment_id=equipment_id,
            role=role,
        )
        self._validate_equipment_assignment(assignment, observation_target_id=observation.target_id)

        with transaction(self.connection):
            self.connection.execute(
                """
                INSERT INTO observation_equipment (observation_id, equipment_id, role)
                VALUES (?, ?, ?)
                ON CONFLICT(observation_id, role)
                DO UPDATE SET equipment_id = excluded.equipment_id;
                """,
                (observation_id, equipment_id, role),
            )

        return assignment

    def remove_equipment_assignment(self, observation_id: str, role: str) -> None:
        with transaction(self.connection):
            self.connection.execute(
                "DELETE FROM observation_equipment WHERE observation_id = ? AND role = ?;",
                (observation_id, role),
            )

    def delete_observation(self, observation_id: str) -> None:
        with transaction(self.connection):
            self.connection.execute("DELETE FROM observations WHERE id = ?;", (observation_id,))

    def _validate_observation_domain_consistency(self, observation: Observation) -> None:
        session = self.connection.execute("SELECT 1 FROM sessions WHERE id = ?;", (observation.session_id,)).fetchone()
        if session is None:
            raise sqlite3.IntegrityError(f"Missing session for observation: {observation.session_id}")

        target = self.planning_repository.get_target(observation.target_id)
        if target is None:
            raise sqlite3.IntegrityError(f"Missing target for observation: {observation.target_id}")

        if observation.site_id is not None and self.planning_repository.get_site(observation.site_id) is None:
            raise sqlite3.IntegrityError(f"Missing site for observation: {observation.site_id}")

        if observation.acquisition_plan_id is not None:
            plan = self.planning_repository.get_acquisition_plan(observation.acquisition_plan_id)
            if plan is None:
                raise sqlite3.IntegrityError(
                    f"Missing acquisition plan for observation: {observation.acquisition_plan_id}"
                )
            if plan.target_id != observation.target_id:
                raise ValidationError(
                    "Observation target must match acquisition plan target."
                )

        seen_roles: set[str] = set()
        for assignment in observation.equipment_assignments:
            if assignment.observation_id != observation.id:
                raise ValidationError("Equipment assignment observation_id must match Observation.id.")
            if assignment.role in seen_roles:
                raise ValidationError(f"Duplicate equipment role for observation: {assignment.role}")
            seen_roles.add(assignment.role)
            self._validate_equipment_assignment(assignment, observation_target_id=observation.target_id)

    def _validate_equipment_assignment(
        self,
        assignment: ObservationEquipmentAssignment,
        *,
        observation_target_id: str,
    ) -> None:
        if assignment.role not in ALLOWED_OBSERVATION_ROLES:
            raise ValidationError(f"Unsupported observation equipment role: {assignment.role}")

        equipment = self.planning_repository.get_equipment(assignment.equipment_id)
        if equipment is None:
            raise sqlite3.IntegrityError(f"Missing equipment for observation: {assignment.equipment_id}")

        allowed_types = ROLE_TO_EQUIPMENT_TYPES[assignment.role]
        if equipment.equipment_type not in allowed_types:
            raise ValidationError(
                f"Equipment {assignment.equipment_id} of type {equipment.equipment_type} "
                f"cannot be assigned to role {assignment.role}."
            )

        if assignment.role == "other" and observation_target_id == "":
            raise ValidationError("Observation target id is required.")

    def _validate_status_transition(self, old_status: str, new_status: str) -> None:
        if new_status not in ALLOWED_STATUS_TRANSITIONS.get(old_status, set()):
            raise ValidationError(
                f"Invalid observation status transition: {old_status} -> {new_status}"
            )

    def _insert_equipment_assignment(self, assignment: ObservationEquipmentAssignment) -> None:
        self.connection.execute(
            """
            INSERT INTO observation_equipment (observation_id, equipment_id, role)
            VALUES (?, ?, ?);
            """,
            (assignment.observation_id, assignment.equipment_id, assignment.role),
        )

    def _list_equipment_assignments(self, observation_id: str) -> list[ObservationEquipmentAssignment]:
        rows = self.connection.execute(
            """
            SELECT observation_id, equipment_id, role
            FROM observation_equipment
            WHERE observation_id = ?
            ORDER BY role;
            """,
            (observation_id,),
        ).fetchall()
        return [
            ObservationEquipmentAssignment(
                observation_id=row["observation_id"],
                equipment_id=row["equipment_id"],
                role=row["role"],
            )
            for row in rows
        ]


def _validate_observation_payload(observation: Observation) -> None:
    if not observation.id:
        raise ValidationError("Observation id is required.")
    if not observation.session_id:
        raise ValidationError("Observation session_id is required.")
    if not observation.target_id:
        raise ValidationError("Observation target_id is required.")
    if observation.status not in ALLOWED_OBSERVATION_STATUSES:
        raise ValidationError(f"Unsupported observation status: {observation.status}")
    if (
        observation.moon_illumination_pct is not None
        and not 0 <= observation.moon_illumination_pct <= 100
    ):
        raise ValidationError("Observation moon_illumination_pct must be in [0, 100].")
    if (
        observation.started_at is not None
        and observation.finished_at is not None
        and observation.finished_at < observation.started_at
    ):
        raise ValidationError("Observation finished_at must be >= started_at.")


def _row_to_observation(
    row: sqlite3.Row,
    assignments: list[ObservationEquipmentAssignment],
) -> Observation:
    return Observation(
        id=row["id"],
        observation_number=row["observation_number"],
        session_id=row["session_id"],
        target_id=row["target_id"],
        site_id=row["site_id"],
        acquisition_plan_id=row["acquisition_plan_id"],
        status=row["status"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        operator_notes=row["operator_notes"],
        weather_notes=row["weather_notes"],
        moon_illumination_pct=row["moon_illumination_pct"],
        equipment_assignments=assignments,
    )
