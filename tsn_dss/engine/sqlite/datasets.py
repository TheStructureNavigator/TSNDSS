from __future__ import annotations

import sqlite3

from ...domain.models import Dataset
from .db import transaction
from .observation import ObservationRepository
from .planning import PlanningRepository, ValidationError

ALLOWED_DATASET_STATUSES = {"open", "complete", "archived"}


class DatasetRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.observations = ObservationRepository(connection)
        self.planning = PlanningRepository(connection)

    def create_dataset(self, dataset: Dataset) -> Dataset:
        self._validate_dataset_payload(dataset)
        self._validate_dataset_domain_consistency(dataset)

        with transaction(self.connection):
            self.connection.execute(
                """
                INSERT INTO datasets (
                    id,
                    target_id,
                    name,
                    status,
                    total_light_integration_s,
                    notes
                ) VALUES (?, ?, ?, ?, ?, ?);
                """,
                (
                    dataset.id,
                    dataset.target_id,
                    dataset.name,
                    dataset.status,
                    dataset.total_light_integration_s,
                    dataset.notes,
                ),
            )
            self._replace_dataset_membership(dataset)

        return self.get_dataset(dataset.id)

    def get_dataset(self, dataset_id: str) -> Dataset | None:
        row = self.connection.execute(
            """
            SELECT id, target_id, name, status, total_light_integration_s, notes
            FROM datasets
            WHERE id = ?;
            """,
            (dataset_id,),
        ).fetchone()
        if not row:
            return None

        observation_ids = [
            item[0]
            for item in self.connection.execute(
                """
                SELECT observation_id
                FROM dataset_observations
                WHERE dataset_id = ?
                ORDER BY observation_id;
                """,
                (dataset_id,),
            ).fetchall()
        ]
        frame_ids = [
            item[0]
            for item in self.connection.execute(
                """
                SELECT frame_id
                FROM dataset_frames
                WHERE dataset_id = ?
                ORDER BY frame_id;
                """,
                (dataset_id,),
            ).fetchall()
        ]

        return Dataset(
            id=row["id"],
            target_id=row["target_id"],
            name=row["name"],
            status=row["status"],
            total_light_integration_s=row["total_light_integration_s"],
            notes=row["notes"],
            observation_ids=observation_ids,
            frame_ids=frame_ids,
        )

    def list_datasets(self, *, target_id: str | None = None, status: str | None = None) -> list[Dataset]:
        query = """
            SELECT id
            FROM datasets
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
        query += " ORDER BY id;"

        rows = self.connection.execute(query, params).fetchall()
        return [self.get_dataset(row["id"]) for row in rows]

    def update_dataset(self, dataset: Dataset) -> Dataset:
        self._validate_dataset_payload(dataset)
        self._validate_dataset_domain_consistency(dataset)

        with transaction(self.connection):
            cursor = self.connection.execute(
                """
                UPDATE datasets
                SET
                    target_id = ?,
                    name = ?,
                    status = ?,
                    total_light_integration_s = ?,
                    notes = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?;
                """,
                (
                    dataset.target_id,
                    dataset.name,
                    dataset.status,
                    dataset.total_light_integration_s,
                    dataset.notes,
                    dataset.id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Dataset not found: {dataset.id}")

            self.connection.execute("DELETE FROM dataset_observations WHERE dataset_id = ?;", (dataset.id,))
            self.connection.execute("DELETE FROM dataset_frames WHERE dataset_id = ?;", (dataset.id,))
            self._replace_dataset_membership(dataset)

        return self.get_dataset(dataset.id)

    def delete_dataset(self, dataset_id: str) -> None:
        with transaction(self.connection):
            self.connection.execute("DELETE FROM datasets WHERE id = ?;", (dataset_id,))

    def _replace_dataset_membership(self, dataset: Dataset) -> None:
        for observation_id in sorted(set(dataset.observation_ids)):
            self.connection.execute(
                """
                INSERT INTO dataset_observations (dataset_id, observation_id)
                VALUES (?, ?);
                """,
                (dataset.id, observation_id),
            )

        for frame_id in sorted(set(dataset.frame_ids)):
            self.connection.execute(
                """
                INSERT INTO dataset_frames (dataset_id, frame_id)
                VALUES (?, ?);
                """,
                (dataset.id, frame_id),
            )

    def _validate_dataset_payload(self, dataset: Dataset) -> None:
        if not dataset.id or not dataset.target_id or not dataset.name:
            raise ValidationError("Dataset id, target_id and name are required.")
        if dataset.status not in ALLOWED_DATASET_STATUSES:
            raise ValidationError(f"Unsupported dataset status: {dataset.status}")
        if dataset.total_light_integration_s < 0:
            raise ValidationError("Dataset total_light_integration_s must be >= 0.")

    def _validate_dataset_domain_consistency(self, dataset: Dataset) -> None:
        target = self.planning.get_target(dataset.target_id)
        if target is None:
            raise sqlite3.IntegrityError(f"Missing target for dataset: {dataset.target_id}")

        normalized_observation_ids = list(dict.fromkeys(dataset.observation_ids))
        normalized_frame_ids = list(dict.fromkeys(dataset.frame_ids))

        frame_observation_ids: set[str] = set()
        accepted_light_integration = 0.0

        for observation_id in normalized_observation_ids:
            observation = self.observations.get_observation(observation_id)
            if observation is None:
                raise sqlite3.IntegrityError(f"Missing observation for dataset: {observation_id}")
            if observation.target_id != dataset.target_id:
                raise ValidationError("Dataset target must match every linked Observation target.")

        for frame_id in normalized_frame_ids:
            frame_row = self.connection.execute(
                """
                SELECT id, observation_id, frame_type, exposure_s, accepted
                FROM frames
                WHERE id = ?;
                """,
                (frame_id,),
            ).fetchone()
            if frame_row is None:
                raise sqlite3.IntegrityError(f"Missing frame for dataset: {frame_id}")

            observation = self.observations.get_observation(frame_row["observation_id"])
            if observation is None:
                raise sqlite3.IntegrityError(
                    f"Missing observation for frame in dataset: {frame_row['observation_id']}"
                )
            if observation.target_id != dataset.target_id:
                raise ValidationError("Dataset target must match every linked Frame observation target.")

            frame_observation_ids.add(frame_row["observation_id"])

            if frame_row["frame_type"] == "light" and frame_row["accepted"] == 1:
                accepted_light_integration += float(frame_row["exposure_s"] or 0.0)

        if frame_observation_ids and not frame_observation_ids.issubset(set(normalized_observation_ids)):
            raise ValidationError(
                "Dataset observation_ids must include every Observation referenced by dataset frame membership."
            )

        if normalized_frame_ids and dataset.total_light_integration_s != accepted_light_integration:
            raise ValidationError(
                "Dataset total_light_integration_s must equal accepted light integration from dataset_frames."
            )
