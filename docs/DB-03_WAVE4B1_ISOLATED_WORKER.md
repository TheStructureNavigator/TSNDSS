# DB-03 Wave 4B-1 — isolated decoder worker: protocol, state machine, launcher

Status: **implemented offline; verified on Linux; not yet verified on Windows.** Implements section 16 / phase 4B-1 of `docs/DB-03_WAVE4B_PROCESS_ISOLATION_DESIGN.md` (revision 3). There is no decoder, no OpenCV, no shared segment, no image and no network in this wave; nothing is connected to the preview manager.

## What exists

Package `tsn_dss/engine/opencv_isolated_decoder/`:

| Module | Content |
|---|---|
| `protocol.py` | Versioned binary control protocol (`TDW1`), message dataclasses, `encode`/`decode`, the numeric status table (decision D13), parent-originated categories, `ProtocolError`/`UnknownStatus`. Pure; no I/O; no pickle. |
| `segment.py` | Shared-memory header codec (64 bytes) and `validate_image` (design 7.4 step 3). Pure functions; no segment is created. |
| `states.py` | `WorkerState`, `WorkerEvent`, the explicit transition table (20 entries), `worker_next_state`. |
| `worker_main.py` | Dedicated importable worker entry point (`python -P -m …worker_main --ctl <handle> --proto 1`), `serve` loop with the reader-thread lifeline watchdog, `ProductionHandler` (handshake only). |
| `process.py` | `WorkerProcess` (parent side): spawn, handshake, `ping`, bounded `stop`, `WorkerConfig`, `WorkerError`, `IsolationUnsupported`, `child_environment`, `abandoned_worker_count`. |

### API

```python
w = WorkerProcess(WorkerConfig())      # raises IsolationUnsupported("python_unsupported") below Python 3.13; starts nothing
w.start()                              # CREATED -> STARTING -> READY (INIT/HELLO + PING/PONG); WorkerError(category) on failure, worker already stopped
w.ping()                               # liveness probe while READY
report = w.stop()                      # idempotent, any thread; close -> terminate -> bounded wait -> kill -> bounded wait
w.state, w.transitions, w.failure_category, w.exit_code, w.pid, w.abandoned, w.popen_released, w.decoder_open
```

### State machine

`CREATED -> STARTING -> READY -> (OPENING | READING) -> READY …`; any failure goes `FAILED -> STOPPING -> TERMINATED`; `STOPPING` and `TERMINATED` accept `STOP_REQUESTED` (idempotent). `FAILED` never rests. `OPENING`/`READING` exist in the table for 4B-3 but nothing enters them in 4B-1.

### Readiness (4B-1 scope)

READY requires: protocol and status-table versions equal on both sides, `HELLO` status 0, the 64-bit nonce echoed, and a `PING`/`PONG` round trip with the matching `op_id`, all within `start_deadline_s`. Process started is never READY. The attach-and-cross-check of the shared segment (design 6, steps 2 and 4) arrives in 4B-2; in 4B-1 `INIT` carries no segment and a worker that is asked for one answers `shm_unavailable`.

### Handle ownership on Windows and POSIX (no private CPython API)

- The parent creates a duplex `multiprocessing.connection.Pipe`; on Windows the child end is made inheritable (`os.set_handle_inheritable`) and passed through `STARTUPINFO(lpAttributeList={"handle_list": [handle]})`, on POSIX through `pass_fds`. This is the mechanism the operator's spike verified on Windows. The parent closes its copy of the child end immediately so EOF is visible.
- The child's process handle lives inside the `Popen`. The launcher keeps the `Popen` in exactly one attribute. After the child has been reaped (`wait`/`poll` returned an exit code) that attribute is set to `None`; the `Popen` is then unreferenced and CPython closes the process handle when it is finalized (`subprocess.Handle` is finalized with its `Popen`; confirmed in the CPython 3.13 source). Tests assert, with weak references, that the `Popen` is gone right after `stop()` on the success, failure and forced-containment paths, and that handle/descriptor counts are steady over repeated cycles.
- A child that cannot be reaped keeps its `Popen` (its handle is still needed) in a module-level registry; `abandoned_worker_count()` makes that leak visible, and the worker is marked `abandoned` with the failure category `containment_failed`.
- The explicit `Popen._handle` / `Handle.Close()` calls of the spike are **not** used in production code. A test forbids them. If the Windows run of this wave shows the reference drop is not enough, that is a finding to analyse separately (a private-API fallback would need its own decision).
- Concurrent spawns are serialized by a lock (handle-inheritance flags are per handle). A concurrent `subprocess` call elsewhere in the process with `close_fds=False` could still inherit the briefly inheritable handle; the window is the duration of one `Popen` call.

### Differences from the design document, and findings made while implementing

1. **Message name.** The design calls the image notification `FRAME`; in this project "Frame" is a domain term (REQ-033). The wire message is `IMAGE_READY` (type 9, same payload), the class `ImageReady`, the validator `validate_image`. Reason tokens are `image_*`.
2. **Handshake scope.** Design section 6 puts the segment attach and the nonce cross-check into the handshake. 4B-1 has no segment, so the 4B-1 handshake is steps 1, 3 and 5 (versions, nonce echo, PING/PONG); 4B-2 adds the rest before READY.
3. **D13 table vs Wave 4 categories.** Wave 4's decoder can also propagate the `InvalidImage` categories `dimensions_out_of_range`, `image_too_large`, `data_not_bytes` and `pixels_missing`, which the approved table does not contain. Resolution (a rule, not a table change): the worker maps any category outside the table to `worker_internal` (255). A test pins the rule and a second test proves every `DecoderError` category literal of the Wave 4 decoder has a code. The only table entries with no Wave 4 literal are `length_mismatch`, `python_unsupported`, `shm_unavailable` and `worker_internal`.
4. **Vocabulary.** Category `worker_ipc_lost` is also used when `stop()` interrupts an operation and when a PING after READY is not answered in time; the approved vocabulary has no dedicated "stopped" or "ping deadline" category and none was added.
5. **`-P`.** The launcher starts the child with `python -P -m …` so the temp working directory is not on its `sys.path` (a planted module cannot shadow an import). The spike did not use `-P`; it is therefore an untested-on-Windows difference. `-P` exists since Python 3.11.
6. **`cwd`.** The child's working directory is the temp directory, not the project root, to keep relative paths of the parent out of the worker.
7. **Package import chain.** Importing `tsn_dss.engine.opencv_isolated_decoder` runs `tsn_dss/engine/__init__.py` first (SQLite repository modules, import only), as already documented in the design (section 4.3).
8. **Test seam.** The launcher accepts private keyword arguments (`_entry_module`, `_extra_args`, `_extra_pythonpath`) so tests can start fixture workers; production code always uses `PRODUCTION_ENTRY`, and a boundary test checks that no fixture selection exists in the package.

9. **`isatty` on Windows (found by the operator's Windows run, 118/120).** The hygiene test originally asserted `sys.stdin.isatty()` is false in the worker. On Windows it was true. The launcher starts the child with `stdin=stdout=stderr=DEVNULL`; in the CPython 3.13 source `subprocess` maps that, on Windows, to a handle of the null device (`os.open(os.devnull)`) set as `hStdInput`/`hStdOutput`/`hStdError` with `STARTF_USESTDHANDLES`, so the child cannot have inherited the console. The Windows C runtime documents `_isatty` as true for any *character device*, and the null device is one, so `isatty` cannot distinguish NUL from a console there (the exact CPython behavior was not confirmed from source here). The test now measures what was meant: `is_console` (Windows `GetConsoleMode`, POSIX `isatty`) must be false for stdin, stdout and stderr, and a read from stdin must return end-of-file immediately; the old `isatty` value is only recorded for diagnosis. The assertion was strengthened, not weakened, and a mutation (stdin left as an idle pipe) fails it. The launcher is unchanged.

### Not in 4B-1

`OPEN`/`READ` handling and any decoder (4B-3); creating, attaching and unlinking the shared segment, deadlines on `OPEN`/`READ`, the process cap (D10) and exit cleanup via `atexit` (4B-2/4B-3); `before_connect` and the integration with the preview manager (4B-3); any hardware or Windows production claim.

## Running the tests (Windows operator)

```
cd /d <repo>
py -m unittest tests.test_isolated_protocol tests.test_isolated_states tests.test_isolated_worker_handler tests.test_isolated_boundaries tests.test_isolated_launcher tests.test_isolated_handles tests.test_opencv_isolated_traceability -v
```

Windows-specific behavior is covered by `tests.test_isolated_launcher` (real child processes, lifeline test with a ctypes liveness probe) and `tests.test_isolated_handles` (`SteadyStateTests` count real process handles with typed Win32 calls). POSIX-only cases (`ignore SIGTERM`) are skipped there and counted as skips.
