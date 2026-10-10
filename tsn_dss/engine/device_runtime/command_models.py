"""Command kind policy descriptors and the kind registry (DSS-CTR-013 sections 9-10; DB-04 S1).

Provider-neutral and data only. A policy describes what a Command kind *is* (state-changing,
physical, idempotent, safety-sensitive) and which evidence it requires to be fresh. It carries no
vendor behavior and no freshness values of its own: every ``max_age`` is supplied by whoever
registers the kind (REQ-051). Nothing here submits, validates a request against a Provider or
decides an outcome; that belongs to later DB-04 slices.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .errors import DeviceRuntimeError
from .models import CommandRef


class CommandPolicyError(DeviceRuntimeError):
    """A Command kind policy is malformed or violates a DSS-CTR-013 invariant."""


class CommandKindAlreadyRegistered(DeviceRuntimeError):
    """A kind_id is already registered."""


class UnknownCommandKind(DeviceRuntimeError):
    """No Command kind is registered under the requested kind_id."""


@dataclass(slots=True, frozen=True)
class FreshnessRequirement:
    """One required Capability, Telemetry or physical-state item and how old its evidence may be (REQ-051).

    Naming convention (owner decision, DB-04 S3): a plain ``item`` names a telemetry item; an item
    prefixed ``capability:`` names a Capability Report entry (``capability:<capability name>``).
    ``max_age`` has no default and is chosen by whoever registers the kind.
    """

    item: str
    max_age: timedelta

    def __post_init__(self) -> None:
        if not isinstance(self.item, str) or not self.item.strip():
            raise CommandPolicyError("freshness item must be a non-empty string.")
        if not isinstance(self.max_age, timedelta) or self.max_age <= timedelta(0):
            raise CommandPolicyError("freshness max_age must be a positive timedelta.")


@dataclass(slots=True, frozen=True)
class CommandKindPolicy:
    """Command-kind-specific policy.

    Invariants enforced at construction (fail closed):

    * a physical Command is state-changing and safety-sensitive. This is a conservative design
      decision of this implementation: REQ-062 requires command-kind-specific safety gates for
      Commands that affect physical equipment but does not mandate this exact implication;
    * a safety-sensitive kind declares at least one freshness requirement (REQ-051);
    * only a kind that is safely idempotent may use an idempotency key (section 9), and a
      physical Command is never treated as idempotent here, so it is never auto-retried (REQ-027).
    """

    kind_id: str
    state_changing: bool
    physical: bool
    idempotent: bool
    safety_sensitive: bool
    freshness: tuple[FreshnessRequirement, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.kind_id, str) or not self.kind_id.strip():
            raise CommandPolicyError("kind_id must be a non-empty string.")
        for name in ("state_changing", "physical", "idempotent", "safety_sensitive"):
            if not isinstance(getattr(self, name), bool):
                raise CommandPolicyError(f"{name} must be a bool.")
        object.__setattr__(self, "freshness", tuple(self.freshness))
        if not all(isinstance(r, FreshnessRequirement) for r in self.freshness):
            raise CommandPolicyError("freshness must contain FreshnessRequirement items.")
        if len({r.item for r in self.freshness}) != len(self.freshness):
            raise CommandPolicyError("freshness items must be unique.")
        if self.physical and not (self.state_changing and self.safety_sensitive):
            raise CommandPolicyError("a physical kind must be state-changing and safety-sensitive.")
        if self.physical and self.idempotent:
            raise CommandPolicyError("a physical kind is never declared idempotent in this contract version.")
        if self.safety_sensitive and not self.freshness:
            raise CommandPolicyError("a safety-sensitive kind must declare freshness requirements.")


@dataclass(slots=True, frozen=True)
class CommandRequest:
    """A requested Command. ``requested_by`` names the authorized caller or approved procedure (REQ-021)."""

    ref: CommandRef
    kind_id: str
    requested_by: str
    requested_at: datetime
    deadline: datetime | None = None
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind_id, str) or not self.kind_id.strip():
            raise CommandPolicyError("kind_id must be a non-empty string.")
        if not isinstance(self.requested_by, str) or not self.requested_by.strip():
            raise CommandPolicyError("requested_by must be a non-empty string.")
        for name in ("requested_at", "deadline"):
            value = getattr(self, name)
            if value is None and name == "deadline":
                continue
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise CommandPolicyError(f"{name} must be a timezone-aware datetime.")
        if self.idempotency_key is not None and not self.idempotency_key.strip():
            raise CommandPolicyError("idempotency_key must be non-empty when given.")

    def violates_policy(self, policy: CommandKindPolicy) -> str | None:
        """A rejection reason when the request cannot match the policy, else ``None``. Pure."""
        if policy.kind_id != self.kind_id:
            return "kind_mismatch"
        if self.idempotency_key is not None and not policy.idempotent:
            return "idempotency_key_on_non_idempotent_kind"
        return None


class CommandKindRegistry:
    """Provider-neutral kind registry. Providers register the kinds they support; the core names none."""

    def __init__(self) -> None:
        self._kinds: dict[str, CommandKindPolicy] = {}

    def register(self, policy: CommandKindPolicy) -> None:
        if not isinstance(policy, CommandKindPolicy):
            raise CommandPolicyError("register expects a CommandKindPolicy.")
        if policy.kind_id in self._kinds:
            raise CommandKindAlreadyRegistered(policy.kind_id)
        self._kinds[policy.kind_id] = policy

    def get(self, kind_id: str) -> CommandKindPolicy:
        try:
            return self._kinds[kind_id]
        except KeyError:
            raise UnknownCommandKind(kind_id) from None

    def kind_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._kinds))
