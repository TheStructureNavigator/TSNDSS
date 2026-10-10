"""DB-04 S6: conformance of the implemented transition tables with DSS-CTR-013 sections 6 and 9.

The contract's own tables are parsed from the contract file and compared with the code, so the audit does
not rest on a restatement written by the implementer. Known, documented deviations are pinned explicitly.
"""

from __future__ import annotations

import itertools
import re
import unittest
from pathlib import Path

from tsn_dss.engine.device_runtime.command_lifecycle import COMMAND_TRANSITIONS, CommandEvent as E
from tsn_dss.engine.device_runtime.lifecycle import BUSY_TRANSITIONS, BusyEvent
from tsn_dss.engine.device_runtime.models import CommandState, ConnectionState as C

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "Contracts" / "DSS-CTR-013-Device-Provider-Runtime-Contract.md"
PACKAGE = ROOT / "tsn_dss" / "engine" / "device_runtime"

# Contract condition (prefix) -> the implementation events that realize it. Alternatives in the contract
# ("rejected or safety_blocked") and shared rows are expanded here and checked cell by cell below.
ROW_EVENTS = {
    "Request shape invalid": [E.REQUEST_INVALID_OR_UNSUPPORTED],
    "Request conflicts": [E.CONFLICT_REJECTED, E.CONFLICT_SAFETY_BLOCKED],
    "Request shape valid": [E.REQUEST_VALID],
    "Deadline elapses before Provider submission": [E.DEADLINE_BEFORE_SUBMISSION],
    "Required authorization or safety evidence": [E.SAFETY_EVIDENCE_INSUFFICIENT],
    "Required unresolved-uncertainty": [E.UNCERTAINTY_NOT_CLEARED],
    "Safety gates pass": [E.GATES_PASSED],
    "Provider rejects before acceptance and no": [E.PROVIDER_REJECTED_NO_EFFECT],
    "Provider rejects before acceptance but": [E.PROVIDER_REJECTED_EFFECT_POSSIBLE],
    "Provider acknowledges": [E.PROVIDER_ACKNOWLEDGED],
    "Deadline elapses or transport is lost": [E.DEADLINE_EFFECT_UNDETERMINED, E.TRANSPORT_LOST_EFFECT_UNKNOWN],
    "Deadline elapses and TSN DSS can prove": [E.DEADLINE_NO_EFFECT_PROVEN, E.DEADLINE_NON_PHYSICAL],
    "Effect requires monitoring": [E.MONITORING_STARTED],
    "Acknowledgement itself": [E.ACKNOWLEDGEMENT_IS_VERIFIED_EFFECT],
    "Required effect is verified": [E.EFFECT_VERIFIED],
    "Provider reports terminal failure": [E.EFFECT_FAILED],
    "Deadline elapses and the state-changing effect": [E.DEADLINE_EFFECT_UNDETERMINED],
    "Deadline elapses for an operation": [E.DEADLINE_NON_PHYSICAL],
    "Transport is lost before effect": [E.TRANSPORT_LOST_EFFECT_UNKNOWN],
    "Cancellation requested and accepted": [E.CANCEL_ACCEPTED_NO_EFFECT],
    "Cancellation races": [E.CANCEL_RACE_UNDETERMINED],
}


def _states(cell: str) -> set[CommandState]:
    return {CommandState(name) for name in re.findall(r"`(\w+)`", cell)}


def contract_rows() -> list[tuple[set[CommandState], str, set[CommandState]]]:
    text = CONTRACT.read_text(encoding="utf-8")
    block = text[text.index("Valid Command transitions:"): text.index("Rejection, validation failure and safety blocking are alternative")]
    rows = []
    for line in block.splitlines():
        if line.startswith("| `"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            rows.append((_states(cells[0]), cells[1], _states(cells[2])))
    return rows


def events_for(condition: str) -> list[E]:
    matches = [events for prefix, events in ROW_EVENTS.items() if condition.startswith(prefix)]
    assert len(matches) == 1, condition
    return matches[0]


class CommandTableConformanceTests(unittest.TestCase):
    def test_the_contract_table_has_the_expected_shape(self) -> None:
        rows = contract_rows()
        self.assertEqual(len(rows), 21)
        self.assertEqual(len(ROW_EVENTS), 21)
        self.assertEqual({events_for(cond)[0] for _, cond, _ in rows}, {e[0] for e in ROW_EVENTS.values()})

    def test_every_contract_cell_is_implemented_with_an_allowed_next_state(self) -> None:
        for starts, condition, nexts in contract_rows():
            for state, event in itertools.product(starts, events_for(condition)):
                entry = COMMAND_TRANSITIONS.get((state, event))
                self.assertIsNotNone(entry, (state, event, condition))
                self.assertIn(entry.next_state, nexts, (state, event, condition))

    def test_the_implemented_table_contains_nothing_the_contract_does_not_allow(self) -> None:
        allowed = set()
        for starts, condition, nexts in contract_rows():
            for state, event in itertools.product(starts, events_for(condition)):
                allowed.add((state, event))
        self.assertEqual(set(COMMAND_TRANSITIONS) - allowed, set())

    def test_nothing_the_contract_allows_is_missing_from_the_implemented_table(self) -> None:
        missing = set()
        for starts, condition, nexts in contract_rows():
            for state, event in itertools.product(starts, events_for(condition)):
                if (state, event) not in COMMAND_TRANSITIONS:
                    missing.add((state.value, event.value))
        self.assertEqual(missing, set())

    def test_terminal_states_and_alternative_outcomes_match_the_contract(self) -> None:
        text = CONTRACT.read_text(encoding="utf-8")
        for state in CommandState:
            match = re.search(rf"\| `{state.value}` \|[^|]*\|\s*(Yes|No)\s*\|", text)
            self.assertIsNotNone(match, state)
            self.assertEqual(match.group(1) == "Yes", state.is_terminal, state)

    def test_every_row_names_its_evidence_like_the_contract(self) -> None:
        for (state, event), row in COMMAND_TRANSITIONS.items():
            self.assertTrue(row.required_evidence.strip(), (state, event))


class DocumentedDeviationTests(unittest.TestCase):
    """Behavior that is deliberately stricter than, or absent from, the table. See the conformance audit."""

    def test_d3_the_no_effect_proof_transition_exists_but_nothing_produces_it(self) -> None:
        self.assertIn((CommandState.SUBMITTED, E.DEADLINE_NO_EFFECT_PROVEN), COMMAND_TRANSITIONS)
        for path in PACKAGE.glob("*.py"):
            if path.name != "command_lifecycle.py":
                self.assertNotIn("DEADLINE_NO_EFFECT_PROVEN", path.read_text(encoding="utf-8"), path.name)

    def test_d4_there_is_no_cancellation_of_a_command_that_was_not_submitted(self) -> None:
        for state in (CommandState.REQUESTED, CommandState.VALIDATED):
            self.assertFalse([k for k in COMMAND_TRANSITIONS if k[0] is state and "cancel" in k[1].value])

    def test_d1_a_provider_failure_report_is_never_turned_into_failed_for_a_possible_physical_effect(self) -> None:
        text = (PACKAGE / "command_executor.py").read_text(encoding="utf-8")
        self.assertIn("record.policy.physical is False or report.effect_possible is False", text)
        self.assertEqual(COMMAND_TRANSITIONS[(CommandState.ACKNOWLEDGED, E.EFFECT_FAILED)].next_state, CommandState.FAILED)


class ConnectionBusyConformanceTests(unittest.TestCase):
    def rows(self) -> list[list[str]]:
        text = CONTRACT.read_text(encoding="utf-8")
        block = text[text.index("Valid Connection transitions:"): text.index("Connection loss and Command outcome")]
        return [[c.strip() for c in line.strip("|").split("|")] for line in block.splitlines() if "`busy`" in line and line.startswith("| `")]

    def test_busy_rows_of_the_contract_are_implemented(self) -> None:
        rows = self.rows()
        self.assertEqual(len(rows), 4)
        self.assertEqual(BUSY_TRANSITIONS[(C.READY, BusyEvent.BUSY_ENTERED)].next_state, C.BUSY)
        self.assertEqual(BUSY_TRANSITIONS[(C.BUSY, BusyEvent.BUSY_LEFT_READY)].next_state, C.READY)
        self.assertEqual(BUSY_TRANSITIONS[(C.BUSY, BusyEvent.BUSY_LEFT_DEGRADED)].next_state, C.DEGRADED)
        self.assertIn("Ordinary disconnect", rows[3][1])
        self.assertIn("Rejection", rows[3][3])  # realized by the audit record, see ConnectionBusyTests

    def test_the_contract_says_busy_is_left_to_ready_or_degraded_only(self) -> None:
        self.assertEqual({t.next_state for (s, _), t in BUSY_TRANSITIONS.items() if s is C.BUSY}, {C.READY, C.DEGRADED})


class Db04BoundaryTests(unittest.TestCase):
    def test_a_full_command_exercise_leaves_the_canonical_database_untouched(self) -> None:
        """REQ-032, REQ-052, REQ-064: admission, submission, uncertainty, recovery and clearance write no domain data."""
        import hashlib
        import tempfile
        from datetime import timedelta

        from tsn_dss.engine.sqlite.db import initialize_database

        try:
            from test_device_command_submission import Rig
            from test_device_uncertainty import Op, recovered
        except ImportError:  # pragma: no cover
            from tests.test_device_command_submission import Rig
            from tests.test_device_uncertainty import Op, recovered
        from tsn_dss.engine.device_runtime.uncertainty import UncertaintyStore

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "tsn.db"
            initialize_database(db_path).close()
            before = hashlib.sha256(db_path.read_bytes()).hexdigest()
            rig = Rig(store=UncertaintyStore(), clearance_authorizer=Op(), recovery_assessors={"move": recovered})
            connection, record = rig.started("move", submit="transport_loss")
            rig.executor.resolve_uncertainty_by_recovery(connection, record.command_id)
            again = rig.ready()
            ok = rig.admit(again, "config", deadline=rig.deadline())
            rig.executor.submit(ok.command_id)
            rig.clock.advance(timedelta(minutes=5))
            rig.executor.enforce_deadline(ok.command_id)
            self.assertEqual(hashlib.sha256(db_path.read_bytes()).hexdigest(), before)

    def test_command_runtime_objects_hold_no_database_handles(self) -> None:
        import sqlite3

        try:
            from test_device_command_submission import Rig
        except ImportError:  # pragma: no cover
            from tests.test_device_command_submission import Rig

        rig = Rig()
        _, record = rig.started("config")
        holders = [rig.executor, rig.executor.uncertainty_store, record]
        for holder in holders:
            names = list(getattr(holder, "__slots__", ())) + list(getattr(holder, "__dict__", {}))
            for name in names:
                self.assertNotIsInstance(getattr(holder, name, None), sqlite3.Connection, name)

    def test_db04_modules_use_no_storage_network_or_process_modules(self) -> None:
        import ast
        import sys

        banned = {"sqlite3", "socket", "ssl", "http", "urllib", "subprocess", "multiprocessing", "ctypes", "asyncio",
                  "shelve", "dbm", "pickle", "json", "os", "pathlib", "tempfile", "shutil"}
        stdlib = set(sys.stdlib_module_names)
        for name in ("command_models", "command_lifecycle", "command_executor", "command_effects", "safety_gates", "uncertainty"):
            tree = ast.parse((PACKAGE / f"{name}.py").read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                modules = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                    [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else [])
                for module in modules:
                    top = module.split(".")[0]
                    self.assertIn(top, stdlib, (name, module))
                    self.assertNotIn(top, banned, (name, module))

    def test_the_unstable_command_surface_is_not_exported_from_the_package(self) -> None:
        """S6 decision: nothing in DB-04 is a stable public interface yet, so nothing is re-exported."""
        import tsn_dss.engine.device_runtime as pkg

        for name in ("CommandExecutor", "CommandIntent", "CommandAuthorizer", "ClearanceAuthorizer", "CommandKindPolicy",
                     "CommandKindRegistry", "CommandRecord", "CommandEvent", "UncertaintyStore", "RecoveryVerdict",
                     "BaselineRecovery", "FreshnessRequirement", "EffectVerifier", "SimulatorCommandProvider",
                     "CommandCapableProvider", "evaluate_gate", "EvidenceSnapshot"):
            self.assertNotIn(name, pkg.__all__, name)
            self.assertFalse(hasattr(pkg, name), name)

    def test_the_conformance_audit_documents_every_discrepancy_and_the_amendment_proposal(self) -> None:
        audit = (ROOT / "docs" / "DB-04_CONFORMANCE_AUDIT.md").read_text(encoding="utf-8")
        for marker in ("D1", "D2", "D3", "D4", "D5", "D6", "restart", "mutation"):
            self.assertIn(marker, audit)
        proposal = (ROOT / "docs" / "DB-04_D1_CONTRACT_AMENDMENT_PROPOSAL.md").read_text(encoding="utf-8")
        self.assertIn("NOT APPLIED", proposal)
        contract = CONTRACT.read_text(encoding="utf-8")
        self.assertNotIn("Provider reports terminal failure and a physical effect may have occurred", contract)


if __name__ == "__main__":
    unittest.main()
