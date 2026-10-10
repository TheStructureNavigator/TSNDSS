"""Dedicated production entry point of the decoder worker (DB-03 Wave 4B-3a):
``python -P -m tsn_dss.engine.opencv_isolated_decoder.decoder_main --ctl <handle> --proto 1``.

Same command line as the handshake-only worker (two numbers, nothing else), same serve loop and lifeline watchdog; the only
difference is the handler: ``DecoderHandler`` answers ``OPEN``/``READ``/``CLOSE`` with the Wave 4 OpenCV adapter. The
handshake-only ``worker_main`` and ``PRODUCTION_ENTRY`` are unchanged. This module imports no OpenCV.
"""

from __future__ import annotations

import sys

from . import protocol as P
from .worker_decoder import DecoderHandler
from .worker_main import _adopt, serve

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    values: dict[str, str] = {}
    it = iter(args)
    for flag in it:
        if flag in ("--ctl", "--proto"):
            values[flag] = next(it, "")
    try:
        handle = int(values["--ctl"])
        proto = int(values["--proto"])
    except (KeyError, ValueError):
        return 64
    if proto != P.PROTOCOL_VERSION or handle < 0:
        return 64
    return serve(_adopt(handle), DecoderHandler())


if __name__ == "__main__":
    sys.exit(main())
