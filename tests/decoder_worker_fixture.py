"""Test-only entry for DB-03 Wave 4B-3a: the REAL ``DecoderHandler`` and the REAL Wave 4 adapter, with a FAKE ``cv2``.

``python -P -m tests.decoder_worker_fixture --ctl H --proto 1 --fixture NAME [--report FILE]``

The fake module (``tests/opencv_fakes.py``) is injected through the handler's ``decoder_factory`` seam; production code has no
way to select it. No OpenCV, no NumPy, no network. ``--report`` is a test-only file in which this process records what the
fake capture received (so a test can prove the address arrived once and where) and what was released.
"""

from __future__ import annotations

import json
import os
import sys
import time

from tests.opencv_fakes import FakeCapture, FakeCv2, FakeFrame, bgr
from tsn_dss.engine.opencv_isolated_decoder import protocol as P
from tsn_dss.engine.opencv_isolated_decoder.worker_decoder import DecoderHandler
from tsn_dss.engine.opencv_isolated_decoder.worker_main import _adopt, serve


def hang() -> None:
    time.sleep(10 ** 6)


def spin() -> None:
    while True:
        pass


class HangReadCapture(FakeCapture):
    def read(self):
        hang()


class SpinReadCapture(FakeCapture):
    def read(self):
        spin()


class CrashReadCapture(FakeCapture):
    def read(self):
        os._exit(139)


class HangConstructCapture(FakeCapture):
    def __init__(self, *a, **k):
        hang()


def frames(n=60, **kw):
    return [(True, bgr(**kw)) for _ in range(n)]


def configure(name: str):
    """Set the fake module up for a fixture name. Returns the module loader."""
    FakeCv2.reset()
    FakeCv2.script = frames()
    if name == "ok_gray":
        FakeCv2.script = [(True, FakeFrame((2, 4), fill=9)) for _ in range(60)]
    elif name == "ok_varying":
        FakeCv2.script = [(True, bgr(fill=10 + i)) for i in range(60)]
    elif name == "open_refused":
        FakeCv2.open_result = False
    elif name == "backend_mismatch":
        FakeCv2.backend_name = "GSTREAMER"
    elif name == "backend_error":
        FakeCv2.backend_error = RuntimeError("x")
    elif name == "version_old":
        FakeCv2.__version__ = "4.7.0"
    elif name == "version_unknown":
        FakeCv2.__version__ = "weird"
    elif name == "construct_error":
        FakeCv2.construct_error = RuntimeError("x")
    elif name == "read_exhausted":
        FakeCv2.script = []
    elif name == "none_frame":
        FakeCv2.script = [(True, None)]
    elif name == "float_frame":
        FakeCv2.script = [(True, FakeFrame((2, 4, 3), dtype="float32"))]
    elif name == "four_channels":
        FakeCv2.script = [(True, FakeFrame((2, 4, 4)))]
    elif name == "too_wide":
        FakeCv2.script = [(True, bgr(width=40, height=2))]
    elif name == "too_big":
        FakeCv2.script = [(True, bgr(width=40, height=40))]
    elif name == "release_error":
        FakeCv2.release_error = RuntimeError("x")
    elif name == "hang_read":
        FakeCv2.VideoCapture = HangReadCapture
    elif name == "spin_read":
        FakeCv2.VideoCapture = SpinReadCapture
    elif name == "crash_read":
        FakeCv2.VideoCapture = CrashReadCapture
    elif name == "hang_construct":
        FakeCv2.VideoCapture = HangConstructCapture
    elif name == "loader_missing":
        def missing():
            raise ImportError("no cv2")
        return missing
    return lambda: FakeCv2


class Fixture(DecoderHandler):
    def __init__(self, name: str, report: str | None) -> None:
        loader = configure(name)

        def factory(config):
            from tsn_dss.engine.opencv_preview_decoder import OpenCvImageDecoder      # lazily, like the production factory
            return OpenCvImageDecoder(config, module_loader=loader)

        super().__init__(factory)
        self.name, self.report_path = name, report

    def write_report(self, **extra) -> None:
        if not self.report_path:
            return
        data = {
            "captures": [{"address": c.address, "api": c.api, "params": c.params, "reads": c.read_calls, "released": c.release_calls}
                         for c in FakeCapture.instances],
            "argv": sys.argv[1:],
            "wave4_imported": "tsn_dss.engine.opencv_preview_decoder" in sys.modules,
            "cv2_imported": "cv2" in sys.modules,
            **extra,
        }
        with open(self.report_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def handle(self, message):
        if isinstance(message, P.Init):
            self.write_report(stage="init_seen")
        result = super().handle(message)
        if isinstance(message, P.Close):
            self.write_report(stage="closed")                  # last write: the process exits right after the reply
        elif isinstance(message, (P.Open, P.Read)):
            self.write_report(stage="busy")
        return result


def main() -> int:
    values: dict[str, str] = {}
    args = iter(sys.argv[1:])
    for flag in args:
        if flag in ("--ctl", "--proto", "--fixture", "--report"):
            values[flag] = next(args, "")
    return serve(_adopt(int(values["--ctl"])), Fixture(values.get("--fixture", "ok_bgr"), values.get("--report")))


if __name__ == "__main__":
    sys.exit(main())
