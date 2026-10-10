# DB-04 — DSS-CTR-013 discrepancies and open interpretations

Status: for owner adjudication. Nothing here changes the normative table of DSS-CTR-013 v0.2 (Draft).
Each item says what the contract text is, what the implementation does, and what is needed to close it.

## D1 — Provider-reported failure of a physical Command after acceptance (adjudication needed)

Contract (section 9 table, row for `acknowledged` or `in_progress`):
"Provider reports terminal failure or host observes failed effect -> `failed`; evidence: Failure evidence".

The same table has the opposite-risk row only for `submitted`:
"Provider rejects before acceptance but physical effect may have occurred -> `unknown_result`".
For `acknowledged`/`in_progress` there is **no** row that sends a Provider-reported terminal failure to
`unknown_result` when a physical effect may have occurred. Read literally, a Provider's own failure report
is "failure evidence" and yields `failed`, although REQ-061 says Provider-reported state is not independent
proof of physical truth and REQ-046 / section 11 say a possible physical effect that cannot be determined is
`unknown_result`.

Implementation (S4, kept in S5): for a physical Command a Provider failure report with `effect_possible`
other than an explicit `False` does not produce `failed`; the Command stays open and the deadline resolves it
to `unknown_result` (`enforce_deadline`). Non-physical kinds and explicit no-effect reports follow the table
(`failed`). The transition table itself is unchanged.

To close: either add a row (`acknowledged`/`in_progress`, Provider-reported failure with possible physical
effect -> `unknown_result`) or state that Provider-reported failure is `failed` for physical Commands.

## D2 — When `busy` begins

Contract: `ready` -> `busy` on "State-changing Command begins". Implementation (owner decision): at admission,
when the Command wins the Connection's exclusive slot; a gate block releases it. Closed by decision; recorded
because it differs from a submission-time reading.

## D3 — `DEADLINE_NO_EFFECT_PROVEN` has no source

The row "`submitted`: deadline elapses and TSN DSS can prove ... physical effect did not occur -> `timed_out`"
needs independent no-effect evidence. DB-04 has no such source, so the event exists in the table and is never
produced; every post-submission deadline on a physical Command ends as `unknown_result`. Candidate for S6.

## D4 — Cancellation before submission

The table allows cancellation only from `submitted`, `acknowledged`, `in_progress`. A `validated` Command can
only leave through `timed_out` or `safety_blocked`. `cancel` on a `validated` Command is therefore refused.

## D5 — Ordinary disconnect while `busy`

Contract: `busy` + ordinary disconnect -> `busy`, "Rejection or safety-block record; no provider disconnect call".
Implementation: no transition exists, so `disconnect` raises `InvalidTransition` before any Provider call and the
Connection stays `busy`; no rejection record is stored. Behavior is safe; the record is missing. Candidate for S6.

## D6 — `degraded` -> `ready` with an unresolved safety condition (resolved in S5)

Contract: `degraded` -> `ready` or `connected` only when "no unresolved safety condition blocks use". S5 uses the
`connected` branch: while the device has an unresolved uncertainty, `refresh_evidence` moves a `degraded`
Connection to `connected`, never `ready`. Reads are unaffected. `connected` -> `ready` and a fresh Connection are
unchanged (the table has no safety condition there), so a Connection can be `ready` on a device with an
unresolved uncertainty; the safety gate, not Connection state, blocks safety-sensitive Commands.

## Remaining for S6

D3 (no-effect proof source), D5 (disconnect rejection record), the DB-04 traceability manifest and roadmap
status, and a final mutation pass.
