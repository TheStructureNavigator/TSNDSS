# DB-05b — parameterized GoTo (offline increment). Physical GoTo is BLOCKED.

Status: **IMPLEMENTED (offline) only. No hardware verification. Physical execution is disabled** by `seestar_control.commands.GOTO_PHYSICAL_ENABLED = False`: the provider refuses a GoTo before any frame is sent, and the CLI refuses `--command goto` before building anything. Tests switch the flag on only against the simulator.

## Design (minimal, no new subsystem)

- **Parameters through the executor.** `CommandIntent` and `CommandRequest` gain an optional, opaque, immutable (hashable) `parameters`; `CommandRecord.parameters` exposes it to effect verifiers; `CommandKindPolicy.takes_parameters` makes the core reject parameters for the four fixed commands and demand them for GoTo. The executor passes `parameters=` to `submit_command` only when it is not `None`, so existing providers and the four commands are untouched. The core knows nothing about RA/Dec.
- **Typed target.** `GotoTarget(ra_hours, dec_deg)`: finite real numbers (no bool, no string), RA 0..24 h, Dec -90..90 deg, frozen. Nothing is converted and no frame is assumed.
- **Wire.** `SeestarControlTransport.send_goto(host, target)` sends the positional `scope_goto [RA_hours, Dec_deg]` after the existing authenticated handshake, through the same pre-send/post-send phase handling as the four commands. It accepts only a `GotoTarget`. The read-only allow-list still refuses `scope_goto`.
- **Kind `seestar.mount.goto`.** Physical, state-changing, safety-sensitive, non-idempotent. Gate: mount stationary, arm open, fresh `capability:seestar.mount.goto`. It says nothing about cameras (no source shows a dependency). Authorization: the existing `OperatorPermit`, plus the same single-use `ARM` confirmation per admission as the arm movements.
- **Verification (never success on acknowledgement).** `VERIFIED` only if, all observed strictly after the submission boundary and inside the telemetry window: the mount is stationary, AND `scope_get_equ_coord` is within `ControlFreshness.goto_tolerance_deg` of the target. No tolerance, an unreadable coordinate, a moving mount or any doubt gives `PENDING`; the deadline then makes it `unknown_result` (an unresolved uncertainty that blocks later safety-sensitive commands). The verifier never returns `FAILED`. No retry, no automatic deploy/park/camera/tracking command, no cancel (cancellation stays refused). `get_camera_state` is not used anywhere.
- **CLI.** `--command goto --ra-hours R --dec-deg D --goto-tolerance-deg T` (all required, no defaults), same single-command flow, confirmations and exit codes; the report carries the target, the device-reported final coordinates and `coordinate_frame: unknown`.

## Facts vs assumptions

Verified on hardware (S30 Pro, fw 9.31, operator-reported): `scope_get_equ_coord` returns numeric `ra`/`dec` with code 0; the four fixed commands and their verification work; `get_camera_state` reports idle during active scenery (so it is not camera-stop evidence).
Third-party reference only (seestarpy, not vendor-confirmed, never run on 9.31): `scope_goto` takes `[RA_hours, Dec_deg]`; completion is announced by unsolicited `ScopeGoto` events; a goto cannot start from the parked position; cancel is `iscope_stop_view` with stage `ScopeGoto`/`AutoGoto`.
Unknown: coordinate frame/epoch; the `move_type` values during a slew; the reply to `scope_goto`; whether `scope_get_equ_coord` lags the real pointing; tracking afterwards; behavior with scenery running.

## Owner decisions required before physical GoTo can be enabled

1. **Contract.** DSS-CTR-013 does not define Command request fields, so carrying an opaque immutable `parameters` value was treated as an implementation detail, and the Provider `submit_command` gained an optional keyword. Confirm that no amendment is needed, or request one.
2. **Coordinate frame/epoch** (J2000 vs of-date): a read-only comparison of reported coordinates against a known pointing is needed. Until then the tolerance must absorb any offset and success means "reported coordinates match the request", not "the target is centered".
3. **Pointing and horizon limits**, including Sun avoidance. No altitude, horizon or Sun-exclusion policy exists; `scope_get_horiz_coord` is not on the read allow-list. This is a safety policy for the owner to define; none was invented.
4. **Completion evidence and tolerance value**, and whether the `ScopeGoto` event stream is needed at all (polling with coordinates is what is implemented).
5. **Cancellation/abort** for a slew (currently refused) and whether a GoTo may run with scenery active or the tracking state changed.
6. **Roadmap placement.** Recommended: a separate DB-05b milestone after DB-05 failure/recovery hardware validation, not part of DB-06A. Not applied here.

To unblock: resolve 2-5, run read-only hardware captures for them, then flip `GOTO_PHYSICAL_ENABLED` in a reviewed change and validate step by step (one supervised GoTo, then failure/recovery paths).
