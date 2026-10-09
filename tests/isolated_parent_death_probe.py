"""Helper run as a script by test_isolated_launcher: starts a worker whose main thread is stuck, then dies without any cleanup."""

import os
import sys

PID_FILE = sys.argv[1]

from tsn_dss.engine.opencv_isolated_decoder import WorkerConfig, WorkerProcess  # noqa: E402

worker = WorkerProcess(WorkerConfig(), _entry_module="tests.process_fixtures", _extra_args=("--fixture", "stuck_after_ready"))
worker.start()                                    # READY; right afterwards the worker's main thread hangs
with open(PID_FILE, "w", encoding="ascii") as fh:
    fh.write(str(worker.pid))
os._exit(9)                                       # no stop(), no atexit, no finalizers: the parent just disappears
