"""Composition root for the isolated decoder (DB-03 Wave 4B-3c): the Wave 3 wiring with a ``ProcessIsolatedDecoder`` per camera.

``build_isolated_preview_manager`` is ``seestar_preview.build_preview_manager`` with one difference: the decoder factory is the
isolated one, and it always carries a ``before_connect`` that asks the very same readiness gate again after the worker is READY and
before ``OPEN``. The manager stays the only way to open a stream; nothing here opens, starts or controls anything, and it has no
command surface (DB-03 only reads a preview that already exists). One ``ImageLimits`` (``isolated.limits``) is authoritative for the
shared slot, the worker and the manager. MAIN and WIDE each get their own decoder and worker (at most two, decision D10).

The re-check uses the same evidence provider and gate as the manager, without ``not_before`` (the manager applied it when it
opened the stream); a failing check or clock fails closed (``readiness_revoked``).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable

from ..device_runtime import Connection
from ..device_runtime.preview_freshness import FreshnessPolicy
from ..device_runtime.preview_manager import PreviewStreamManager
from ..device_runtime.preview_readiness import ReadinessGate, ReadinessIdentity
from ..device_runtime.support import Clock, IdGenerator, random_id_generator, utc_now
from ..seestar_preview.config import SeestarPreviewConfig
from ..seestar_preview.readiness import SeestarReadinessEvidenceProvider, TelemetryReader
from ..seestar_preview.source import make_source_factory
from .decoder import IsolatedDecoderConfig, make_isolated_decoder_factory
from .process import WorkerProcess

__all__ = ["build_isolated_preview_manager"]


def build_isolated_preview_manager(
    *,
    runtime: TelemetryReader,
    connection: Connection,
    config: SeestarPreviewConfig,
    isolated: IsolatedDecoderConfig | None = None,
    clock: Clock = utc_now,
    id_generator: IdGenerator = random_id_generator,
    policy: FreshnessPolicy | None = None,
    _worker_factory: Callable[..., Any] = WorkerProcess,
    _python_version: Any = None,
) -> PreviewStreamManager:
    """Build a manager whose streams are decoded in isolated workers. Nothing is read, opened or started by building it."""
    isolated = isolated or IsolatedDecoderConfig()
    gate = ReadinessGate(
        SeestarReadinessEvidenceProvider(runtime, connection, cameras=config.cameras),
        max_age=timedelta(seconds=config.readiness_max_age_s),
    )
    identity = ReadinessIdentity(
        provider_id=connection.provider_id, connection_id=connection.connection_id, device_ref=connection.device.device_ref
    )

    def still_allowed(camera: str | None) -> bool:
        return bool(camera) and gate.check(identity, camera, clock()).allowed

    decoder_factory = make_isolated_decoder_factory(
        isolated, before_connect=still_allowed, _worker_factory=_worker_factory, _python_version=_python_version
    )
    return PreviewStreamManager(
        connection=connection,
        gate=gate,
        source_factory=make_source_factory(config, decoder_factory),
        cameras=config.cameras,
        clock=clock,
        id_generator=id_generator,
        policy=policy,
        limits=isolated.limits,
    )
