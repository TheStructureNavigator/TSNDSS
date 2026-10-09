"""Deterministic scripted preview source for tests and demos (DB-03 Wave 1).

Pure Python: no network, no decoder, no files, no randomness. A script is a
sequence of *segments*; every ``open()`` consumes the next segment, which is how
a reopen after a loss is simulated. Within a segment each ``read()`` consumes the
next step. When a segment's steps are exhausted, reads return ``None`` (silence).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .preview_models import PixelFormat, PreviewPixels, PreviewSourceError, SourceImage

__all__ = [
    "FaultStep",
    "ImageStep",
    "ScriptSegment",
    "SilenceStep",
    "SimulatedPreviewSource",
    "make_pixels",
    "scenario_fresh",
    "scenario_loss",
    "scenario_reopen",
    "scenario_repeat",
    "scenario_stall",
]


def make_pixels(seed: int = 0, width: int = 8, height: int = 8, pixel_format: PixelFormat = PixelFormat.GRAY8) -> PreviewPixels:
    """Deterministic test pattern. Equal arguments give identical bytes; different seeds differ."""
    count = width * height * pixel_format.bytes_per_pixel
    data = bytes((seed * 37 + index * 11 + 5) % 256 for index in range(count))
    return PreviewPixels(width, height, pixel_format, data)


@dataclass(slots=True, frozen=True)
class ImageStep:
    seed: int = 0
    width: int = 8
    height: int = 8
    pixel_format: PixelFormat = PixelFormat.GRAY8
    provider_reported_at: datetime | None = None

    def image(self) -> SourceImage:
        return SourceImage(make_pixels(self.seed, self.width, self.height, self.pixel_format), self.provider_reported_at)


@dataclass(slots=True, frozen=True)
class SilenceStep:
    """One read that yields nothing."""


@dataclass(slots=True, frozen=True)
class FaultStep:
    category: str = "source_error"


Step = ImageStep | SilenceStep | FaultStep


@dataclass(slots=True, frozen=True)
class ScriptSegment:
    steps: tuple[Step, ...] = ()
    open_failure: str | None = None


class SimulatedPreviewSource:
    simulated = True

    def __init__(self, segments: list[ScriptSegment] | tuple[ScriptSegment, ...]) -> None:
        self._segments = tuple(segments)
        self._segment = -1
        self._cursor = 0
        self._is_open = False
        self.open_calls = 0
        self.read_calls = 0
        self.close_calls = 0

    @property
    def is_open(self) -> bool:
        return self._is_open

    def open(self) -> None:
        self.open_calls += 1
        if self._is_open:
            raise PreviewSourceError("already_open")
        self._segment += 1
        self._cursor = 0
        if self._segment >= len(self._segments):
            raise PreviewSourceError("no_script")
        failure = self._segments[self._segment].open_failure
        if failure is not None:
            raise PreviewSourceError(failure)
        self._is_open = True

    def read(self) -> SourceImage | None:
        self.read_calls += 1
        if not self._is_open:
            raise PreviewSourceError("not_open")
        steps = self._segments[self._segment].steps
        if self._cursor >= len(steps):
            return None
        step = steps[self._cursor]
        self._cursor += 1
        if isinstance(step, ImageStep):
            return step.image()
        if isinstance(step, FaultStep):
            raise PreviewSourceError(step.category)
        return None

    def close(self) -> None:
        self.close_calls += 1
        self._is_open = False


# --- scenarios ---------------------------------------------------------------------


def _segment(*steps: Step) -> ScriptSegment:
    return ScriptSegment(steps=tuple(steps))


def scenario_fresh(count: int = 3, *, with_provider_time: bool = False, origin: datetime | None = None) -> SimulatedPreviewSource:
    """``count`` distinct images; optionally with an advancing source-reported time."""
    steps = [
        ImageStep(seed=i, provider_reported_at=(origin + timedelta(seconds=i)) if with_provider_time and origin else None)
        for i in range(count)
    ]
    return SimulatedPreviewSource([_segment(*steps)])


def scenario_repeat(count: int = 8, *, provider_time: datetime | None = None, advancing: bool = False) -> SimulatedPreviewSource:
    """``count`` byte-identical images. With ``provider_time`` they carry a source time, frozen unless ``advancing``."""
    steps = []
    for i in range(count):
        stamp = None if provider_time is None else provider_time + (timedelta(seconds=i) if advancing else timedelta(0))
        steps.append(ImageStep(seed=1, provider_reported_at=stamp))
    return SimulatedPreviewSource([_segment(*steps)])


def scenario_stall(images_before: int = 2) -> SimulatedPreviewSource:
    """A few images, then silence forever (the source stays open but stops delivering)."""
    return SimulatedPreviewSource([_segment(*[ImageStep(seed=i) for i in range(images_before)])])


def scenario_loss(images_before: int = 2, category: str = "link_down") -> SimulatedPreviewSource:
    """A few images, then a source fault."""
    return SimulatedPreviewSource([_segment(*[ImageStep(seed=i) for i in range(images_before)], FaultStep(category))])


def scenario_reopen(images_before: int = 2, images_after: int = 2) -> SimulatedPreviewSource:
    """Images, a fault, then a second segment that delivers again after a new open."""
    first = _segment(*[ImageStep(seed=i) for i in range(images_before)], FaultStep("link_down"))
    second = _segment(*[ImageStep(seed=100 + i) for i in range(images_after)])
    return SimulatedPreviewSource([first, second])
