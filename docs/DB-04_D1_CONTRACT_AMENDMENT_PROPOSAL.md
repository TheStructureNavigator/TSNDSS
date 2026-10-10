# DSS-CTR-013 amendment A1 — D1 (APPLIED)

Status: **APPLIED** (owner approval received; closes D1). Applied identically to
`Contracts/DSS-CTR-013-Device-Provider-Runtime-Contract.md` and `docs/contracts/DSS-CTR-013-Device-Provider-Runtime-Contract.md`
(the two tracked copies stay byte-identical; a test checks it). The contract stays **Draft 0.2**: a line
`**Amendments:** A1` records the change and the version bump is left to the owner. The text below is what was applied,
plus one consequential edit: the `failed` row of the section 9 state-meaning table now reads "Evidence proves failure,
or Provider reports a terminal failure and no physical effect was possible". The optional REQ-046 extension was not applied.

## Problem

Section 9 has this row:

| Starting state | Event or condition | Next | Terminal | Required evidence |
|---|---|---|---|---|
| `acknowledged` or `in_progress` | Provider reports terminal failure or host observes failed effect | `failed` | Yes | Failure evidence |

A Provider's own failure report satisfies "Provider reports terminal failure" and counts as "failure evidence".
For a Command whose physical effect may have occurred (a partial movement, a possibly started action) that
turns an uncertain outcome into an ordinary `failed`, which then looks safe to retry. This contradicts:

* REQ-046 and section 11: a possible physical effect that cannot be determined is `unknown_result`;
* REQ-061: Provider-reported state is not independent proof of physical truth or of a Command's effect;
* the table's own `submitted` row: "Provider rejects before acceptance but physical effect may have occurred
  -> `unknown_result`". No equivalent row exists after acceptance.

## Proposed minimal amendment (section 9, v0.3)

Replace the row above by two rows:

| Starting state | Event or condition | Allowed next state | Terminal | Required evidence |
|---|---|---|---|---|
| `acknowledged` or `in_progress` | Host observes failed effect, or Provider reports terminal failure and no physical effect was possible | `failed` | Yes | Failure evidence; for a Provider-reported failure, the Provider's statement that no physical effect was possible |
| `acknowledged` or `in_progress` | Provider reports terminal failure but a physical effect may have occurred, and independent evidence does not prove failure or absence of effect | `unknown_result` | Yes | Provider failure report and uncertainty classification |

Add after "A timeout **SHALL NOT** be treated as proof of physical failure unless independent evidence proves failure.":

> A Provider-reported terminal failure of a Command whose physical effect may have occurred **SHALL NOT** by
> itself be treated as proof of failure. It **SHALL** be handled like a timeout after possible physical
> execution (REQ-046).

No new REQ-ID is needed. Optionally extend REQ-046 to read "A timeout, or a Provider-reported terminal failure
after acceptance, after possible physical execution **SHALL** produce `unknown_result` unless independent
evidence proves success, proves failure or proves that no physical effect occurred."

## Effect on the implementation (done)

* Add one Command event (for example `PROVIDER_FAILURE_EFFECT_POSSIBLE`, `acknowledged`/`in_progress` ->
  `unknown_result`) and one table row; `poll` applies it at once instead of leaving the Command open until its
  deadline. A physical Command without a deadline then no longer waits forever.
* Update `tests/test_device_command_conformance.py` (22 contract rows) and `ProgressAndVerificationTests`.
* Re-run the DB-04 mutation set (`docs/DB-04_CONFORMANCE_AUDIT.md`).

## Behavior before the amendment (historical)

A physical Command's failure report with `effect_possible` other than an explicit `False` leaves the Command
`acknowledged`/`in_progress`; `enforce_deadline` then resolves it to `unknown_result`. Non-physical kinds and
explicit no-effect reports end as `failed` as the v0.2 table says.
