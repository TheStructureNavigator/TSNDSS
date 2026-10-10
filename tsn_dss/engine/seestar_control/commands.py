"""The four Seestar control commands and their fixed wire parameters (DB-05 Slice 1).

This is the complete allow-list. A command is identified by a TSN DSS kind id; the wire method and parameters are fixed here
and nowhere else. Callers cannot supply a method name or any parameter. The device behavior behind these methods was
observed on one S30 Pro, firmware 9.31, by the operator-run experiment (operator-reported, not an acceptance record).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

__all__ = ["ARM_DEPLOY", "ARM_PARK", "COMMANDS", "ControlCommand", "SCENERY_START", "SCENERY_STOP", "SCENERY_PARAMS", "wire_message"]

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


def wire_message(command: ControlCommand, message_id: int) -> dict[str, Any]:
    """A fresh message dict for ``command``. Parameters are copied from the fixed table (lists, not tuples)."""
    message: dict[str, Any] = {"id": message_id, "verify": True, "method": command.method}
    if command.params is not None:
        message["params"] = {k: list(v) if isinstance(v, tuple) else v for k, v in command.params.items()}
    return message
