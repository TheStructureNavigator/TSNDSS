"""Helper run as a script by test_isolated_limit: a host process that starts a fixture worker and then ends in a chosen way.

usage: isolated_shm_probe.py REPORT_FILE MODE
  die_after_ready   : handshake done, then ``os._exit`` (no stop, no atexit, no finalizers)
  die_before_hello  : the worker never answers INIT; the host dies while ``start()`` is still waiting
  exit_clean        : handshake done, then a normal interpreter exit without ``stop()`` (atexit must contain the worker)
  exit_stubborn     : like exit_clean, but the worker ignores SIGTERM and hangs on CLOSE (needs the kill step)
"""

import json
import os
import sys
import threading
import time

REPORT, MODE = sys.argv[1], sys.argv[2]

from tsn_dss.engine.opencv_isolated_decoder import WorkerConfig, WorkerProcess  # noqa: E402

FIXTURE = {"die_after_ready": "ok_ops", "die_before_hello": "no_hello", "exit_clean": "ok_ops",
           "exit_stubborn": "ignore_sigterm_hang_on_close"}[MODE]
worker = WorkerProcess(WorkerConfig(slot_bytes=4096, start_deadline_s=30, close_graceful_s=0.3, terminate_wait_s=0.5, kill_wait_s=0.5),
                       _entry_module="tests.process_fixtures", _extra_args=("--fixture", FIXTURE))


def write(**extra) -> None:
    with open(REPORT, "w", encoding="ascii") as fh:
        json.dump({"pid": worker.pid, "segment": worker._segment.name if worker._segment else None, **extra}, fh)


if MODE == "die_before_hello":
    threading.Thread(target=lambda: worker.start(), daemon=True).start()
    deadline = time.monotonic() + 20
    while (worker.pid is None or worker._segment is None) and time.monotonic() < deadline:
        time.sleep(0.02)
    write()
    os._exit(9)
worker.start()
write()
if MODE == "die_after_ready":
    os._exit(9)
sys.exit(0)                                        # exit_clean / exit_stubborn: no stop(); the atexit handler has to do it
