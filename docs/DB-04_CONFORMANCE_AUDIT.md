# DB-04 — conformance audit, restart-safety review and offline closure (S6)

Scope: provider-neutral Safe Command Runtime, simulator-only (`tsn_dss/engine/device_runtime/`, modules
`command_models`, `command_lifecycle`, `command_effects`, `command_executor`, `safety_gates`, `uncertainty`, and the
`SimulatorCommandProvider`). Evidence class: **offline** (deterministic Linux tests). No hardware, no network, no
Seestar code, no persistence. Not production readiness.

Contract: DSS-CTR-013 v0.2 (Draft) with amendment A1 (D1, owner-approved; see section 2). The version number is unchanged. Traceability: `tests/device_command_traceability.json` (DB-04, separate from
the historical DB-01 manifest `tests/device_runtime_traceability.json`), validated by
`tests/test_device_command_traceability.py`.

## 1. Transition conformance (section 9, 22 rows after A1) and Connection `busy` rows (section 6, 4 rows)

`tests/test_device_command_conformance.py` parses the contract's own tables and compares them with the code cell by
cell: every contract cell exists with an allowed next state, nothing exists that the contract does not allow, and the
terminal flags equal the contract's.

| Contract row (condition) | Result |
|---|---|
| Request shape invalid or unsupported -> rejected | implemented |
| Conflict with active state-changing Command -> rejected or safety_blocked | implemented; `rejected` chosen (owner decision), `safety_blocked` kept as an alternative event |
| Request shape valid -> validated | implemented |
| Deadline before submission -> timed_out | implemented (`check_deadline`, `submit`, admission) |
| Authorization / safety evidence missing, stale, contradictory, unknown, unsafe -> safety_blocked | implemented (authorizer, readiness, gate). "Unsafe" values are evaluated since DB-05 Slice 0 (section 7) |
| Unresolved-uncertainty recovery/clearance absent -> safety_blocked | implemented (store; unknown history also blocks, section 3) |
| Gates pass, no conflict -> submitted | implemented; the gate re-runs immediately before the Provider call |
| Provider rejects, no effect possible -> failed | implemented (only on an explicit `effect_possible=False`) |
| Provider rejects, effect may have occurred -> unknown_result | implemented |
| Provider acknowledges -> acknowledged | implemented; never success |
| Deadline or transport loss before determination -> unknown_result | implemented |
| Deadline, can prove no effect / cannot produce physical effect -> timed_out | **partial**: non-physical kinds yes; the *proof* branch has no evidence source (D3) |
| Effect requires monitoring -> in_progress | implemented |
| Acknowledgement is the verified effect (non-physical) -> succeeded | implemented via `EffectVerifier` only |
| Effect verified -> succeeded | implemented via `EffectVerifier` only |
| Host observes failure, or Provider reports terminal failure and no physical effect was possible -> failed | implemented (A1); a kind that cannot have a physical effect, or the Provider's explicit `effect_possible=False` |
| Provider reports terminal failure, physical effect may have occurred -> unknown_result | implemented (A1, `PROVIDER_FAILURE_EFFECT_POSSIBLE`); applied at once, not at the deadline |
| Deadline, state-changing effect undeterminable -> unknown_result | implemented |
| Deadline, operation cannot produce a physical effect -> timed_out | implemented |
| Transport lost before effect known -> unknown_result | implemented (runtime observer) |
| Cancellation accepted with no-effect evidence -> cancelled | implemented; needs non-empty Provider evidence |
| Cancellation races -> unknown_result | implemented |
| `ready` -> `busy` on Command begins | implemented at admission (**D2**, accepted) |
| `busy` -> `ready` / `degraded` on terminal outcome | implemented; `degraded` only for the Command's own physical `unknown_result` |
| `busy` + unknown_result of non-idempotent physical -> `degraded` | implemented |
| `busy` + ordinary disconnect -> `busy`, rejection record, no Provider call | implemented in S6 (**D5**) |

## 2. Discrepancy register

| ID | Subject | Status |
|---|---|---|
| D1 | Provider-reported failure of a physical Command after acceptance: table said `failed`, REQ-046/061 say `unknown_result` | **Closed** by contract amendment A1 (owner-approved, applied to both tracked contract copies): `docs/DB-04_D1_CONTRACT_AMENDMENT_PROPOSAL.md`. |
| D2 | `busy` begins at admission rather than "Command begins" | Accepted by the owner. |
| D3 | No independent no-effect / failure evidence source; `DEADLINE_NO_EFFECT_PROVEN` exists in the table and is never produced | Unavailable by owner decision until independent evidence exists. REQ-025 and REQ-046 are `partially_verified`. |
| D4 | Cancellation of a `validated` Command is not in the table | Not expanded (owner decision). |
| D5 | Rejection record for disconnect while `busy` | Implemented (contract requires a record). |
| D6 | `degraded` -> `ready` with an unresolved safety condition | Accepted: the `connected` branch is used while the uncertainty is open. |

Details: `docs/DB-04_CONTRACT_DISCREPANCIES.md`.

## 2a. Provider trust model for `effect_possible`

* A Provider's acknowledgement, progress, completion and failure reports are Provider-reported evidence. None of them
  establishes physical truth (REQ-024, REQ-061); success needs an `EffectVerifier` over fresh post-command evidence.
* `effect_possible=False` (on a refusal at submission or on a failure report) is a **Provider-reported statement, not
  independent proof**. The runtime relies on it in exactly one direction: to let that refusal or failure be `failed`
  instead of `unknown_result`. It never uses it to clear an uncertainty, to retry, or to mark success.
* Only the explicit value `False` counts. `True`, `None`, any other value, a missing statement, and the default of
  `ProviderCommandReport` all mean "a physical effect may have occurred" and give `unknown_result`.
* Consequently a Provider that wrongly states `False` can turn an uncertain physical outcome into `failed`, and the
  runtime cannot detect that. Provider implementations (DB-05) must state `False` only when they can support it, and
  their acceptance must test it; a Provider that cannot decide should leave the default.
* For a kind that cannot produce a physical effect (`physical=False`) a Provider failure report is `failed` regardless of
  the flag, because no physical effect is possible by definition of the kind.

## 3. Restart-safety review

**Finding.** Through S5 an empty, newly constructed `UncertaintyStore` answered `NONE_RECORDED`, and the safety gate
treated that as a pass. After a process restart the store is empty by construction, so the runtime would have read "no
recorded uncertainty" as "no unresolved physical effect". Only the per-Command freshness requirements stood in the way;
a pending effect that is not visible in the required telemetry would have been invisible too. That contradicts REQ-049/050
(an empty store after a restart must not establish physical safety).

**Fix (smallest provider-neutral, in-process).** A device's history is *unknown until established in this process*:

* `UncertaintyStore.state_for` answers `UNKNOWN` for a device with no baseline and no resolved entry; any open entry still
  answers `UNRESOLVED`.
* The gate no longer lets `NONE_RECORDED` or `UNKNOWN` pass (`HISTORY_NOT_ESTABLISHED`, `UNCERTAINTY_UNKNOWN`). Only an
  explicitly established history (`RESOLVED_BY_RECOVERY_EVIDENCE` or `CLEARED_BY_OPERATOR`) can pass, and every freshness
  requirement still applies on top.
* History is established only by an explicit act, in either of the two recorded kinds that stay distinguishable:
  `establish_baseline_by_recovery(connection)` (a configured `BaselineRecovery` with caller-declared freshness
  requirements and an assessor; fresh evidence required; nothing is sent to the Provider) or
  `establish_baseline_by_operator(device_ref, operator_id, reason)` (an authorized, recorded operator clearance, which
  proves nothing about physical effects). Resolving an uncertainty entry also establishes the history.
* A baseline never hides an open uncertainty, is refused if one exists, and cannot be established twice.
* Without a configured `BaselineRecovery` or a `ClearanceAuthorizer` nothing can establish history: safety-sensitive
  physical Commands stay blocked (fail closed). Passive DB-01 reads, `refresh_evidence`, and non-safety-sensitive kinds are
  unaffected (tests in `RestartTests`).

**Residual risk (stated, not hidden).** Establishing a baseline accepts the device's *current observed state* (or an
operator's word) as the starting point; it cannot know what happened before this process started. No persistence, database,
global arbitration or vendor-specific assumption was added.

## 4. API-export decision

No DB-04 name is exported from `tsn_dss.engine.device_runtime` (`__all__` unchanged; asserted by
`Db04BoundaryTests.test_the_unstable_command_surface_is_not_exported_from_the_package`). Nothing outside the package needs
them yet, the interfaces (executor, store, recovery assessors, `CommandCapableProvider`) are not stable until DB-05
exercises them against a real Provider and D1 is adjudicated, and exporting would turn them into a compatibility promise.
Callers import from the submodules. Revisit when DB-05 consumes the runtime.

## 5. Final mutation review

45 targeted mutants across all DB-04 modules (and the DB-01 touch points `lifecycle.py`, `connection.py`, `runtime.py`) were
applied one at a time against the DB-04 and DB-01 device-runtime test modules; **all 45 were killed**, none survived. Three target amendment A1: a Provider failure report left open instead of `unknown_result`, a non-explicit `effect_possible` accepted as no-effect, and the new table row sent to `failed`. One further candidate (removing the early return in the combined uncertainty view) is an equivalent mutant, because the final `return` yields the same state, and was dropped.
Covered: acknowledgement counted as success; provider completion counted as success; physical failure report as `failed`;
reject default as no-effect; physical deadline as `timed_out`; transport loss as `failed`; missing no-effect/evidence
checks; authorization fail-open and truthy-authorization; missing conflict check and `ready` check; slot never freed;
unlocked admission; gate clock read before evidence; missing re-gate at submission; missing degrade; missing loss observer;
cancellation without evidence; missing uncertainty entry; ignored open entry; resolution overwrite; recovery with stale or
unresolved evidence; open clearance; ignored store verdict; double baseline; empty store reading as "none recorded";
`NONE_RECORDED` passing the gate; loose age; future timestamps accepted; contradictions accepted; provider time ignored;
missing-view pass; physical truth claimed; `ready` withheld check removed; missing disconnect audit; unauthorized `busy`
control; `busy` entered from `connected`. Mutation runs are not part of the committed suite.

## 6. Closure statement

DB-04 is **IMPLEMENTED (offline), simulator-only**. It is not hardware-verified (the stage requires none) and not production
ready. Open items that do not block offline closure: D1 is closed by amendment A1; D3 branch unavailable by
decision (REQ-025, REQ-046 remain `partially_verified`); D4 not expanded; single executor per Connection (no global
arbitration); no command-kind-specific controlled shutdown/cancellation procedure; in-process only (no persistence);
`EffectVerifier`, `RecoveryAssessor` and `BaselineRecovery` implementations for real Providers belong to DB-05.

## 7. Post-closure extension: value constraints (DB-05 Slice 0)

Finding (from the DB-05 readiness research): through DB-04 closure the gate checked that required evidence was present,
known, fresh and uncontradicted, but not its *value*, so the section 9 word "unsafe" (and preconditions such as "known
non-moving state") could not be expressed in the neutral runtime.

Change (additive, backward-compatible, provider-neutral):

* `FreshnessRequirement` gains an optional `allowed_values` (default `None` = no value constraint, i.e. the previous
  behavior). Accepted: a non-empty collection of `str`, `int`, `float` or `bool`; refused: empty, `None`, `NaN`, containers,
  duplicates, and any use on a `capability:` item.
* `evaluate_gate` reports `GateReasonCode.STATE_UNSAFE` (item and source only; the value is never echoed) for fresh,
  known evidence whose value is not allowed. Matching is type-sensitive (`value_allowed`): `True` does not match `1`, `1` does
  not match `1.0`, `"1"` matches neither, and strings are case-sensitive.
* Expired, future-dated, stale, unknown, unavailable, missing and contradictory evidence keep their own reasons and are never
  reported as merely unsafe or accepted. Contradictory readings stay `CONTRADICTORY` (plus `STATE_UNSAFE` for each disallowed
  reading).
* No executor change: the existing gate integration already runs at admission and again immediately before submission, so a
  disallowed value blocks both (`safety_blocked`, event `SAFETY_EVIDENCE_INSUFFICIENT`, `busy` released, no Provider call).
* The runtime defines no state names, values, thresholds or default freshness values (tested). Public exports and the
  executor's public surface are unchanged.

Contract note. Section 9 lists "unsafe" among the conditions that produce `safety_blocked` but does not define what makes a
value unsafe; this extension leaves that to the registrant of each Command kind and does not amend the contract.
