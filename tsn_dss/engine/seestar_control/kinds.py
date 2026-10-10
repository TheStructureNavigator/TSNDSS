"""Command kind policies for the four Seestar commands (DB-05 Slice 1).

Freshness windows are not defined here: the caller of ``build_command_kinds`` supplies both explicitly (``ControlFreshness``
has no defaults). Values constrain the telemetry the gate must see before the command may be submitted (DB-04 Slice 0):

=======================  =====================================================================================================
kind                     required, fresh and known telemetry
=======================  =====================================================================================================
seestar.arm.deploy       mount stationary; arm folded
seestar.scenery.start    mount stationary; arm open; every camera item in a stopped word (conservative, see below)
seestar.scenery.stop     every camera item known (the camera state is what the command acts on; any value is accepted)
seestar.arm.park         mount stationary; arm open; every camera item in a stopped word
=======================  =====================================================================================================

Each also requires its own capability report entry to be fresh and available. All four are state-changing, physical,
non-idempotent and safety-sensitive (DB-04 invariants), so none is ever retried automatically.

The stopped-cameras requirement for ``scenery.start`` is stricter than the operator-run experiment (which started when the
cameras were not already serving). It is a conservative, fail-closed choice that hardware validation may relax.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from ..device_runtime.command_models import CAPABILITY_PREFIX, CommandKindPolicy, CommandKindRegistry, FreshnessRequirement
from .commands import ARM_DEPLOY, ARM_PARK, GOTO, SCENERY_START, SCENERY_STOP
from .states import ARM_CLOSED, CAMERA_ITEMS, MOVE_TYPE, STATIONARY, STOPPED_STATES

__all__ = ["ControlFreshness", "build_command_kinds", "build_goto_policy", "register_command_kinds"]


@dataclass(slots=True, frozen=True)
class ControlFreshness:
    """How old required evidence may be. Both windows are chosen by the integrator; there are no defaults."""

    telemetry_max_age: timedelta
    capability_max_age: timedelta
    goto_tolerance_deg: float | None = None  # how close the reported pointing must be to a GoTo target; None = GoTo cannot be verified

    def __post_init__(self) -> None:
        for name in ("telemetry_max_age", "capability_max_age"):
            value = getattr(self, name)
            if not isinstance(value, timedelta) or value <= timedelta(0):
                raise ValueError(f"{name} must be a positive timedelta.")
        tol = self.goto_tolerance_deg
        if tol is not None and (isinstance(tol, bool) or not isinstance(tol, (int, float)) or not 0 < tol <= 180 or tol != tol):
            raise ValueError("goto_tolerance_deg must be a number in (0, 180] or None.")


def build_command_kinds(freshness: ControlFreshness) -> tuple[CommandKindPolicy, ...]:
    age = freshness.telemetry_max_age

    def telemetry(item: str, allowed: tuple | None = None) -> FreshnessRequirement:
        return FreshnessRequirement(item, age, allowed)

    def capability(kind_id: str) -> FreshnessRequirement:
        return FreshnessRequirement(CAPABILITY_PREFIX + kind_id, freshness.capability_max_age)

    still = telemetry(MOVE_TYPE, STATIONARY)
    folded, open_arm = telemetry(ARM_CLOSED, (True,)), telemetry(ARM_CLOSED, (False,))
    cameras_stopped = tuple(telemetry(item, tuple(STOPPED_STATES)) for item in CAMERA_ITEMS)
    cameras_known = tuple(telemetry(item) for item in CAMERA_ITEMS)

    def policy(kind_id: str, *requirements: FreshnessRequirement) -> CommandKindPolicy:
        return CommandKindPolicy(
            kind_id, state_changing=True, physical=True, idempotent=False, safety_sensitive=True,
            freshness=(*requirements, capability(kind_id)),
        )

    return (
        policy(ARM_DEPLOY, still, folded),
        policy(SCENERY_START, still, open_arm, *cameras_stopped),
        policy(SCENERY_STOP, *cameras_known),
        policy(ARM_PARK, still, open_arm, *cameras_stopped),
    )


def build_goto_policy(freshness: ControlFreshness, parameter_gate=None) -> CommandKindPolicy:
    """GoTo: mount stationary and the arm open (the protocol reference says a goto cannot start from the parked position), plus its own
    capability, plus the target-dependent ``parameter_gate`` (altitude and Sun proximity, see ``pointing``). It deliberately says nothing
    about the cameras: no source shows that a slew depends on them. Takes a GotoTarget."""
    age = freshness.telemetry_max_age
    return CommandKindPolicy(
        GOTO, state_changing=True, physical=True, idempotent=False, safety_sensitive=True, takes_parameters=True,
        parameter_gate=parameter_gate,
        freshness=(FreshnessRequirement(MOVE_TYPE, age, STATIONARY), FreshnessRequirement(ARM_CLOSED, age, (False,)),
                   FreshnessRequirement(CAPABILITY_PREFIX + GOTO, freshness.capability_max_age)),
    )


def register_command_kinds(registry: CommandKindRegistry, freshness: ControlFreshness) -> None:
    for policy in build_command_kinds(freshness):
        registry.register(policy)
