"""Seestar control commands behind the DB-04 Safe Command Runtime (ROADMAP_DEVICE_BACKEND.md DB-05, Slice 1).

Vendor-specific, outside the vendor-neutral ``device_runtime`` and separate from the read-only ``seestar_provider`` (whose
acceptance and boundary tests are unaffected). Importing this package performs no network access and reads no key. Nothing
here is wired to a hardware path yet: it is exercised only against scripted fakes. Slice 2 adds the composition root (``SeestarControl``), effect verifiers, recovery rules and a deterministic driver.
"""

from .commands import ARM_DEPLOY, ARM_PARK, COMMANDS, SCENERY_PARAMS, SCENERY_START, SCENERY_STOP, ControlCommand
from .composition import ControlAttachError, ControlHandle, SeestarControl
from .driver import CommandDriver, CommandOutcome
from .errors import ControlPostSendError, ControlPreSendError
from .kinds import ControlFreshness, build_command_kinds, register_command_kinds
from .provider import SeestarCommandProvider
from .transport import SeestarControlTransport

__all__ = [
    "ARM_DEPLOY", "ARM_PARK", "COMMANDS", "CommandDriver", "CommandOutcome", "ControlAttachError", "ControlCommand",
    "ControlFreshness", "ControlHandle", "ControlPostSendError", "ControlPreSendError", "SCENERY_PARAMS", "SCENERY_START",
    "SCENERY_STOP", "SeestarCommandProvider", "SeestarControl", "SeestarControlTransport", "build_command_kinds",
    "register_command_kinds",
]
