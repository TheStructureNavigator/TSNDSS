"""A small deterministic driver over the DB-04 executor (DB-05 Slice 2).

``CommandDriver.execute`` runs ONE command through ``admit``, ``submit``, then ``poll`` and ``enforce_deadline`` until the
command is terminal, and returns a structured ``CommandOutcome``. It is not a second lifecycle: every transition is the
executor's. It never retries, never recovers an uncertainty, never clears one, and never picks a deadline or poll interval:
both are required arguments. The caller supplies the clock and sleep, so tests advance time themselves.

Each iteration polls first and then applies the deadline, so a verified success observed at the deadline is honored, and a
physical command whose deadline passes without verification becomes ``unknown_result`` (the executor's rule).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from ..device_runtime import CommandState, Connection
from ..device_runtime.command_executor import CommandExecutor, CommandIntent

__all__ = ["CommandDriver", "CommandOutcome"]

_CLASSIFICATION = {
    CommandState.SUCCEEDED: "succeeded",
    CommandState.FAILED: "failed",
    CommandState.UNKNOWN_RESULT: "unknown_result",
    CommandState.SAFETY_BLOCKED: "safety_blocked",
    CommandState.REJECTED: "rejected",
    CommandState.TIMED_OUT: "timed_out",
    CommandState.CANCELLED: "cancelled",
}


@dataclass(slots=True, frozen=True)
class CommandOutcome:
    """What happened to one command. Carries no address, key or device payload."""

    command_id: str
    kind_id: str
    final_state: str
    classification: str
    submitted: bool  # the possible-submission boundary was crossed
    polls: int
    last_evidence: str
    uncertainty_open: bool  # the device has an unresolved uncertainty after this command
    connection_state: str
    history: tuple[tuple[str, str, str, str], ...]  # (from_state, event, to_state, evidence)

    @property
    def succeeded(self) -> bool:
        return self.classification == "succeeded"

    @property
    def needs_recovery(self) -> bool:
        return self.uncertainty_open


class CommandDriver:
    def __init__(
        self,
        executor: CommandExecutor,
        *,
        clock: Callable[[], datetime],
        sleep: Callable[[float], None],
    ) -> None:
        self._executor = executor
        self._clock = clock
        self._sleep = sleep

    def execute(
        self,
        connection: Connection,
        kind_id: str,
        requested_by: str,
        *,
        deadline: datetime,
        poll_interval: timedelta,
        parameters: object = None,
    ) -> CommandOutcome:
        if not isinstance(poll_interval, timedelta) or poll_interval <= timedelta(0):
            raise ValueError("poll_interval must be a positive timedelta.")
        if not isinstance(deadline, datetime) or deadline.tzinfo is None:
            raise ValueError("deadline must be a timezone-aware datetime.")
        executor = self._executor
        record = executor.admit(CommandIntent(connection, kind_id, requested_by, deadline=deadline, parameters=parameters))
        polls = 0
        if not record.state.is_terminal:
            executor.submit(record.command_id)
            while not record.state.is_terminal:
                executor.poll(record.command_id)
                polls += 1
                if record.state.is_terminal:
                    break
                executor.enforce_deadline(record.command_id)
                if record.state.is_terminal:
                    break
                self._sleep(poll_interval.total_seconds())
        return self._outcome(record, connection, polls)

    def _outcome(self, record, connection, polls: int) -> CommandOutcome:
        history = tuple((h.from_state, h.event, h.to_state, h.evidence) for h in record.history)
        device = (record.provider_id, connection.device.device_ref)
        return CommandOutcome(
            command_id=record.command_id,
            kind_id=record.policy.kind_id if record.policy is not None else "",
            final_state=record.state.value,
            classification=_CLASSIFICATION[record.state],
            submitted=any(h[2] == "submitted" for h in history),
            polls=polls,
            last_evidence=history[-1][3] if history else "",
            uncertainty_open=device in self._executor.unresolved_devices(),
            connection_state=connection.state.value,
            history=history,
        )
