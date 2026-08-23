from __future__ import annotations

import json
import sqlite3

from ...domain.models import ProcessingRun
from .datasets import DatasetRepository
from .db import transaction
from .planning import ValidationError

ALLOWED_PROCESSING_STATUSES = {"planned", "running", "completed", "failed"}
ALLOWED_PROCESSING_TRANSITIONS = {
    "planned": {"planned", "running", "failed"},
    "running": {"running", "completed", "failed"},
    "completed": {"completed"},
    "failed": {"failed"},
}


class ProcessingRunRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.datasets = DatasetRepository(connection)

    def create_processing_run(self, run: ProcessingRun) -> ProcessingRun:
        self._validate_processing_run_payload(run)
        self._validate_processing_run_domain_consistency(run)

        with transaction(self.connection):
            self.connection.execute(
                """
                INSERT INTO processing_runs (
                    id,
                    dataset_id,
                    version_label,
                    engine_name,
                    engine_version,
                    pipeline_json,
                    parameters_json,
                    linear_stack_path,
                    preview_path,
                    final_image_path,
                    status,
                    started_at,
                    finished_at,
                    notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    run.id,
                    run.dataset_id,
                    run.version_label,
                    run.engine_name,
                    run.engine_version,
                    json.dumps(run.pipeline),
                    json.dumps(run.parameters, sort_keys=True),
                    run.linear_stack_path,
                    run.preview_path,
                    run.final_image_path,
                    run.status,
                    run.started_at,
                    run.finished_at,
                    run.notes,
                ),
            )

        return self.get_processing_run(run.id)

    def get_processing_run(self, run_id: str) -> ProcessingRun | None:
        row = self.connection.execute(
            """
            SELECT
                id,
                dataset_id,
                version_label,
                engine_name,
                engine_version,
                pipeline_json,
                parameters_json,
                linear_stack_path,
                preview_path,
                final_image_path,
                status,
                started_at,
                finished_at,
                notes
            FROM processing_runs
            WHERE id = ?;
            """,
            (run_id,),
        ).fetchone()
        return _row_to_processing_run(row) if row else None

    def list_processing_runs(
        self,
        *,
        dataset_id: str | None = None,
        status: str | None = None,
    ) -> list[ProcessingRun]:
        query = """
            SELECT id
            FROM processing_runs
        """
        params: list[object] = []
        clauses: list[str] = []

        if dataset_id is not None:
            clauses.append("dataset_id = ?")
            params.append(dataset_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)

        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY dataset_id, version_label, id;"

        rows = self.connection.execute(query, params).fetchall()
        return [self.get_processing_run(row["id"]) for row in rows]

    def update_processing_run(self, run: ProcessingRun) -> ProcessingRun:
        self._validate_processing_run_payload(run)
        current = self.get_processing_run(run.id)
        if current is None:
            raise KeyError(f"ProcessingRun not found: {run.id}")

        self._validate_status_transition(current.status, run.status)
        self._validate_processing_run_domain_consistency(run)

        with transaction(self.connection):
            cursor = self.connection.execute(
                """
                UPDATE processing_runs
                SET
                    dataset_id = ?,
                    version_label = ?,
                    engine_name = ?,
                    engine_version = ?,
                    pipeline_json = ?,
                    parameters_json = ?,
                    linear_stack_path = ?,
                    preview_path = ?,
                    final_image_path = ?,
                    status = ?,
                    started_at = ?,
                    finished_at = ?,
                    notes = ?
                WHERE id = ?;
                """,
                (
                    run.dataset_id,
                    run.version_label,
                    run.engine_name,
                    run.engine_version,
                    json.dumps(run.pipeline),
                    json.dumps(run.parameters, sort_keys=True),
                    run.linear_stack_path,
                    run.preview_path,
                    run.final_image_path,
                    run.status,
                    run.started_at,
                    run.finished_at,
                    run.notes,
                    run.id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"ProcessingRun not found: {run.id}")

        return self.get_processing_run(run.id)

    def set_processing_run_status(
        self,
        run_id: str,
        new_status: str,
        *,
        started_at: str | None = None,
        finished_at: str | None = None,
        final_image_path: str | None = None,
    ) -> ProcessingRun:
        current = self.get_processing_run(run_id)
        if current is None:
            raise KeyError(f"ProcessingRun not found: {run_id}")

        self._validate_status_transition(current.status, new_status)

        updated = ProcessingRun(
            id=current.id,
            dataset_id=current.dataset_id,
            version_label=current.version_label,
            engine_name=current.engine_name,
            engine_version=current.engine_version,
            pipeline=current.pipeline,
            parameters=current.parameters,
            linear_stack_path=current.linear_stack_path,
            preview_path=current.preview_path,
            final_image_path=final_image_path if final_image_path is not None else current.final_image_path,
            status=new_status,
            started_at=started_at if started_at is not None else current.started_at,
            finished_at=finished_at if finished_at is not None else current.finished_at,
            notes=current.notes,
        )
        return self.update_processing_run(updated)

    def delete_processing_run(self, run_id: str) -> None:
        with transaction(self.connection):
            self.connection.execute("DELETE FROM processing_runs WHERE id = ?;", (run_id,))

    def _validate_processing_run_payload(self, run: ProcessingRun) -> None:
        if not run.id or not run.dataset_id or not run.version_label or not run.engine_name:
            raise ValidationError("ProcessingRun id, dataset_id, version_label and engine_name are required.")
        if run.status not in ALLOWED_PROCESSING_STATUSES:
            raise ValidationError(f"Unsupported processing status: {run.status}")
        if not isinstance(run.pipeline, list):
            raise ValidationError("ProcessingRun pipeline must be a list.")
        if not isinstance(run.parameters, dict):
            raise ValidationError("ProcessingRun parameters must be a dictionary.")
        if run.started_at is not None and run.finished_at is not None and run.finished_at < run.started_at:
            raise ValidationError("ProcessingRun finished_at must be >= started_at.")
        if run.status == "completed" and not run.final_image_path:
            raise ValidationError("Completed ProcessingRun must define final_image_path.")

    def _validate_processing_run_domain_consistency(self, run: ProcessingRun) -> None:
        dataset = self.datasets.get_dataset(run.dataset_id)
        if dataset is None:
            raise sqlite3.IntegrityError(f"Missing dataset for processing run: {run.dataset_id}")

    def _validate_status_transition(self, old_status: str, new_status: str) -> None:
        if new_status not in ALLOWED_PROCESSING_TRANSITIONS.get(old_status, set()):
            raise ValidationError(
                f"Invalid processing status transition: {old_status} -> {new_status}"
            )


def _row_to_processing_run(row: sqlite3.Row) -> ProcessingRun:
    return ProcessingRun(
        id=row["id"],
        dataset_id=row["dataset_id"],
        version_label=row["version_label"],
        engine_name=row["engine_name"],
        engine_version=row["engine_version"],
        pipeline=json.loads(row["pipeline_json"]),
        parameters=json.loads(row["parameters_json"]),
        linear_stack_path=row["linear_stack_path"],
        preview_path=row["preview_path"],
        final_image_path=row["final_image_path"],
        status=row["status"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        notes=row["notes"],
    )
