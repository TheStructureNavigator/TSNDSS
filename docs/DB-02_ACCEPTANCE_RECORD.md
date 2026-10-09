# DB-02 acceptance record — Seestar read-only integration

## 1. Decision

**Status: HARDWARE_VERIFIED (read-only scope).**

- **Scope of the claim:** one Seestar S30 Pro, firmware 9.31, exercised through the read-only operations of the DB-02 Provider: discovery, connection, capability reporting, telemetry and preview availability evidence. The device is believed to have been idle during the sessions (inferred, see section 6).
- **Not claimed:** production readiness, any behavior of DB-03 (preview runtime), DB-04 (Command Runtime) or DB-05 (physical commands), or behavior on other firmware versions or devices.
- **Accepted by:** the repository owner and operator, after review of the offline evidence, the hardware validation reports and the limitations in section 7.
- **Basis:** roadmap acceptance criteria and DSS-CTR-013 Draft v0.2, assessed in sections 4 and 5. The limitations L1–L7 are part of the status, not exceptions to it.

## 2. Baseline

| Item | Value |
|---|---|
| Repository state during validation and audit | `d572487` |
| Provider and validator code | unchanged since `9899cdc`; `d572487` added only the report auditor, its tests and `.gitignore` patterns |
| Contract | `Contracts/DSS-CTR-013-Device-Provider-Runtime-Contract.md`, Draft v0.2 |
| Roadmap | `ROADMAP_DEVICE_BACKEND.md`, DB-02 section |
| Procedure | `docs/DB-02_HARDWARE_VALIDATION.md` |
| Traceability | `tests/seestar_db02_traceability.json` |

## 3. Evidence inventory

### Evidence classes used in this record

| Class | Meaning |
|---|---|
| Observed (hardware) | A validator step run against the real device asserts it, and the step passed. |
| Operator observation | A human observed it. It is not machine evidence. |
| By construction | Guaranteed by code structure and static analysis. The device cannot show it. |
| Offline | Deterministic tests against synthetic fixtures and fake or scripted peers. |
| Inferred | Concluded from other evidence. Not directly observed. |
| Not verified | No evidence of the stated kind exists. |

### Hardware sessions

Four local reports were produced by `tools/seestar_readonly_validate.py` and audited by `tools/seestar_report_audit.py` (exit code 0, `STRUCTURE_AND_SANITIZATION_OK`, violations 0, missing evidence 0). The reports are local files and are **not** part of the repository, by design (`.gitignore`).

| # | Report | Kind | Result | Notes |
|---|---|---|---|---|
| 1 | `seestar_validation_report.json` | full session | 9 of 9 steps passed | first session |
| 2 | `seestar_validation_report_no.json` | early stop | 1 of 2 steps passed | discovery failed with category `connect_failed` (network class); the hotspot was disconnected deliberately |
| 3 | `seestar_validation_report_no2.json` | full session | 9 of 9 steps passed | after the network was restored |
| 4 | `seestar_validation_report_no3.json` | full session | 9 of 9 steps passed | after the network was restored |

Facts from the audit, as reported by the operator:

- All three full sessions report model Seestar S30 Pro, firmware 9.31, and the same device identity group (the fingerprint value itself is not recorded here).
- All three full sessions report 31 telemetry items: 19 known, 10 unavailable, 2 unknown, and preview availability `unknown`.
- **The three full-session reports were byte-identical to one another** (operator-reported from the auditor's `identical_content_to_report` output). The reports contain no timestamps, so identical content is expected for repeat sessions and cannot, by itself, tell genuine repeat sessions from copies (see L5).
- The operator physically confirmed that the telescope did not move. The operator did not independently confirm that no camera was started (see L1).

### Offline and static evidence

- Operator's offline run: 125 of 125 passed. This equals the protocol (42), provider (56), boundaries (21) and traceability (6) test modules. The validation-script (9) and report-auditor (24) test modules were not part of that figure.
- Cloud environment at `d572487`: DB-02 focused tests 134 passed, report auditor 24 passed, DB-01 tests 103 passed. The full suite has 60 failures (4 failures, 56 errors) that are identical on the pristine baseline, mostly the missing `astropy`; they are not related to DB-02.
- Static audit of the provider and validator: no state-changing method name appears; the only wire methods are the three allow-listed reads (`get_device_state` with an allow-listed key filter, `iscope_get_app_state`, `test_connection`), the opt-in UDP discovery probe, and the three authentication-handshake messages that exist only in `auth.py`. Bytes leave the process only through one private send function and one UDP send, both fed from the allow-listed encoder or the fixed handshake. The validator calls only discovery, connection, refresh, capability, telemetry, preview and disconnect operations, writes only its report file, and builds its transport only after the credential check passes.

## 4. Roadmap acceptance matrix

### Deterministic acceptance tests

| # | Criterion | Result | Hardware evidence | Offline evidence |
|---|---|---|---|---|
| A1 | Read-only discovery returns a runtime Device Reference | PASS | Observed: discovery step passed in three sessions | Provider tests |
| A2 | Connection state reports provider evidence without becoming persistent Device identity | PASS | Observed: connection step passed in three sessions | No store exists; canonical database unchanged after a full exercise |
| A3 | Telemetry includes host observation time and preserves unknown, stale and unavailable values | PASS | Observed: telemetry step passed; unknown (2) and unavailable (10) states present, so nothing was invented. Stale: not verified on hardware | Stale, unknown and unavailable tests |
| A4 | Capability Reports include freshness metadata | PASS | Observed: capability step asserts the observation time | Capability tests |
| A5 | Static capabilities do not prove command success | PASS | Observed: capability step asserts unsupported operations are not available | `proves_command_success` is constant false |
| A6 | Discovery, connection reads, telemetry and capability reads submit no Commands | PASS (by construction) | Not observable on the device; operator saw no movement | Static audit; call-log tests |

### Hardware validation requirements

| Requirement | Result | Evidence |
|---|---|---|
| Must not move hardware | PASS | Operator observation; the mount-state comparison step passed |
| Must not start preview | PASS (by construction) | No preview, RTSP or image-stream code path exists |
| Must not start acquisition | PASS (by construction) | No such code path |
| Must not enable heaters | PASS (by construction) | No such code path |
| Must not submit state-changing commands | PASS (by construction) | Allow-list at the wire, static audit, tests |

### Exit criteria

| Criterion | Result | Evidence |
|---|---|---|
| Read-only runtime data conforms to the provider-neutral models | PASS | The validator drives the Provider through the DB-01 runtime on the real device |
| Hardware validation confirms no physical side effects | PASS for motion; camera state not independently confirmed (L1) | Operator observation; construction |

## 5. DSS-CTR-013 matrix (DB-02 scope)

Hardware column values: observed, exercised (ran in the sessions but no step asserts it), by construction, not applicable (structural), not verified (offline only). The status of each requirement in the traceability manifest is unchanged by this record.

| Requirements | Hardware | Offline | Notes |
|---|---|---|---|
| REQ-001, 002, 035 | exercised | PASS | Provider identity and declarations |
| REQ-003, 005, 008, 011, 012 | not applicable | PASS | Structural; no Commands exist |
| REQ-004 | exercised | PASS | |
| REQ-006 | observed | PASS | Discovery step |
| REQ-007, 055, 059 | by construction | PASS | Plus operator observation of no movement |
| REQ-009, 038, 042 | observed | PASS | Reconnect step: a new connection identity within one session |
| REQ-010, 041, 056 | exercised | PASS | |
| REQ-013, 014, 015 | observed / exercised | PASS | Capability step |
| REQ-016 (telemetry half), 017 | observed / exercised | PASS | Preview half belongs to DB-03. Preview availability was also read on hardware (step S7, reported value `unknown`); that is availability evidence, not evidence for REQ-018 |
| REQ-036 | partially observed | PASS | Hardware showed success and one failure with category `connect_failed`. Valid empty, fatal, refresh from degraded and in-session recovery are offline only |
| REQ-037, 043 | not verified | PASS | Offline only |
| REQ-057 (partial) | observed | PASS | Fresh-read evidence only; enforcement against Commands is DB-04 |
| REQ-058 | partially observed | PASS | Unknown and unavailable observed; stale offline only |
| Boundaries: REQ-018, 019, 032, 033, 052, 054, 064, 066, 067 | not applicable | PASS | Structural tests. For REQ-018 (preview data does not automatically become domain state) these are the canonical-database-untouched and no-domain-identity-fields tests; see the correction note in section 9 |
| REQ-065 | by construction | PASS | No provider-native target data is mapped |

## 6. Telemetry interpretation

**Observed** (from the audited reports): 31 items per full session; 19 known, 10 unavailable, 2 unknown; preview availability `unknown`. The step that checks the mount and view state before and after the session passed. The "evidence refresh reaches ready" step also passed, so the Connection reached `ready` in sessions whose telemetry sample contained 10 unavailable items. That is a real-device instance of the limitation already documented in the roadmap: `ready` does not guarantee that every telemetry item is fresh, and item-level freshness enforcement belongs to DB-04.

**Inferred, not observed:** the reports record counts, not field names. The counts are consistent with this reading, which is an inference and not an observation:

- All 18 mapped device-state fields plus the raw timestamp item were present on firmware 9.31. Two of them, the exposure and gain settings, hold documented device defaults and were mapped to `unknown` by design, leaving 17 known.
- Of the 11 app-state fields only the selected-camera field was known, and the 10 camera sub-fields were absent. That is consistent with preview availability `unknown` and with the cameras not having been started, but it does not prove either.
- One host-observed connection-state item is known.

Under that reading 17 + 1 + 1 gives the 19 known, 2 are unknown, and 10 are unavailable. A different split of the same totals cannot be excluded without per-field names.

## 7. Limitations

| ID | Limitation | Why it does not block DB-02 | What would close it |
|---|---|---|---|
| L1 | No independent confirmation that no camera was started. | The validator has no code path that can start one, and the roadmap constraint is on the procedure. The before/after state step is weak evidence for cameras, because the camera sub-fields were probably unavailable in both samples (inferred). | An operator note, or per-field item names by state recorded in a future run. |
| L2 | The names of the 10 unavailable items are not recorded. The preview-available path, the stale state, valid empty and fatal discovery, and replacement of a failed Connection were not exercised on hardware. | The criteria require unknown and unavailable to be preserved, which was observed. The other paths are verified offline. The preview-available path is DB-03 scope. | Recording item names by state; DB-03 hardware work. |
| L3 | It is not known whether read requests are served without the authentication handshake, and the reports do not record whether the handshake ran or was skipped. | Requiring a key is a design decision, not an acceptance criterion. The sessions worked with the operator's key. | A hardware experiment, if the question ever matters. |
| L4 | Loss and recovery were shown only across validator restarts. No loss-to-recovery sequence within one session was run on hardware. | The written procedure defined the loss check as restart-based, which was done. Within-session behavior is DB-01 runtime semantics, verified offline. | DB-04 or a future supervisor. |
| L5 | Reports contain no timestamps. Session order rests on file modification times and the operator's account. Byte-identical reports cannot be told apart from copies. | The contract requires observation times in runtime objects, and the validator asserts they exist. This weakens the audit trail only. | A UTC run timestamp in a future validator version. |
| L6 | One device, firmware 9.31, apparently idle. | The claim is scoped to exactly this. | More devices, firmware versions and device states. |
| L7 | This is not production readiness. | No stage may claim it from a single successful run (roadmap, hardware safety gates). | Separate repeated validation and policy review. |

## 8. Handed to later stages

- **DB-03:** opening and reading a preview stream, the preview-available mapping on hardware, stale-frame detection, stream loss and recovery.
- **DB-04:** item-level freshness predicates (REQ-051, REQ-060), which must evaluate item states and never Connection state alone; Commands; exclusivity; transport loss during a Command.
- **DB-05:** physical commands, whether control operations need the handshake, and the unexplained first timeout in the research notes.
- **Housekeeping, not changed here:** the DB-01 section of the roadmap still shows its original status and "next action" text.

## 9. Correction note (documentation only)

REQ-018 reads: "Preview data SHALL NOT automatically become Capture, Frame, Dataset, ProcessingRun, Observation or Session state." The original record listed it as "exercised" with the note "Preview availability evidence only". That described the availability read of step S7, which belongs to the preview half of REQ-016, not to REQ-018. REQ-018 is a structural prohibition: it is confirmed by the offline architecture tests named in the manifest entry `DSS-CTR-013-REQ-018`, and a hardware read cannot show it. It is therefore classified with the other structural boundaries (not applicable on hardware). The historical hardware fact is unchanged: preview availability was read in the sessions and the reported value was `unknown`. No requirement status in the manifest, no runtime code and no overall DB-02 status was changed by this correction.
