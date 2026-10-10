"""Baseline and recovery rules for the real Seestar (DB-05 Slice 2).

Both answer one question: *is the device's current physical state known, settled and consistent enough to start from?*
They never answer "did the earlier command work?": the evidence is provider-reported telemetry about the present, the
original ``unknown_result`` stays exactly as it was, and resolving an uncertainty authorizes nothing. New commands still need
the injected authorizer, fresh evidence and the kind's value constraints.

Accepted state (all values KNOWN, from this Connection's fresh sample):

* the mount is stationary and its arm position is known (open or closed), and
* the cameras are either both ready (scenery/RTSP working on their ports) with the arm open, or all four camera items stopped.

Everything else is refused: a moving or unknown mount, unknown or missing values, one camera ready and the other not, a
partly stopped pair, unfamiliar camera words, and cameras running with the arm closed (inconsistent).
"""

from __future__ import annotations

from ..device_runtime.uncertainty import BaselineRecovery, RecoveryVerdict
from .commands import COMMANDS
from .kinds import ControlFreshness
from ..device_runtime.command_models import FreshnessRequirement
from .states import ARM_CLOSED, CAMERA_ITEMS, MOVE_TYPE, STATIONARY, STOPPED_STATES, arm_closed, arm_stationary, cameras_ready, cameras_stopped_count

__all__ = ["build_recovery_assessors", "observed_state_assessor", "seestar_baseline_recovery"]

_PROVIDER_REPORTED = "provider_reported"
_BASIS = tuple((item, _PROVIDER_REPORTED) for item in (MOVE_TYPE, ARM_CLOSED, *CAMERA_ITEMS))
_NOT_RESOLVED = RecoveryVerdict(False)


def observed_state_assessor(connection, record, snapshot, now) -> RecoveryVerdict:
    """Resolve only when the present state is known, settled and consistent. ``record`` is ``None`` for a baseline."""
    sample = getattr(snapshot, "telemetry", None)
    if sample is None or arm_stationary(sample) is not True:
        return _NOT_RESOLVED
    closed = arm_closed(sample)
    if closed is None:
        return _NOT_RESOLVED
    if cameras_ready(sample):
        if closed:
            return _NOT_RESOLVED  # cameras serving with the arm folded is inconsistent
        cameras = "MAIN and WIDE ready"
    elif cameras_stopped_count(sample) == len(CAMERA_ITEMS):
        cameras = "cameras stopped"
    else:
        return _NOT_RESOLVED
    arm = "closed" if closed else "open"
    return RecoveryVerdict(
        True,
        f"current state observed (provider-reported): arm {arm} and stationary, {cameras}; "
        "this says nothing about whether any earlier command took effect",
        _BASIS,
    )


def seestar_baseline_recovery(freshness: ControlFreshness) -> BaselineRecovery:
    age = freshness.telemetry_max_age
    camera_words = ("working", *STOPPED_STATES)
    return BaselineRecovery(
        observed_state_assessor,
        (
            FreshnessRequirement(MOVE_TYPE, age, STATIONARY),
            FreshnessRequirement(ARM_CLOSED, age, (True, False)),
            *(FreshnessRequirement(item, age, camera_words) for item in CAMERA_ITEMS),
        ),
    )


def build_recovery_assessors() -> dict:
    return {kind_id: observed_state_assessor for kind_id in COMMANDS}
