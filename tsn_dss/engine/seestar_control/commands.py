"""The four Seestar control commands and their fixed wire parameters (DB-05 Slice 1).

This is the complete allow-list. A command is identified by a TSN DSS kind id; the wire method and parameters are fixed here
and nowhere else. Callers cannot supply a method name or any parameter. The device behavior behind these methods was
observed on one S30 Pro, firmware 9.31, by the operator-run experiment (operator-reported, not an acceptance record).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

__all__ = ["GOTO", "GOTO_PHYSICAL_ENABLED", "GotoTarget", "MountCoordinates", "angular_separation_deg", "goto_wire_message", "ARM_DEPLOY", "ARM_PARK", "COMMANDS", "ControlCommand", "SCENERY_START", "SCENERY_STOP", "SCENERY_PARAMS", "wire_message"]

ARM_DEPLOY = "seestar.arm.deploy"
SCENERY_START = "seestar.scenery.start"
SCENERY_STOP = "seestar.scenery.stop"
ARM_PARK = "seestar.arm.park"

# Fixed scenery parameters (MAIN and WIDE, cam_id=1). Never built from caller input.
SCENERY_PARAMS: Mapping[str, Any] = MappingProxyType(
    {"mode": "scenery", "target_ra_dec": (None, None), "target_name": "Unknown", "lp_filter": False, "cam_id": 1}
)


@dataclass(slots=True, frozen=True)
class ControlCommand:
    kind_id: str
    method: str
    params: Mapping[str, Any] | None = None
    result_must_be_zero: bool = False  # the device answers the start request with result 0 when it accepts it


COMMANDS: Mapping[str, ControlCommand] = MappingProxyType(
    {
        ARM_DEPLOY: ControlCommand(ARM_DEPLOY, "scope_move_to_horizon"),
        SCENERY_START: ControlCommand(SCENERY_START, "iscope_start_view", SCENERY_PARAMS, result_must_be_zero=True),
        SCENERY_STOP: ControlCommand(SCENERY_STOP, "iscope_stop_view"),
        ARM_PARK: ControlCommand(ARM_PARK, "scope_park"),
    }
)


# --- GoTo (DB-05b): the one command with a caller-supplied parameter, a validated immutable target --------------------------------
GOTO = "seestar.mount.goto"

# Physical GoTo is BLOCKED. The provider refuses before any frame is sent unless this is True. It stays False until the owner has decided
# the open safety questions (coordinate frame/epoch, pointing and horizon limits, completion evidence, tolerance); tests set it only to
# exercise the offline path against the simulator.
GOTO_PHYSICAL_ENABLED = False


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number.")
    return float(value)


@dataclass(slots=True, frozen=True)
class GotoTarget:
    """A requested pointing, exactly as the third-party protocol reference documents the arguments: RA in decimal hours (0-24) and Dec in
    decimal degrees (-90..90). The coordinate frame/epoch is NOT known and is never assumed or converted here."""

    ra_hours: float
    dec_deg: float

    def __post_init__(self) -> None:
        ra, dec = _finite_number(self.ra_hours, "ra_hours"), _finite_number(self.dec_deg, "dec_deg")
        if not 0.0 <= ra <= 24.0:
            raise ValueError("ra_hours must be within 0..24.")
        if not -90.0 <= dec <= 90.0:
            raise ValueError("dec_deg must be within -90..90.")
        object.__setattr__(self, "ra_hours", ra)
        object.__setattr__(self, "dec_deg", dec)


@dataclass(slots=True, frozen=True)
class MountCoordinates:
    """What the device reported for its current pointing, in its own frame, and when this process read it."""

    ra_hours: float
    dec_deg: float
    observed_at: object


def angular_separation_deg(ra1_hours: float, dec1_deg: float, ra2_hours: float, dec2_deg: float) -> float:
    """Great-circle separation in degrees between two (RA hours, Dec degrees) points taken as being in the same frame."""
    a1, a2 = math.radians(ra1_hours * 15.0), math.radians(ra2_hours * 15.0)
    d1, d2 = math.radians(dec1_deg), math.radians(dec2_deg)
    h = math.sin((d2 - d1) / 2) ** 2 + math.cos(d1) * math.cos(d2) * math.sin((a2 - a1) / 2) ** 2
    return math.degrees(2 * math.asin(min(1.0, math.sqrt(h))))


def goto_wire_message(target: GotoTarget, message_id: int) -> dict[str, Any]:
    """The GoTo request for a validated target (positional ``[RA_hours, Dec_degrees]``, as the protocol reference documents)."""
    return {"id": message_id, "verify": True, "method": "scope_goto", "params": [target.ra_hours, target.dec_deg]}


def wire_message(command: ControlCommand, message_id: int) -> dict[str, Any]:
    """A fresh message dict for ``command``. Parameters are copied from the fixed table (lists, not tuples)."""
    message: dict[str, Any] = {"id": message_id, "verify": True, "method": command.method}
    if command.params is not None:
        message["params"] = {k: list(v) if isinstance(v, tuple) else v for k, v in command.params.items()}
    return message
