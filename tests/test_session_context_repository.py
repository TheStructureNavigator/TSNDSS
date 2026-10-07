from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from tsn_dss.domain.models import Observation, Session, SessionContextFact, Site, Target
from tsn_dss.engine.sqlite.db import DEFAULT_SCHEMA_PATH, foreign_key_violations, initialize_database
from tsn_dss.engine.sqlite.migrations import CURRENT_SCHEMA_VERSION, MIGRATIONS, get_user_version, initialize_schema
from tsn_dss.engine.sqlite.observation import ObservationRepository
from tsn_dss.engine.sqlite.planning import PlanningRepository, ValidationError
from tsn_dss.engine.sqlite.sessions import SessionRepository
from tsn_dss.engine.sqlite.session_context import SessionContextRepository

STARTED = "2026-10-07T20:00:00+00:00"
RECORDED = "2026-10-07T20:14:00+00:00"
VALID = "2026-10-07T20:14:00+00:00"
FORECAST_FOR = "2026-10-07T21:00:00+00:00"


class SessionContextRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "session_context.db"
        self.connection = initialize_database(self.db_path)
        self.sessions = SessionRepository(self.connection)
        self.context = SessionContextRepository(self.connection)
        self.planning = PlanningRepository(self.connection)
        self.observations = ObservationRepository(self.connection)
        self.session = self.sessions.create_session(Session(id="session:test", started_at=STARTED))
        self.target = self.planning.create_target(
            Target(id="target:m42", catalog="M", catalog_id="42", name="Orion Nebula", ra_deg=83.8, dec_deg=-5.4)
        )

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def numeric_fact(self, **overrides) -> SessionContextFact:
        values = dict(
            session_id=self.session.id,
            fact_name="temperature",
            epistemic_kind="observed",
            number_value=8.2,
            unit="degC",
            recorded_at=RECORDED,
            valid_from=VALID,
        )
        values.update(overrides)
        return SessionContextFact(**values)

    def text_fact(self, **overrides) -> SessionContextFact:
        values = dict(
            session_id=self.session.id,
            fact_name="operator_note",
            epistemic_kind="declared",
            text_value="thin clouds west",
            recorded_at=RECORDED,
        )
        values.update(overrides)
        return SessionContextFact(**values)

    def table_names(self) -> set[str]:
        return {
            row[0]
            for row in self.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table';").fetchall()
        }

    def column_names(self, table: str) -> list[str]:
        return [row[1] for row in self.connection.execute(f"PRAGMA table_info({table});").fetchall()]

    def test_session_valid_with_zero_context_facts(self) -> None:
        self.assertIsNotNone(self.sessions.get_session(self.session.id))
        self.assertEqual(self.context.list_facts(self.session.id), [])

    def test_context_fact_requires_existing_session(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.context.add_fact(self.numeric_fact(session_id="session:missing"))

    def test_context_fact_may_exist_with_zero_observations_and_creates_none(self) -> None:
        saved = self.context.add_fact(self.numeric_fact())

        self.assertIsNotNone(saved.id)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM observations;").fetchone()[0], 0)
        self.assertEqual(self.context.list_facts(self.session.id), [saved])

    def test_observed_numeric_fact_persists_and_round_trips(self) -> None:
        saved = self.context.add_fact(self.numeric_fact(source="manual"))

        self.assertEqual(saved.session_id, self.session.id)
        self.assertEqual(saved.fact_name, "temperature")
        self.assertEqual(saved.epistemic_kind, "observed")
        self.assertEqual(saved.number_value, 8.2)
        self.assertIsNone(saved.text_value)
        self.assertEqual(saved.unit, "degC")
        self.assertEqual(saved.recorded_at, RECORDED)
        self.assertEqual(saved.valid_from, VALID)
        self.assertEqual(saved.source, "manual")

    def test_forecast_numeric_fact_persists_and_round_trips(self) -> None:
        saved = self.context.add_fact(
            self.numeric_fact(fact_name="cloud_cover", epistemic_kind="forecast", number_value=40.0, unit="percent", valid_from=FORECAST_FOR, source="open-meteo")
        )

        self.assertEqual(saved.epistemic_kind, "forecast")
        self.assertEqual(saved.fact_name, "cloud_cover")
        self.assertEqual(saved.valid_from, FORECAST_FOR)
        self.assertEqual(saved.source, "open-meteo")

    def test_derived_numeric_fact_persists_and_round_trips(self) -> None:
        saved = self.context.add_fact(
            self.numeric_fact(fact_name="dew_point", epistemic_kind="derived", number_value=4.1, unit="degC", source="tsn_dss")
        )

        self.assertEqual(saved.epistemic_kind, "derived")
        self.assertEqual(saved.fact_name, "dew_point")
        self.assertEqual(saved.source, "tsn_dss")

    def test_declared_text_fact_persists_and_round_trips(self) -> None:
        saved = self.context.add_fact(self.text_fact(source="manual"))

        self.assertEqual(saved.epistemic_kind, "declared")
        self.assertEqual(saved.text_value, "thin clouds west")
        self.assertIsNone(saved.number_value)
        self.assertIsNone(saved.unit)
        self.assertEqual(saved.source, "manual")

    def test_forecast_observed_and_derived_facts_remain_distinguishable(self) -> None:
        observed = self.context.add_fact(self.numeric_fact(fact_name="cloud_cover", epistemic_kind="observed", number_value=25.0, unit="percent"))
        forecast = self.context.add_fact(self.numeric_fact(fact_name="cloud_cover", epistemic_kind="forecast", number_value=40.0, unit="percent", valid_from=FORECAST_FOR))
        derived = self.context.add_fact(self.numeric_fact(fact_name="dew_margin", epistemic_kind="derived", number_value=3.0, unit="degC"))

        facts = self.context.list_facts(self.session.id)
        self.assertEqual({fact.id: fact.epistemic_kind for fact in facts}, {
            observed.id: "observed",
            forecast.id: "forecast",
            derived.id: "derived",
        })

    def test_numeric_fact_requires_nonblank_unit(self) -> None:
        for unit in (None, "", "   "):
            with self.subTest(unit=unit):
                with self.assertRaisesRegex(ValidationError, "unit"):
                    self.context.add_fact(self.numeric_fact(unit=unit))

    def test_text_fact_rejects_unit(self) -> None:
        with self.assertRaisesRegex(ValidationError, "unit"):
            self.context.add_fact(self.text_fact(unit="note"))

    def test_fact_requires_exactly_one_numeric_or_text_value(self) -> None:
        with self.assertRaisesRegex(ValidationError, "exactly one"):
            self.context.add_fact(self.numeric_fact(number_value=None))
        with self.assertRaisesRegex(ValidationError, "exactly one"):
            self.context.add_fact(self.numeric_fact(text_value="also text"))

    def test_blank_text_rejected(self) -> None:
        for text in ("", "   "):
            with self.subTest(text=repr(text)):
                with self.assertRaisesRegex(ValidationError, "blank"):
                    self.context.add_fact(self.text_fact(text_value=text))

    def test_forecast_requires_valid_from_but_non_forecast_may_omit_it(self) -> None:
        with self.assertRaisesRegex(ValidationError, "valid_from"):
            self.context.add_fact(self.numeric_fact(epistemic_kind="forecast", valid_from=None))

        saved = self.context.add_fact(self.numeric_fact(epistemic_kind="observed", valid_from=None))
        self.assertIsNone(saved.valid_from)

    def test_optional_source_round_trips_and_blank_source_is_rejected(self) -> None:
        no_source = self.context.add_fact(self.numeric_fact(source=None))
        self.assertIsNone(no_source.source)

        with self.assertRaisesRegex(ValidationError, "source"):
            self.context.add_fact(self.numeric_fact(source="   "))

    def test_blank_fact_name_and_invalid_epistemic_kind_are_rejected(self) -> None:
        for fact_name in ("", "   "):
            with self.subTest(fact_name=repr(fact_name)):
                with self.assertRaisesRegex(ValidationError, "fact_name"):
                    self.context.add_fact(self.numeric_fact(fact_name=fact_name))

        with self.assertRaisesRegex(ValidationError, "epistemic_kind"):
            self.context.add_fact(self.numeric_fact(epistemic_kind="referenced"))

    def test_multiple_same_name_facts_are_allowed_and_preserve_older_fact(self) -> None:
        older = self.context.add_fact(self.numeric_fact(number_value=8.2, recorded_at="2026-10-07T20:14:00+00:00"))
        newer = self.context.add_fact(self.numeric_fact(number_value=7.9, recorded_at="2026-10-07T20:20:00+00:00"))

        facts = self.context.list_facts(self.session.id)
        self.assertEqual([fact.id for fact in facts], [older.id, newer.id])
        self.assertEqual([fact.number_value for fact in facts], [8.2, 7.9])

    def test_facts_list_deterministically(self) -> None:
        null_time = self.context.add_fact(self.text_fact(fact_name="note", text_value="late null", valid_from=None, recorded_at="2026-10-07T20:05:00+00:00"))
        middle = self.context.add_fact(self.numeric_fact(fact_name="temperature", number_value=7.9, valid_from="2026-10-07T20:20:00+00:00", recorded_at="2026-10-07T20:20:00+00:00"))
        first = self.context.add_fact(self.numeric_fact(fact_name="temperature", number_value=8.2, valid_from="2026-10-07T20:10:00+00:00", recorded_at="2026-10-07T20:14:00+00:00"))

        self.assertEqual([fact.id for fact in self.context.list_facts(self.session.id)], [first.id, middle.id, null_time.id])

    def test_session_deletion_cascades_context_facts_without_api_change(self) -> None:
        self.context.add_fact(self.numeric_fact())

        self.connection.execute("DELETE FROM sessions WHERE id = ?;", (self.session.id,))
        self.connection.commit()

        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM session_context_facts;").fetchone()[0], 0)

    def test_context_schema_has_no_observation_linkage_parent_table_or_deferred_payload_columns(self) -> None:
        self.assertNotIn("session_contexts", self.table_names())
        columns = self.column_names("session_context_facts")
        self.assertEqual(
            columns,
            ["id", "session_id", "fact_name", "epistemic_kind", "number_value", "text_value", "unit", "recorded_at", "valid_from", "source"],
        )
        forbidden = {
            "observation_id", "snapshot_id", "context_snapshot_id", "category", "value_kind", "boolean_value",
            "reference_type", "reference_id", "valid_until", "source_kind", "source_id", "source_ref",
            "payload_json", "metadata_json",
        }
        self.assertFalse(forbidden & set(columns))

    def test_existing_site_and_telemetry_authority_remain_unchanged(self) -> None:
        self.planning.create_site(Site(id="site:field", name="Field", latitude_deg=50.0, longitude_deg=20.0))
        self.observations.create_observation(Observation(id="obs:1", session_id=self.session.id, target_id=self.target.id, status="planned"))
        self.connection.execute(
            """
            INSERT INTO telemetry (measured_at, observation_id, source, metric, value_real, unit)
            VALUES ('2026-10-07T20:14:00+00:00', 'obs:1', 'sensor', 'temperature', 8.2, 'degC');
            """
        )
        self.connection.commit()

        self.assertIn("site_id", [row[1] for row in self.connection.execute("PRAGMA table_info(site_horizon_profile_points);").fetchall()])
        self.assertEqual(
            [row[1] for row in self.connection.execute("PRAGMA table_info(telemetry);").fetchall()],
            ["id", "measured_at", "observation_id", "source", "metric", "value_real", "value_text", "unit", "metadata_json"],
        )
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM session_context_facts;").fetchone()[0], 0)


class SessionContextSchemaMigrationTests(unittest.TestCase):
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

    def _context_schema(self, connection: sqlite3.Connection) -> dict[str, list[tuple]]:
        return {
            "columns": [tuple(row) for row in connection.execute("PRAGMA table_info(session_context_facts);").fetchall()],
            "fks": [tuple(row) for row in connection.execute("PRAGMA foreign_key_list(session_context_facts);").fetchall()],
            "indexes": [tuple(row) for row in connection.execute("PRAGMA index_list(session_context_facts);").fetchall()],
            "checks": [row[0] for row in connection.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'session_context_facts';").fetchall()],
        }

    def test_v8_to_v9_migration_creates_zero_context_facts_and_preserves_data(self) -> None:
        connection = self._connect("upgrade.db")
        result = self._initialize(connection, migrations=MIGRATIONS[:7])
        self.assertEqual(result.final_version, 8)
        connection.executescript(
            """
            INSERT INTO targets (id, catalog, catalog_id, name, ra_deg, dec_deg)
            VALUES ('target:m42', 'M', '42', 'Orion Nebula', 83.8, -5.4);
            INSERT INTO sites (id, name, latitude_deg, longitude_deg)
            VALUES ('site:field', 'Field', 50.0, 20.0);
            INSERT INTO sessions (id, title, state, started_at, site_id, notes)
            VALUES ('session:m42', 'M42 Session', 'planned', '2026-10-07T20:00:00+00:00', 'site:field', 'Session note');
            INSERT INTO observations (id, session_id, target_id, site_id, status, weather_notes, moon_illumination_pct)
            VALUES ('obs:m42', 'session:m42', 'target:m42', 'site:field', 'planned', 'thin cloud', 42.0);
            INSERT INTO telemetry (measured_at, observation_id, source, metric, value_real, unit)
            VALUES ('2026-10-07T20:14:00+00:00', 'obs:m42', 'sensor', 'temperature', 8.2, 'degC');
            """
        )
        connection.commit()
        connection.close()

        connection = self._connect("upgrade.db")
        result = self._initialize(connection)

        self.assertEqual(result.applied_migrations, (9, 10))
        self.assertEqual(get_user_version(connection), CURRENT_SCHEMA_VERSION)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM session_context_facts;").fetchone()[0], 0)
        for table in ("targets", "sites", "sessions", "observations", "telemetry"):
            with self.subTest(table=table):
                self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table};").fetchone()[0], 1)
        self.assertNotIn("session_contexts", {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table';").fetchall()
        })
        self.assertEqual(foreign_key_violations(connection), [])

    def test_fresh_v9_and_upgraded_v8_context_schemas_converge(self) -> None:
        fresh = self._connect("fresh.db")
        upgraded = self._connect("upgraded.db")
        self._initialize(fresh)
        self._initialize(upgraded, migrations=MIGRATIONS[:7])
        upgraded.close()
        self._connections.remove(upgraded)
        upgraded = self._connect("upgraded.db")
        self._initialize(upgraded)

        self.assertEqual(get_user_version(fresh), 10)
        self.assertEqual(get_user_version(upgraded), 10)
        self.assertEqual(self._context_schema(upgraded), self._context_schema(fresh))
        self.assertEqual(
            [column[1] for column in self._context_schema(fresh)["columns"]],
            ["id", "session_id", "fact_name", "epistemic_kind", "number_value", "text_value", "unit", "recorded_at", "valid_from", "source"],
        )
        self.assertIn(
            (0, 0, "sessions", "session_id", "id", "CASCADE", "CASCADE", "NONE"),
            self._context_schema(fresh)["fks"],
        )
        indexes = {index[1]: index for index in self._context_schema(fresh)["indexes"]}
        self.assertIn("idx_session_context_facts_session", indexes)
        self.assertIn("idx_session_context_facts_session_name", indexes)
        self.assertEqual(indexes["idx_session_context_facts_session"][2], 0)
        self.assertEqual(indexes["idx_session_context_facts_session_name"][2], 0)
        table_sql = "\n".join(self._context_schema(fresh)["checks"])
        for snippet in ("epistemic_kind IN", "number_value IS NOT NULL", "unit IS NOT NULL", "source IS NULL", "valid_from IS NOT NULL"):
            self.assertIn(snippet, table_sql)
        self.assertEqual(foreign_key_violations(fresh), [])
        self.assertEqual(foreign_key_violations(upgraded), [])


if __name__ == "__main__":
    unittest.main()
