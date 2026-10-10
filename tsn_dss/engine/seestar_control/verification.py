"""Effect verification for the four commands (DB-05 Slice 2).

A Provider acknowledgement or its ``REPORTED_COMPLETE`` is never success. ``succeeded`` needs a verdict from one of these
verifiers, and a verdict needs fresh telemetry that was observed **after** the command's possible-submission boundary (the
time the Command became ``submitted``). Telemetry is provider-reported evidence (REQ-061) and is labeled as such; it is the
best evidence available and it does not prove anything about an earlier command.

Rules, all requiring KNOWN values and the Connection's own sample:

* ``seestar.arm.deploy``     arm open and stationary
* ``seestar.scenery.start``  MAIN and WIDE in scenery/RTSP working state on their own ports, arm open and stationary
* ``seestar.scenery.stop``   both cameras and both RTSP streams in a stopped word
* ``seestar.arm.park``       arm closed and stationary

Anything else, including unreadable, unknown, partial, stale or pre-boundary evidence, is ``PENDING``. A verifier never returns
``FAILED``: a state that does not (yet) match is not proof that the command failed.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from ..device_runtime import DeviceRuntimeError, TelemetrySample
from ..device_runtime.command_effects import EffectVerdict, EffectVerdictKind
from .commands import ARM_DEPLOY, ARM_PARK, COMMANDS, GOTO, SCENERY_START, SCENERY_STOP, GotoTarget, angular_separation_deg
from .kinds import ControlFreshness
from .states import CAMERA_ITEMS, arm_closed, arm_stationary, cameras_ready, cameras_stopped_count

__all__ = ["build_goto_verifier", "build_verifiers", "submission_boundary"]

_PENDING = EffectVerdict(EffectVerdictKind.PENDING, "no verifying evidence yet")


def submission_boundary(record) -> datetime | None:
    """When the Command became ``submitted`` (the possible-submission boundary), or ``None`` if it never did."""
    for entry in reversed(record.history):
        if entry.to_state == "submitted":
            return entry.at
    return None


def _target_reached(kind_id: str, sample: TelemetrySample) -> str | None:
    """A short description of the verified target state, or ``None`` if the sample does not show it."""
    stationary, closed = arm_stationary(sample), arm_closed(sample)
    if kind_id == ARM_DEPLOY and stationary is True and closed is False:
        return "arm open and stationary"
    if kind_id == ARM_PARK and stationary is True and closed is True:
        return "arm closed and stationary"
    if kind_id == SCENERY_START and stationary is True and closed is False and cameras_ready(sample):
        return "arm open and stationary; MAIN and WIDE scenery/RTSP working on their ports"
    if kind_id == SCENERY_STOP and cameras_stopped_count(sample) == len(CAMERA_ITEMS):
        return "both cameras and RTSP streams stopped"
    return None


def build_verifiers(runtime, freshness: ControlFreshness, clock: Callable[[], datetime]) -> dict:
    """One ``EffectVerifier`` per kind. ``clock`` must be the clock the runtime and the executor use."""

    def make(kind_id: str):
        def verify(connection, record) -> EffectVerdict:
            boundary = submission_boundary(record)
            if boundary is None or record.policy is None or record.policy.kind_id != kind_id:
                return _PENDING
            try:
                sample = runtime.read_telemetry(connection)  # a passive read, taken now
            except DeviceRuntimeError:
                return _PENDING
            now = clock()
            if sample.connection_id != connection.connection_id or sample.provider_id != record.provider_id:
                return _PENDING
            if not sample.host_observed_at > boundary:  # must be observed strictly after the possible-submission boundary
                return _PENDING
            if not timedelta_ok(now - sample.host_observed_at, freshness):
                return _PENDING
            reached = _target_reached(kind_id, sample)
            if reached is None:
                return _PENDING
            return EffectVerdict(
                EffectVerdictKind.VERIFIED,
                f"provider-reported telemetry observed after submission shows {reached}",
            )

        return verify

    return {kind_id: make(kind_id) for kind_id in COMMANDS}


def build_goto_verifier(runtime, completion_of, coordinates, freshness: ControlFreshness, clock: Callable[[], datetime]):
    """GoTo effect verifier.

    ``VERIFIED`` means *firmware-reported completion*, not confirmed target centering. It needs (1) an outer ``ScopeGoto`` event with state
    ``complete`` on the issuing connection, seen after the possible-submission boundary, and (2) a fresh post-submission sample showing the
    mount stationary. The device's own coordinates are an optional cross-check: when a tolerance is configured and a fresh post-submission
    reading exists it must agree with the target (same provisional frame), and a disagreement is contradictory evidence, so the verdict
    stays ``PENDING`` (then ``unknown_result`` at the deadline). ``fail``, ``cancel``, a lost connection, a missing event or any doubt never
    verify. The verifier never returns ``FAILED``."""

    def verify(connection, record) -> EffectVerdict:
        boundary, target = submission_boundary(record), record.parameters
        if boundary is None or not isinstance(target, GotoTarget):
            return _PENDING
        if record.policy is None or record.policy.kind_id != GOTO:
            return _PENDING
        completion = completion_of(record.command_id)
        if completion is None or completion.state != "complete" or not completion.observed_at > boundary:
            return _PENDING
        tolerance, reading = freshness.goto_tolerance_deg, None
        try:
            sample = runtime.read_telemetry(connection)
        except DeviceRuntimeError:
            return _PENDING
        if tolerance is not None:
            try:
                reading = coordinates(connection)
            except DeviceRuntimeError:
                reading = None  # the cross-check is optional: an unreadable coordinate is "not cross-checked", never a verification
        now = clock()  # after the evidence was read, never before
        if sample.connection_id != connection.connection_id or sample.provider_id != record.provider_id:
            return _PENDING
        if not sample.host_observed_at > boundary or not timedelta_ok(now - sample.host_observed_at, freshness):
            return _PENDING
        if arm_stationary(sample) is not True:
            return _PENDING
        note = "coordinates not cross-checked"
        if tolerance is not None:
            if reading is not None and reading.observed_at > boundary and timedelta_ok(now - reading.observed_at, freshness):
                separation = angular_separation_deg(reading.ra_hours, reading.dec_deg, target.ra_hours, target.dec_deg)
                if separation > tolerance:
                    return _PENDING  # the firmware says complete but its own coordinates disagree: contradictory
                note = f"device-reported coordinates within {tolerance} deg of the target (provisional shared frame)"
        return EffectVerdict(
            EffectVerdictKind.VERIFIED,
            f"firmware-reported ScopeGoto complete after submission and mount stationary; {note}; target centering is NOT confirmed",
        )

    return verify


def timedelta_ok(age: timedelta, freshness: ControlFreshness) -> bool:
    """True when ``age`` is not negative and not older than the integrator's telemetry window."""
    return timedelta(0) <= age <= freshness.telemetry_max_age
