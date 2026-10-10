"""Seestar control commands behind the DB-04 Safe Command Runtime (ROADMAP_DEVICE_BACKEND.md DB-05, Slice 1).

Vendor-specific, outside the vendor-neutral ``device_runtime`` and separate from the read-only ``seestar_provider`` (whose
acceptance and boundary tests are unaffected). Importing this package performs no network access and reads no key. Nothing
here is wired to a hardware path yet: this slice is exercised only against scripted fakes.
"""

from .commands import ARM_DEPLOY, ARM_PARK, COMMANDS, SCENERY_PARAMS, SCENERY_START, SCENERY_STOP, ControlCommand
from .errors import ControlPostSendError, ControlPreSendError
from .kinds import ControlFreshness, build_command_kinds, register_command_kinds
from .provider import SeestarCommandProvider
from .transport import SeestarControlTransport

__all__ = [
    "ARM_DEPLOY", "ARM_PARK", "COMMANDS", "ControlCommand", "ControlFreshness", "ControlPostSendError", "ControlPreSendError",
    "SCENERY_PARAMS", "SCENERY_START", "SCENERY_STOP", "SeestarCommandProvider", "SeestarControlTransport",
    "build_command_kinds", "register_command_kinds",
]
