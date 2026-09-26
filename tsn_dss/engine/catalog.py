from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..domain.models import CatalogObject, CatalogObjectAlias


class CatalogConflictError(ValueError):
    pass


@dataclass(slots=True)
class CatalogRegistrationItem:
    catalog_object: CatalogObject
    aliases: list[CatalogObjectAlias] = field(default_factory=list)


@dataclass(slots=True)
class CatalogRegistrationReport:
    objects_created: list[str] = field(default_factory=list)
    objects_unchanged: list[str] = field(default_factory=list)
    aliases_created: list[str] = field(default_factory=list)
    aliases_unchanged: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.conflicts


_MESSIER_PATTERN = re.compile(r"^(?:m|messier)\s*0*([1-9][0-9]*)$")
_NGC_PATTERN = re.compile(r"^ngc\s*0*([1-9][0-9]*)$")
_WHITESPACE = re.compile(r"\s+")


def normalize_catalog_alias(value: str) -> str:
    """Normalize an astronomical designation or name for deterministic exact lookup."""
    normalized = _WHITESPACE.sub(" ", value.strip().casefold())
    if not normalized:
        return ""

    messier = _MESSIER_PATTERN.match(normalized)
    if messier:
        return f"messier:{int(messier.group(1))}"

    ngc = _NGC_PATTERN.match(normalized)
    if ngc:
        return f"ngc:{int(ngc.group(1))}"

    return normalized
