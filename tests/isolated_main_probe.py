"""Helper run as a script by test_isolated_launcher: proves the worker does not re-execute the parent's main module.

Top-level code below runs once per execution of this file as __main__. If the launcher made the child re-run the parent's
main module, the marker file would get a second line.
"""

import os
import sys

MARKER, REPORT = sys.argv[1], sys.argv[2]
with open(MARKER, "a", encoding="ascii") as fh:
    fh.write("main-executed pid=%d name=%s\n" % (os.getpid(), __name__))

from tsn_dss.engine.opencv_isolated_decoder import WorkerConfig, WorkerProcess  # noqa: E402

if __name__ == "__main__":
    worker = WorkerProcess(WorkerConfig(), _entry_module="tests.process_fixtures", _extra_args=("--fixture", "report", "--report", REPORT))
    worker.start()
    worker.ping()
    print("report-ready", flush=True)
    worker.stop()
