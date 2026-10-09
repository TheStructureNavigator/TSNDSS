"""Wiring: DB-02 evidence -> readiness gate -> manager -> injected decoder."""

from __future__ import annotations

from datetime import timedelta
from typing import Callable

from ..device_runtime import Connection
from ..device_runtime.preview_freshness import FreshnessPolicy
from ..device_runtime.preview_manager import PreviewStreamManager
from ..device_runtime.preview_models import ImageLimits
from ..device_runtime.preview_readiness import ReadinessGate
from ..device_runtime.support import Clock, IdGenerator, random_id_generator, utc_now
from .config import SeestarPreviewConfig
from .readiness import SeestarReadinessEvidenceProvider, TelemetryReader
from .source import ImageDecoder, make_source_factory

__all__ = ["build_preview_manager"]


def build_preview_manager(
    *,
    runtime: TelemetryReader,
    connection: Connection,
    config: SeestarPreviewConfig,
    decoder_factory: Callable[[str], ImageDecoder],
    clock: Clock = utc_now,
    id_generator: IdGenerator = random_id_generator,
    policy: FreshnessPolicy | None = None,
    limits: ImageLimits | None = None,
) -> PreviewStreamManager:
    """Build a manager for ``connection``. Nothing is read, opened or started by building it.

    ``clock`` must be the same clock the runtime uses to stamp telemetry, so that evidence age is
    meaningful; a mismatch can only make the gate deny (future-dated or stale evidence).
    """
    gate = ReadinessGate(
        SeestarReadinessEvidenceProvider(runtime, connection, cameras=config.cameras),
        max_age=timedelta(seconds=config.readiness_max_age_s),
    )
    return PreviewStreamManager(
        connection=connection,
        gate=gate,
        source_factory=make_source_factory(config, decoder_factory),
        cameras=config.cameras,
        clock=clock,
        id_generator=id_generator,
        policy=policy,
        limits=limits,
    )
