"""Injectable clock and identifier support (standard library only)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import uuid4

Clock = Callable[[], datetime]
IdGenerator = Callable[[str], str]


def utc_now() -> datetime:
    """Default clock: timezone-aware current UTC time."""
    return datetime.now(timezone.utc)


def random_id_generator(kind: str) -> str:
    """Default runtime identifier source, e.g. ``conn-3f9a1c0b7d2e``."""
    return f"{kind}-{uuid4().hex[:12]}"


class SequentialIdGenerator:
    """Deterministic identifier source: ``<kind>-0001``, ``<kind>-0002``, ..."""

    def __init__(self, prefix: str = "") -> None:
        self._prefix = prefix
        self._counters: dict[str, int] = {}

    def __call__(self, kind: str) -> str:
        count = self._counters.get(kind, 0) + 1
        self._counters[kind] = count
        return f"{self._prefix}{kind}-{count:04d}"


class ManualClock:
    """Deterministic clock. Every read advances time by ``step``."""

    def __init__(
        self,
        start: datetime | None = None,
        step: timedelta = timedelta(seconds=1),
    ) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
        if self._now.tzinfo is None or self._now.utcoffset() is None:
            raise ValueError("start must be timezone-aware.")
        self._step = step

    def __call__(self) -> datetime:
        current = self._now
        self._now = current + self._step
        return current

    def advance(self, delta: timedelta) -> None:
        self._now += delta
