# DB-05 hardware validation record — Seestar physical commands

## 1. Decision

**Status: IMPLEMENTED (offline) + PARTIAL HARDWARE VALIDATION. Not hardware-accepted. Not production readiness.**

- **Scope of the claim:** one Seestar S30 Pro, firmware 9.31. The supervised happy path of the four allow-listed commands (arm deploy, scenery start, scenery stop, arm park) ran once on hardware through the DB-04 Safe Command Runtime (`SeestarControl`, `CommandDriver`, `OperatorPermit`, the effect verifiers) with explicit operator confirmation of each command.
- **Not claimed:** the roadmap exit criterion "controlled hardware validation, including failure and timeout paths". Failure, unknown-result, transport-loss and recovery scenarios were **not** exercised on hardware. Preview through the DB-05 path was **not** exercised. Behavior on other firmware or devices is not claimed.
- **Evidence provenance:** the hardware results below are **operator-reported**. The raw JSON reports are not stored in the repository and were not independently reproduced. This is evidence, not formal acceptance.

## 2. Code baseline

Branch `feat/db05-slice0-gate-values` (Slice 0 `8c9530d`, Slice 1 `c12034d`, Slice 2 `02f7b8c`, recovery continuity `07e18d5`, Slice 3 `9caa4f1`, diagnostics `919c027`, app-state shape diagnostic `3c5b7b4`, step-by-step mode `e02de02`). Contract: DSS-CTR-013 Draft 0.2 plus amendment A1.

## 3. Hardware evidence (operator-reported)

| Run | Result |
|---|---|
| DB-05 H0, read-only | identify PASS (`arm_closed_cameras_stopped`); the six observed telemetry items known; mount stationary, arm closed, both camera states and both RTSP states `cancel`; no command sent |
| DB-05 supervised, `--step-by-step`, `--no-preview` | identify PASS; baseline PASS (`established_by_recovery`); deploy PASS (`opened`, succeeded after 7 polls); scenery start PASS (`ready`, 2 polls); preview skipped by the operator; scenery stop PASS (`stopped`, 1 poll); park PASS (`parked`, 8 polls); final state arm closed, cameras stopped; every command confirmed by the operator (GO/NEXT, plus ARM for deploy and park) |
| Standalone E2E validator (`tools/seestar_e2e_validate.py`), three cycles | succeeded, including MAIN/WIDE preview. **DB-03/E2E evidence only.** It bypasses the DB-04 runtime and is not DB-05 evidence, in particular not DB-05 preview acceptance. |

What the supervised run shows on the real device: the DB-05 policies' preconditions were satisfiable from real DB-02 telemetry and capability reports; the baseline by recovery worked; each command was gated, authorized, submitted and verified by fresh provider-reported telemetry strictly after the submission boundary; the vocabulary `cancel` is what a stopped camera reports at rest on this firmware.

Parameters used in the supervised run (operator-reported): telemetry freshness 10 s, capability freshness 10 s, command deadline 90 s, polling interval 2 s, permit validity 600 s, preview disabled (`--no-preview`). They are operator choices for that run and are **not** calibrated defaults of the code (the code has none).

## 4. Assessment against the roadmap criteria

| Criterion (roadmap DB-05) | Offline | Hardware |
|---|---|---|
| Commands rejected when evidence is stale, unavailable, contradictory or unknown | verified (`tests/test_device_gate_values.py`, `tests/test_seestar_control*.py`) | not exercised as a refusal. Earlier H0 reads showed the four camera items `unavailable` on a parked, idle device (later H0: known). Cause undetermined; see L3. |
| Movement requires known non-moving state and authorization | verified (policies, `OperatorPermit`) | observed on the happy path (deploy and park, each with ARM confirmation) |
| Scenery start requires compatible state and verified readiness | verified | observed (start succeeded after verified ready telemetry) |
| Park requires stopped conflicting activity | verified | observed (park after verified stop) |
| Provider acknowledgement is not physical success; verified effect needs fresh post-command evidence | verified | observed: success only after fresh telemetry showed the effect |
| Timeout or transport loss after possible execution gives `unknown_result` | verified in the simulator | **not exercised** |
| Unresolved uncertainty blocks later safety-sensitive commands until recovery evidence or authorized clearance | verified in the simulator | **not exercised** |
| Recovery (`recover()`, operator clearance) | verified in the simulator | **not exercised** |
| Operator authorization | verified (`tests/test_seestar_control_permit.py`) | observed (DEPLOY grant, ARM per arm movement) |
| Preview through the DB-05 path | not part of the runtime; the CLI only calls the DB-03 tool | **not exercised** (`--no-preview`) |
| Hardware validation including failure and recovery (mandatory) | n/a | **open** |

Offline test evidence: `tests/test_seestar_control.py` (53), `test_seestar_control_runtime.py` (48), `test_seestar_control_permit.py` (20), `test_seestar_command_validate.py` (39), `test_seestar_readonly_app_shape.py` (6), `test_device_gate_values.py` (30). The full suite has 1720 tests with the same 60 known failures (4 failures, 56 errors) as the baseline before DB-05; none involves DB-05.

## 5. Limitations (part of the status)

- **L1 — failure and recovery untested on hardware.** No unknown-result, lost-connection, deadline-expiry, failed-cleanup or operator-clearance path ran on the device. These are verified only against the simulator. The CLI currently has no recovery action and recovery state is in-process only, so after an uncertain outcome the tool reports `recovery_required` and exits; exercising `recover()` on hardware needs a small CLI addition first.
- **L2 — preview not exercised in DB-05.** DB-03 open gaps G1–G8 are unchanged by this record.
- **L3 — camera items can be `unavailable` on a parked idle device.** An earlier H0 reported all four camera items `unavailable` (the provider maps an absent field to `unavailable`); a later H0 showed them known. The raw app-state shape that explains the difference was not recorded here. Physical mode refuses to start when the camera items are not known and stopped (fail-closed, nothing sent); the code does not treat `unavailable` as stopped.
- **L4 — timing not calibrated.** The values listed in section 3 worked in one run (deploy needed 7 polls and park 8 polls of 2 s, well inside the 90 s deadline). One run is not calibration and the code has no defaults.
- **L5 — one device, one firmware, one supervised run.** Operator-reported and not independently reproduced.
- **L6 — in-process only.** Uncertainty and the original command record live in the process; a restart loses them and requires an explicit baseline. No global arbitration across processes. Independent no-effect or failure evidence does not exist (DSS-CTR-013 D3, unchanged).
- **L7 — stricter start precondition than the E2E tool.** `scenery.start` requires all four camera items known and stopped.
- **L8 — no GoTo, HTTP, MCP, frontend, acquisition or persistent registry.** DB-05 sends exactly four commands; GoTo / target slewing is outside it and not assigned to any stage (see the roadmap, DB-06A scope note). The CLI is the only operator entry point.

## 6. Closing the remaining criteria

Acceptance (as opposed to this partial status) needs supervised hardware runs of at least: a deliberate deadline expiry or lost connection producing `unknown_result` and the tool's exit 3; recovery of that uncertainty by `recover()` from fresh telemetry after reconnect; a refusal on unavailable or contradictory state; and optionally the preview stage through the CLI. Each must be recorded as operator-reported or with attached sanitized reports.
