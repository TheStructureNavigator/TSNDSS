"""Offline fixtures for DB-03 Wave 3: DB-02 provider over a fake transport, fake decoders.

Nothing here opens a socket, runs a decoder library or reads a key.
"""

from __future__ import annotations

import copy
from datetime import timedelta

from tsn_dss.engine.device_runtime import ManualClock, ProviderRuntime, SequentialIdGenerator
from tsn_dss.engine.device_runtime.preview_freshness import FreshnessPolicy
from tsn_dss.engine.device_runtime.preview_simulator import make_pixels
from tsn_dss.engine.seestar_preview import DecoderError, SeestarPreviewConfig, build_preview_manager
from tsn_dss.engine.seestar_provider import SeestarProvider

try:
    from seestar_support import HOST, FakeSeestarTransport, make_config
except ModuleNotFoundError:  # pragma: no cover
    from tests.seestar_support import HOST, FakeSeestarTransport, make_config

POLICY = FreshnessPolicy(max_age=timedelta(seconds=5), repeat_threshold=3, repeat_span=timedelta(seconds=4))
VIEW_KEY = {"main": "View", "wide": "SecondView"}
PORT = {"main": 4554, "wide": 4555}


class FakeDecoder:
    """Scripted ImageDecoder. Declares itself simulated. Records every call, including endpoints."""

    simulated = True

    def __init__(self, camera: str, script=None, *, open_error: Exception | None = None, close_error: Exception | None = None):
        self.camera = camera
        self.script = list(script) if script is not None else [make_pixels(i) for i in range(50)]
        self.open_error = open_error
        self.close_error = close_error
        self.open_calls = self.read_calls = self.close_calls = 0
        self.endpoints: list = []

    def open(self, endpoint) -> None:
        self.open_calls += 1
        self.endpoints.append(endpoint)
        if self.open_error is not None:
            raise self.open_error

    def read(self):
        self.read_calls += 1
        if not self.script:
            return None
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class DecoderFactory:
    """Creates FakeDecoders per camera from scripted queues; records every creation."""

    def __init__(self) -> None:
        self.queues: dict[str, list] = {}
        self.calls: list[str] = []
        self.decoders: list[FakeDecoder] = []

    def __call__(self, camera: str) -> FakeDecoder:
        self.calls.append(camera)
        queue = self.queues.get(camera)
        decoder = queue.pop(0) if queue else FakeDecoder(camera)
        self.decoders.append(decoder)
        return decoder

    def for_camera(self, camera: str) -> list[FakeDecoder]:
        return [d for d in self.decoders if d.camera == camera]


class Flow:
    """DB-02 provider (fake transport) -> runtime -> Connection -> manager with fake decoders."""

    def __init__(self, *, config: SeestarPreviewConfig | None = None, max_age_s: float = 10.0) -> None:
        self.clock = ManualClock(step=timedelta(0))
        self.transport = FakeSeestarTransport()
        self.provider = SeestarProvider(make_config(), self.transport, clock=self.clock)
        self.runtime = ProviderRuntime(self.provider, clock=self.clock, id_generator=SequentialIdGenerator())
        device = self.runtime.discover().devices[0]
        self.connection = self.runtime.connect(self.runtime.open_connection(device))
        self.runtime.refresh_evidence(self.connection)
        self.config = config or SeestarPreviewConfig(host=HOST, readiness_max_age_s=max_age_s)
        self.decoders = DecoderFactory()
        self.calls_before_build = len(self.transport.calls)
        self.manager = build_preview_manager(
            runtime=self.runtime, connection=self.connection, config=self.config,
            decoder_factory=self.decoders, clock=self.clock, id_generator=SequentialIdGenerator(), policy=POLICY,
        )
        self.calls_after_build = len(self.transport.calls)

    def advance(self, seconds: float) -> None:
        self.clock.advance(timedelta(seconds=seconds))

    def app_view(self, camera: str) -> dict:
        return self.transport.app_reply["result"][VIEW_KEY[camera]]

    def set_camera_ready(self, camera: str, ready: bool = True) -> None:
        view = self.app_view(camera)
        if ready:
            view.update(state="working", mode="scenery", stage="RTSP")
            view["RTSP"] = {"state": "working", "port": PORT[camera]}
        else:
            view.update(state="idle", mode="none", stage="Idle")
            view["RTSP"] = {"state": "stopped", "port": PORT[camera]}

    def drop_camera_block(self, camera: str) -> None:
        self.transport.app_reply["result"].pop(VIEW_KEY[camera], None)

    def copy_app_result(self) -> dict:
        return copy.deepcopy(self.transport.app_reply["result"])


def decoder_error(category: str = "stream_ended") -> DecoderError:
    return DecoderError(category)
