"""Shared deterministic fixtures for DB-03 Wave 2 tests."""

from __future__ import annotations

from datetime import timedelta

from tsn_dss.engine.device_runtime.models import PreviewAvailability
from tsn_dss.engine.device_runtime.preview_freshness import FreshnessPolicy
from tsn_dss.engine.device_runtime.preview_manager import PreviewStreamManager
from tsn_dss.engine.device_runtime.preview_readiness import ReadinessEvidence, ReadinessGate
from tsn_dss.engine.device_runtime.preview_simulator import scenario_fresh
from tsn_dss.engine.device_runtime.support import ManualClock, SequentialIdGenerator

try:
    from device_runtime_support import connected_runtime
except ModuleNotFoundError:  # pragma: no cover
    from tests.device_runtime_support import connected_runtime

POLICY = FreshnessPolicy(max_age=timedelta(seconds=5), repeat_threshold=3, repeat_span=timedelta(seconds=4))
CAM_A, CAM_B = "cam_a", "cam_b"


class FakeEvidence:
    """Scripted ReadinessEvidenceProvider. Counts reads. Never touches a device."""

    def __init__(self, clock, connection) -> None:
        self.clock = clock
        self.connection = connection
        self.availability = {CAM_A: PreviewAvailability.AVAILABLE, CAM_B: PreviewAvailability.AVAILABLE}
        self.override: dict[str, object] = {}
        self.reads = 0

    def live(self, label, availability=None, **changes):
        values = dict(
            provider_id=self.connection.provider_id,
            connection_id=self.connection.connection_id,
            device_ref=self.connection.device.device_ref,
            source_label=label,
            availability=availability or self.availability[label],
            host_observed_at=self.clock(),
            simulated=True,
        )
        values.update(changes)
        return ReadinessEvidence(**values)

    def read_evidence(self, label):
        self.reads += 1
        item = self.override.get(label, "live")
        if isinstance(item, Exception):
            raise item
        if item == "live":
            return self.live(label)
        return item


class SourceFactory:
    """Builds simulated sources per camera from scripted queues; records every call."""

    def __init__(self) -> None:
        self.queues: dict[str, list] = {}
        self.calls: list[str] = []
        self.sources: list = []
        self.fail: Exception | None = None

    def __call__(self, label):
        self.calls.append(label)
        if self.fail is not None:
            raise self.fail
        queue = self.queues.get(label)
        source = queue.pop(0) if queue else scenario_fresh(50)
        self.sources.append(source)
        return source

    @property
    def total_open_calls(self) -> int:
        return sum(s.open_calls for s in self.sources)


class Rig:
    def __init__(self, *, cameras=(CAM_A, CAM_B), max_active=2, max_age=timedelta(seconds=10)) -> None:
        self.runtime, self.provider, self.connection = connected_runtime()
        self.runtime.refresh_evidence(self.connection)
        self.clock = ManualClock(step=timedelta(0))
        self.fail_clock = False
        self.evidence = FakeEvidence(self.clock, self.connection)
        self.factory = SourceFactory()
        self.manager = PreviewStreamManager(
            connection=self.connection,
            gate=ReadinessGate(self.evidence, max_age=max_age),
            source_factory=self.factory,
            cameras=cameras,
            clock=self.now,
            id_generator=SequentialIdGenerator(),
            policy=POLICY,
            max_active_streams=max_active,
        )

    def now(self):
        if self.fail_clock:
            raise RuntimeError("clock")
        return self.clock()

    def advance(self, seconds: float) -> None:
        self.clock.advance(timedelta(seconds=seconds))
