"""Effect verification types (DSS-CTR-013 section 9; DB-04 S4).

``succeeded`` needs a verified effect: fresh post-command evidence, or, for a Command that cannot
produce a physical effect, a requirement-specific verification of the acknowledgement. A Provider's
acknowledgement or its own "complete" report is never enough. A kind with no registered verifier can
therefore never reach ``succeeded``. The verifier owns the freshness of the evidence it relies on.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

__all__ = ["EffectVerdict", "EffectVerdictKind", "EffectVerifier"]


class EffectVerdictKind(Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    VERIFIED_BY_ACKNOWLEDGEMENT = "verified_by_acknowledgement"  # non-physical kinds only
    FAILED = "failed"


@dataclass(slots=True, frozen=True)
class EffectVerdict:
    kind: EffectVerdictKind
    evidence: str = ""


# (connection, command_record) -> EffectVerdict
EffectVerifier = Callable[[object, object], EffectVerdict]
