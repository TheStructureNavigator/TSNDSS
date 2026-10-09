"""DB-03 Wave 4B-1: worker state machine."""

from __future__ import annotations

import unittest

from tsn_dss.engine.device_runtime.errors import InvalidTransition
from tsn_dss.engine.opencv_isolated_decoder.states import WORKER_TRANSITIONS, WorkerEvent as V, WorkerState as S, worker_next_state


class TransitionTableTests(unittest.TestCase):
    def test_states_are_the_requested_set_plus_opening(self) -> None:
        self.assertEqual({s.name for s in S}, {"CREATED", "STARTING", "READY", "OPENING", "READING", "FAILED", "STOPPING", "TERMINATED"})

    def test_every_pair_is_allowed_or_invalid(self) -> None:
        for state in S:
            for event in V:
                if (state, event) in WORKER_TRANSITIONS:
                    self.assertIsInstance(worker_next_state(state, event), S)
                else:
                    with self.assertRaises(InvalidTransition):
                        worker_next_state(state, event)

    def test_exact_table_size(self) -> None:
        self.assertEqual(len(WORKER_TRANSITIONS), 20)

    def test_happy_path(self) -> None:
        state = S.CREATED
        for event, expected in ((V.START_REQUESTED, S.STARTING), (V.HANDSHAKE_COMPLETE, S.READY), (V.OPEN_REQUESTED, S.OPENING),
                                (V.OPEN_COMPLETE, S.READY), (V.READ_REQUESTED, S.READING), (V.READ_COMPLETE, S.READY),
                                (V.STOP_REQUESTED, S.STOPPING), (V.STOPPED, S.TERMINATED)):
            state = worker_next_state(state, event)
            self.assertIs(state, expected)

    def test_failed_always_proceeds_to_stopping_and_never_rests(self) -> None:
        for state in (S.STARTING, S.READY, S.OPENING, S.READING):
            self.assertIs(worker_next_state(state, V.FAILURE), S.FAILED)
        self.assertIs(worker_next_state(S.FAILED, V.STOP_REQUESTED), S.STOPPING)
        for event in V:
            if event is not V.STOP_REQUESTED:
                self.assertNotIn((S.FAILED, event), WORKER_TRANSITIONS, event)

    def test_terminated_is_terminal_and_never_reused(self) -> None:
        self.assertEqual([e for (s, e) in WORKER_TRANSITIONS if s is S.TERMINATED], [V.STOP_REQUESTED])
        self.assertIs(worker_next_state(S.TERMINATED, V.STOP_REQUESTED), S.TERMINATED)
        for event in (V.START_REQUESTED, V.HANDSHAKE_COMPLETE, V.OPEN_REQUESTED, V.READ_REQUESTED, V.FAILURE):
            with self.assertRaises(InvalidTransition):
                worker_next_state(S.TERMINATED, event)

    def test_stop_is_idempotent_and_allowed_everywhere_it_makes_sense(self) -> None:
        for state in (S.CREATED, S.STARTING, S.READY, S.OPENING, S.READING, S.FAILED, S.STOPPING, S.TERMINATED):
            self.assertIn((state, V.STOP_REQUESTED), WORKER_TRANSITIONS)

    def test_operations_need_ready(self) -> None:
        for state in (S.CREATED, S.STARTING, S.OPENING, S.READING, S.FAILED, S.STOPPING, S.TERMINATED):
            for event in (V.OPEN_REQUESTED, V.READ_REQUESTED):
                self.assertNotIn((state, event), WORKER_TRANSITIONS)

    def test_no_second_operation_while_one_is_running(self) -> None:
        self.assertNotIn((S.OPENING, V.READ_REQUESTED), WORKER_TRANSITIONS)
        self.assertNotIn((S.READING, V.OPEN_REQUESTED), WORKER_TRANSITIONS)
        self.assertNotIn((S.READING, V.READ_REQUESTED), WORKER_TRANSITIONS)

    def test_a_secondary_failure_while_stopping_is_not_a_new_state(self) -> None:
        self.assertIs(worker_next_state(S.STOPPING, V.FAILURE), S.STOPPING)


if __name__ == "__main__":
    unittest.main()
