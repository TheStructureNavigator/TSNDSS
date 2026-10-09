"""Fake ``cv2`` module, fake VideoCapture and fake frames. No OpenCV, no NumPy, no network."""

from __future__ import annotations

import threading


class FakeFrame:
    def __init__(self, shape, dtype="uint8", fill=7, data=None):
        self.shape = tuple(shape)
        self.dtype = dtype
        n = 1
        for part in self.shape:
            n *= part
        self._data = data if data is not None else bytes([fill % 256]) * n
        self.tobytes_calls = 0

    def tobytes(self):
        self.tobytes_calls += 1
        return self._data


def bgr(width=4, height=2, fill=7):
    return FakeFrame((height, width, 3), fill=fill)


class FakeCapture:
    """Scripted cv2.VideoCapture. Records construction arguments and every call."""

    instances: list = []

    def __init__(self, address, api, params):
        if FakeCv2.construct_error is not None:
            raise FakeCv2.construct_error  # a constructor that raises leaves no object behind
        self.address, self.api, self.params = address, api, list(params)
        port = address.rsplit(":", 1)[-1].split("/")[0]
        self.opened = FakeCv2.open_result and port not in FakeCv2.refusing_ports
        self.backend = FakeCv2.backend_name
        self.script = list(FakeCv2.scripts.get(port, FakeCv2.script))
        self.read_calls = self.release_calls = 0
        self.read_gate: threading.Event | None = FakeCv2.read_gate
        self.read_entered = threading.Event()
        FakeCapture.instances.append(self)

    def isOpened(self):
        if FakeCv2.isopened_error is not None:
            raise FakeCv2.isopened_error
        return self.opened

    def getBackendName(self):
        if FakeCv2.backend_error is not None:
            raise FakeCv2.backend_error
        return self.backend

    def read(self):
        self.read_calls += 1
        self.read_entered.set()
        if self.read_gate is not None:
            self.read_gate.wait(5)
        if not self.script:
            return False, None
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def release(self):
        self.release_calls += 1
        if FakeCv2.release_error is not None:
            raise FakeCv2.release_error


class FakeCv2:
    """Stand-in for the cv2 module. Class-level knobs are reset by ``reset``."""

    __version__ = "4.10.0"
    CAP_FFMPEG = 1900
    CAP_PROP_OPEN_TIMEOUT_MSEC = 53
    CAP_PROP_READ_TIMEOUT_MSEC = 54

    open_result = True
    backend_name = "FFMPEG"
    script: list = []
    scripts: dict = {}
    refusing_ports: set = set()
    read_gate = None
    construct_error = isopened_error = backend_error = release_error = None

    VideoCapture = FakeCapture

    @classmethod
    def reset(cls, **knobs):
        cls.__version__ = "4.10.0"
        cls.open_result, cls.backend_name, cls.script, cls.read_gate = True, "FFMPEG", [], None
        cls.scripts, cls.refusing_ports = {}, set()
        cls.construct_error = cls.isopened_error = cls.backend_error = cls.release_error = None
        FakeCapture.instances = []
        for key, value in knobs.items():
            setattr(cls, key, value)
        return cls


def loader():
    return FakeCv2
