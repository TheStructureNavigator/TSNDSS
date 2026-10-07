from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from tsn_dss.domain.models import SessionEvent, new_session_event_id
from tsn_dss.engine.sqlite.db import DEFAULT_SCHEMA_PATH, foreign_key_violations
from tsn_dss.engine.sqlite.migrations import CURRENT_SCHEMA_VERSION, MIGRATIONS, get_user_version, initialize_schema
from tsn_dss.engine.sqlite.planning import ValidationError
from tsn_dss.engine.sqlite.session_events import SessionEventRepository


class SessionEventRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.connection = self._connect("test.db")
        initialize_schema(self.connection, baseline_path=DEFAULT_SCHEMA_PATH)
        self._seed_session("session:one")

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def _connect(self, name: str) -> sqlite3.Connection:
        connection = sqlite3.connect(self.root / name)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON;")
        return connection

    def _seed_session(self, session_id: str) -> None:
        self.connection.execute(
            "INSERT INTO sessions (id, title, state, started_at) VALUES (?, ?, 'planned', ?);",
            (session_id, f"Session {session_id}", "2026-10-07T20:00:00+00:00"),
        )
        self.connection.commit()

    def _seed_observation(self, observation_id: str, session_id: str = "session:one") -> None:
        self.connection.execute(
            """
            INSERT OR IGNORE INTO targets (id, catalog, catalog_id, name, ra_deg, dec_deg)
            VALUES ('target:m42', 'M', '42', 'Orion Nebula', 83.8, -5.4);
            """
        )
        self.connection.execute(
            """
            INSERT INTO observations (id, session_id, target_id, status)
            VALUES (?, ?, 'target:m42', 'planned');
            """,
            (observation_id, session_id),
        )
        self.connection.commit()

    def _event(self, **overrides: object) -> SessionEvent:
        values = {
            "id": "event:one",
            "session_id": "session:one",
            "event_type": "operator.note",
            "occurred_at": "2026-10-07T20:10:00+00:00",
            "observation_id": None,
            "source": None,
        }
        values.update(overrides)
        return SessionEvent(**values)

    def test_new_session_event_id_is_opaque_event_identity(self) -> None:
        first = new_session_event_id()
        second = new_session_event_id()

        self.assertRegex(first, r"^event:[0-9a-f]{12}$")
        self.assertNotEqual(first, second)

    def test_session_with_zero_session_events_remains_valid(self) -> None:
        self.assertEqual(SessionEventRepository(self.connection).list_events("session:one"), [])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM sessions;").fetchone()[0], 1)

    def test_add_and_list_canonical_event(self) -> None:
        repository = SessionEventRepository(self.connection)

        saved = repository.add_event(self._event(source="operator"))

        self.assertEqual(saved, self._event(source="operator"))
        self.assertEqual(repository.list_events("session:one"), [saved])

    def test_list_events_is_ordered_by_occurred_at_then_id(self) -> None:
        repository = SessionEventRepository(self.connection)
        repository.add_event(self._event(id="event:c", occurred_at="2026-10-07T20:20:00+00:00"))
        repository.add_event(self._event(id="event:b", occurred_at="2026-10-07T20:10:00+00:00"))
        repository.add_event(self._event(id="event:a", occurred_at="2026-10-07T20:10:00+00:00"))

        self.assertEqual([event.id for event in repository.list_events("session:one")], ["event:a", "event:b", "event:c"])

    def test_duplicate_id_is_rejected(self) -> None:
        repository = SessionEventRepository(self.connection)
        repository.add_event(self._event())

        with self.assertRaises(ValidationError):
            repository.add_event(self._event(event_type="another.type"))

    def test_nonexistent_session_is_rejected(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            SessionEventRepository(self.connection).add_event(self._event(session_id="session:missing"))

    def test_blank_id_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            SessionEventRepository(self.connection).add_event(self._event(id="  "))

    def test_blank_event_type_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            SessionEventRepository(self.connection).add_event(self._event(event_type="  "))

    def test_unknown_event_type_is_accepted(self) -> None:
        saved = SessionEventRepository(self.connection).add_event(self._event(event_type="future.event.type"))

        self.assertEqual(saved.event_type, "future.event.type")

    def test_historical_occurred_at_is_preserved_unchanged(self) -> None:
        timestamp = "1999-01-02T03:04:05+00:00"

        saved = SessionEventRepository(self.connection).add_event(self._event(occurred_at=timestamp))

        self.assertEqual(saved.occurred_at, timestamp)

    def test_blank_occurred_at_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            SessionEventRepository(self.connection).add_event(self._event(occurred_at="  "))

    def test_null_observation_id_is_accepted(self) -> None:
        saved = SessionEventRepository(self.connection).add_event(self._event(observation_id=None))

        self.assertIsNone(saved.observation_id)

    def test_same_session_observation_is_accepted(self) -> None:
        self._seed_observation("obs:one")

        saved = SessionEventRepository(self.connection).add_event(self._event(observation_id="obs:one"))

        self.assertEqual(saved.observation_id, "obs:one")

    def test_nonexistent_observation_is_rejected(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            SessionEventRepository(self.connection).add_event(self._event(observation_id="obs:missing"))

    def test_cross_session_observation_is_rejected(self) -> None:
        self._seed_session("session:two")
        self._seed_observation("obs:two", session_id="session:two")

        with self.assertRaises(ValidationError):
            SessionEventRepository(self.connection).add_event(self._event(observation_id="obs:two"))

    def test_null_source_is_accepted(self) -> None:
        saved = SessionEventRepository(self.connection).add_event(self._event(source=None))

        self.assertIsNone(saved.source)

    def test_nonblank_source_is_accepted(self) -> None:
        saved = SessionEventRepository(self.connection).add_event(self._event(source="wzrd"))

        self.assertEqual(saved.source, "wzrd")

    def test_blank_source_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            SessionEventRepository(self.connection).add_event(self._event(source="  "))

    def test_repository_never_returns_legacy_events(self) -> None:
        self.connection.execute(
            """
            INSERT INTO events (occurred_at, source, event_type, severity, payload_json)
            VALUES ('2026-10-07T20:00:00+00:00', 'legacy', 'legacy.event', 'info', '{}');
            """
        )
        self.connection.commit()

        self.assertEqual(SessionEventRepository(self.connection).list_events("session:one"), [])


class SessionEventMigrationTests(unittest.TestCase):
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

    def _event_schema(self, connection: sqlite3.Connection) -> dict[str, list[tuple]]:
        return {
            "columns": [tuple(row) for row in connection.execute("PRAGMA table_info(session_events);").fetchall()],
            "fks": [tuple(row) for row in connection.execute("PRAGMA foreign_key_list(session_events);").fetchall()],
            "indexes": [tuple(row) for row in connection.execute("PRAGMA index_list(session_events);").fetchall()],
            "sql": [row[0] for row in connection.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'session_events';").fetchall()],
        }

    def test_fresh_v10_schema_contains_exact_session_events_shape(self) -> None:
        connection = self._connect("fresh.db")
        result = initialize_schema(connection, baseline_path=DEFAULT_SCHEMA_PATH)

        self.assertEqual(CURRENT_SCHEMA_VERSION, 10)
        self.assertEqual([migration.version for migration in MIGRATIONS], [2, 3, 4, 5, 6, 7, 8, 9, 10])
        self.assertEqual(result.final_version, 10)
        self.assertEqual(get_user_version(connection), 10)
        schema = self._event_schema(connection)
        self.assertEqual([column[1] for column in schema["columns"]], ["id", "session_id", "event_type", "occurred_at", "observation_id", "source"])
        self.assertIn((0, 0, "observations", "observation_id", "id", "CASCADE", "SET NULL", "NONE"), schema["fks"])
        self.assertIn((1, 0, "sessions", "session_id", "id", "CASCADE", "CASCADE", "NONE"), schema["fks"])
        indexes = {index[1] for index in schema["indexes"]}
        self.assertIn("idx_session_events_session_time", indexes)
        self.assertIn("idx_session_events_observation", indexes)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM session_events;").fetchone()[0], 0)

    def test_v9_to_v10_migration_creates_zero_session_events_and_preserves_legacy_events(self) -> None:
        connection = self._connect("upgrade.db")
        result = initialize_schema(connection, baseline_path=DEFAULT_SCHEMA_PATH, migrations=MIGRATIONS[:-1])
        self.assertEqual(result.final_version, 9)
        connection.execute(
            """
            INSERT INTO events (occurred_at, source, event_type, severity, payload_json)
            VALUES ('2026-10-07T20:00:00+00:00', 'legacy', 'legacy.event', 'warning', '{"k": "v"}');
            """
        )
        connection.commit()
        connection.close()

        connection = self._connect("upgrade.db")
        result = initialize_schema(connection, baseline_path=DEFAULT_SCHEMA_PATH)

        self.assertEqual(result.applied_migrations, (10,))
        self.assertEqual(get_user_version(connection), 10)
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table';").fetchall()}
        self.assertIn("events", tables)
        self.assertIn("session_events", tables)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM session_events;").fetchone()[0], 0)
        self.assertEqual(tuple(connection.execute("SELECT source, event_type, severity, payload_json FROM events;").fetchone()), ("legacy", "legacy.event", "warning", '{"k": "v"}'))
        self.assertEqual(foreign_key_violations(connection), [])

    def test_fresh_and_upgraded_v10_session_event_schemas_converge(self) -> None:
        fresh = self._connect("fresh.db")
        upgraded = self._connect("upgraded.db")
        initialize_schema(fresh, baseline_path=DEFAULT_SCHEMA_PATH)
        initialize_schema(upgraded, baseline_path=DEFAULT_SCHEMA_PATH, migrations=MIGRATIONS[:-1])
        upgraded.close()
        self._connections.remove(upgraded)
        upgraded = self._connect("upgraded.db")
        initialize_schema(upgraded, baseline_path=DEFAULT_SCHEMA_PATH)

        self.assertEqual(self._event_schema(upgraded), self._event_schema(fresh))


if __name__ == "__main__":
    unittest.main()
