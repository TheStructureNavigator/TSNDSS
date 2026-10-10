# TSN Deep Space System

## DSS-CTR-013 --- Device & Provider Runtime Contract

**Status:** Draft\
**Version:** 0.2\
**Amendments:** A1 (DB-04 D1, owner-approved): Provider-reported terminal failure with a possible physical effect is `unknown_result` (section 9). The version number is unchanged pending an owner decision.\
**Authority:** TSN DSS\
**Scope:** Runtime device providers, device discovery, connection state, capabilities, telemetry, preview and commands

------------------------------------------------------------------------

## 1. Purpose

This contract defines the canonical TSN DSS runtime boundary for astronomical device providers and devices.

All contracts, policies, procedures, software components, integrations and operational processes that reference provider runtime, device discovery, runtime connection state, capability reporting, telemetry, preview or device commands **SHALL** use the meaning established by this contract.

This contract is intentionally provider-neutral. It supports simulator providers and physical providers without making any specific astronomical device, library, communication protocol, transport, user interface or hardware vendor the owner of TSN DSS domain semantics.

This contract does not implement Acquisition Execution. Acquisition Execution is a future handoff boundary between provider-produced artifacts and canonical TSN DSS provenance.

------------------------------------------------------------------------

## 2. Scope and Exclusions

This contract defines:

- Provider identity and runtime lifecycle,
- Device discovery and provider-native device references,
- runtime Connection identity and lifecycle,
- Capability Report semantics,
- read-only Telemetry and Preview semantics,
- Command identity, lifecycle, outcomes and safety invariants,
- runtime unresolved physical-uncertainty handling,
- Simulator conformance,
- Provider-to-domain ownership boundaries.

This contract does **NOT** define:

- database tables or schema migrations,
- persistent Device identity,
- credential storage,
- HTTP, MCP, frontend or user-interface behavior,
- concrete Seestar, ASCOM, Alpaca or other provider APIs,
- Acquisition Execution,
- Capture or Frame creation from provider artifacts,
- SessionContextFact or SessionEvent persistence from provider telemetry or commands,
- global multi-client arbitration,
- persistent command queues,
- persistent command history,
- physical safety procedures beyond runtime precondition requirements.

------------------------------------------------------------------------

## 3. Terminology

A **Provider** is a configured TSN DSS runtime integration component that can discover, connect to, read from, preview from or command one or more Devices.

A **Device** is a physical or simulated equipment endpoint known to TSN DSS through Provider evidence.

A **Device Reference** is the provider-native runtime reference used to address a discovered Device. A Device Reference is not a canonical TSN DSS database identity.

A **Connection** is one runtime relationship between TSN DSS and a Device Reference through a Provider.

A **Capability Report** is a timestamped runtime statement describing operations a Provider supports and operations currently available for a Connection.

**Telemetry** is read-only runtime evidence reported by a Provider or observed by TSN DSS about a Device, Provider, Connection or related runtime state.

A **Preview** is read-only runtime image or stream evidence used for inspection, alignment, diagnostics or operator feedback.

A **Command** is an explicit state-changing runtime request submitted by TSN DSS to a Provider for a Device or Connection.

A **Provider acknowledgement** is evidence that the Provider accepted or received a Command. It is not evidence that the physical or simulated effect succeeded.

A **verified effect** is evidence observed after Command submission that the intended runtime or physical state change occurred.

A **deadline elapsed event** records that an operation did not produce required evidence within its allowed interval. It is not by itself a final physical outcome.

An **unresolved physical uncertainty condition** is runtime evidence that a non-idempotent physical Command reached `unknown_result` and therefore may have affected a physical Device in a way TSN DSS cannot determine.

------------------------------------------------------------------------

## 4. Provider Identity and Lifecycle

Every Provider **SHALL** have a stable `provider_id` within a configured TSN DSS runtime.

Every Provider **SHALL** declare a provider kind, implementation label or version, simulation status and configuration status.

Provider identity **SHALL NOT** depend solely on a discovered Device Reference, a Connection identity, a Command identity, a Session identity, an Observation identity, a Project identity or a provider-specific network endpoint.

A Provider MAY support zero, one or many Devices.

Provider lifecycle describes the integration component itself. It does not describe the state of any one Device Connection.

Provider lifecycle states are:

| State | Meaning | Terminal |
|---|---|---|
| `unconfigured` | The Provider is known but required runtime configuration is missing. | No |
| `configured` | The Provider has enough configuration to attempt discovery or connection. | No |
| `discovering` | The Provider is actively attempting discovery or discovery refresh. | No |
| `available` | The Provider can perform at least one configured runtime operation. | No |
| `degraded` | The Provider is partially usable but some required evidence, transport or capability is stale, unavailable or failing. | No |
| `failed` | The Provider cannot proceed without intervention or reconfiguration. | Yes until reset |

Valid Provider transitions:

| Starting state | Event or condition | Allowed next state | Evidence |
|---|---|---|---|
| `unconfigured` | Required configuration supplied | `configured` | Configuration validation result |
| `configured` | Discovery starts | `discovering` | Discovery request timestamp |
| `available` or `degraded` | Discovery refresh starts | `discovering` | Refresh request timestamp |
| `discovering` | Discovery completes with one or more usable Device References | `available` | Discovery result and freshness metadata |
| `discovering` | Discovery completes with zero Device References and zero devices is valid for this Provider configuration | `available` | Empty discovery result and configuration evidence |
| `discovering` | Discovery completes with zero Device References when configuration requires a specific Device | `degraded` | Empty discovery result and missing-device expectation |
| `discovering` | Nonfatal discovery failure occurs | `degraded` | Recoverable error category and timestamp |
| `discovering` | Fatal discovery failure occurs | `failed` | Fatal failure category and message |
| `configured` or `available` | Provider health becomes partial outside discovery | `degraded` | Error, stale data or partial capability evidence |
| `degraded` | Provider health recovers without discovery refresh | `available` | Fresh successful provider evidence |
| `degraded` | Discovery refresh recovers required evidence | `available` | Fresh discovery result |
| Any state | Fatal provider condition | `failed` | Failure category and message |
| `failed` | Operator or configuration reset | `configured` or `unconfigured` | Reset evidence |

Provider lifecycle **SHALL NOT** include device-specific `connecting`, `connected`, `ready`, `busy` or `unknown_result` states. Those states belong to Connection or Command semantics.

------------------------------------------------------------------------

## 5. Device Discovery and References

Device discovery **SHALL** return provider-native Device References and discovery evidence.

A discovered Device entry SHOULD include:

- `provider_id`,
- `device_ref`,
- display label when available,
- model or product family when available,
- firmware or software version when available,
- endpoint hint when safe to expose,
- `discovered_at`,
- evidence quality or freshness.

A Device Reference **SHALL NOT** be treated as a canonical Project, Session, Observation, Target, SessionPlan, AcquisitionPlan, MosaicPlan, Capture, Frame, Dataset, ProcessingRun, SessionContextFact or SessionEvent identity.

Discovery **SHALL NOT** create, replace or modify canonical TSN DSS domain records.

Discovery **SHALL NOT** move hardware, start acquisition, change heater state, start preview or submit any state-changing Command.

Device identity is runtime-only in this contract version. Persistent device identity is deferred to a future contract revision or dependent contract.

------------------------------------------------------------------------

## 6. Connection Identity and Lifecycle

Every Connection **SHALL** have a runtime `connection_id` controlled by TSN DSS.

A Connection **SHALL** reference exactly one Provider and one provider-native Device Reference.

Connection identity **SHALL NOT** be treated as Provider identity, Device persistence identity, Session identity, Observation identity, Capture identity or Command identity.

Connection lifecycle states are:

| State | Meaning | Terminal |
|---|---|---|
| `new` | Connection object exists but no transport/session attempt has begun. | No |
| `connecting` | A connection attempt is in progress. | No |
| `connected` | A transport or provider session is established. | No |
| `ready` | Fresh evidence says the Connection can perform currently available operations. | No |
| `busy` | One state-changing Command is active for this Connection. | No |
| `degraded` | The Connection is partly usable or has stale/partial evidence. | No |
| `disconnecting` | Cleanup or disconnect is in progress. | No |
| `disconnected` | No active connection exists. A completed disconnected Connection is terminal and reconnect creates a new `connection_id`. | Yes |
| `failed` | The Connection cannot proceed without intervention. Failed Connections are replaced rather than silently reset. | Yes |

Valid Connection transitions:

| Starting state | Event or condition | Allowed next state | Evidence |
|---|---|---|---|
| `new` | Connect requested | `connecting` | Connect request timestamp |
| `connecting` | Repeated connect requested | `connecting` | Existing pending attempt identity; no duplicate provider call |
| `connecting` | Provider transport/session established | `connected` | Provider or host-observed connection evidence |
| `connected` or `ready` | Repeated connect requested | `connected` or `ready` | Existing Connection identity; no duplicate provider call |
| `connected` | Required fresh state obtained | `ready` | Fresh state/capability evidence |
| `ready` | State-changing Command begins | `busy` | Command identity |
| `busy` | Active Command reaches a terminal outcome and no unresolved safety condition blocks use | `ready` or `degraded` | Command outcome and post-command evidence |
| `busy` | Active Command reaches `unknown_result` for a non-idempotent physical Command | `degraded` | Unresolved physical uncertainty condition |
| `connected` or `ready` | Evidence becomes stale or partial | `degraded` | Staleness or partial-failure evidence |
| `degraded` | Fresh usable evidence returns and no unresolved safety condition blocks use | `ready` or `connected` | Fresh state evidence |
| `connected`, `ready` or `degraded` | Disconnect requested with no active nonterminal state-changing Command | `disconnecting` | Disconnect request timestamp |
| `busy` | Ordinary disconnect requested during active nonterminal state-changing Command | `busy` | Rejection or safety-block record; no provider disconnect call |
| `disconnecting` | Repeated disconnect requested | `disconnecting` | Existing pending disconnect identity; no duplicate provider call |
| `new` or `disconnected` | Disconnect requested | `new` or `disconnected` | Idempotent no-op record; no provider disconnect call |
| `disconnecting` | Disconnect completes | `disconnected` | Provider or host-observed disconnect evidence |
| Any nonterminal state | Unexpected transport loss | `disconnected`, `degraded` or `failed` | Connection-loss evidence |
| Any nonterminal state | Connection cannot continue | `failed` | Failure category and message |

Connection loss and Command outcome **SHALL** be modeled independently. A lost Connection MAY produce `disconnected`, `degraded` or `failed` Connection state and MAY leave an active Command in `unknown_result`.

Connection lifecycle **SHALL NOT** include `unknown_result`. `unknown_result` is a Command outcome.

For this contract version, each Connection **SHALL** permit at most one active nonterminal state-changing Command.

A second state-changing Command for a Connection that already has an active nonterminal state-changing Command **SHALL** be rejected or safety-blocked before Provider submission unless the second operation is an explicit cancellation or controlled-shutdown operation allowed by the active Command kind.

Read-only status, Capability, Telemetry and Preview reads MAY remain available while a Command is active when the Provider can perform them without changing physical state.

A completed `disconnected` Connection **SHALL NOT** be reactivated. Reconnect **SHALL** create a new `connection_id`.

A failed Connection **SHALL** be replaced rather than silently reset. Replacement **SHALL NOT** erase unresolved physical uncertainty associated with the Provider and Device Reference.

------------------------------------------------------------------------

## 7. Capability Reporting and Freshness

A Capability Report **SHALL** identify the Provider and, when applicable, the Connection to which it applies.

A Capability Report **SHALL** distinguish Provider support from current availability.

A Capability Report **SHALL** include observation time or freshness metadata.

A Capability Report SHOULD distinguish:

- supported by Provider,
- currently available for this Connection,
- requires safety gate,
- simulated behavior,
- hardware-confirmed behavior,
- implemented but untested behavior,
- unavailable or unsupported behavior.

A Capability Report **SHALL NOT** be treated as proof that a Command will succeed.

Static capability declarations **SHALL NOT** replace fresh runtime state when a safety-sensitive Command requires current physical evidence.

Every safety-sensitive Command kind **SHALL** define the freshness predicate for each Capability, Telemetry or physical-state item it requires before Provider submission.

If required Capability, Telemetry or physical-state evidence fails a Command kind's freshness predicate, the Command **SHALL** be safety-blocked before Provider submission.

This contract does not impose one global freshness duration across all Providers or Commands.

Providers MAY report device-specific capabilities. This contract does not require all Providers or all Devices to implement the same capabilities.

------------------------------------------------------------------------

## 8. Telemetry and Preview Semantics

Telemetry and Preview are read-only runtime evidence.

Telemetry samples **SHALL** include host observation time and SHOULD include provider-reported time when available.

Telemetry **SHALL** distinguish provider-reported state from host-observed evidence when both exist.

Telemetry **SHALL** preserve unknown, stale or unavailable state rather than fabricating current values.

Preview data **SHALL** remain runtime evidence unless an explicit future Acquisition Execution or provenance workflow imports it.

Preview data **SHALL NOT** automatically become Capture, Frame, Dataset, ProcessingRun, Observation or Session state.

Telemetry **SHALL NOT** automatically become SessionContextFact or SessionEvent.

Reading Telemetry or Preview **SHALL NOT** submit state-changing Commands or alter physical Device state.

------------------------------------------------------------------------

## 9. Command Identity and Lifecycle

Every Command **SHALL** have a TSN DSS controlled `command_id`.

Every Command **SHALL** identify the Provider and Connection against which it was requested.

A Command MAY include an idempotency key when the Command kind is safely idempotent.

A Command **SHALL** be explicitly requested by an authorized caller or approved procedure.

Passive Provider, Device, Connection, Capability, Telemetry or Preview reads **SHALL NOT** create or submit Commands.

Command lifecycle states are:

| State | Meaning | Terminal |
|---|---|---|
| `requested` | TSN DSS has received a Command request. | No |
| `validated` | Request shape and target references are valid. | No |
| `rejected` | Request is invalid, unsupported or conflicts with an active Command before Provider submission. | Yes |
| `safety_blocked` | Required safety evidence, freshness, authorization or unresolved-uncertainty clearance is missing, stale, unsafe or contradictory. | Yes |
| `submitted` | Command has been sent to the Provider or may have been sent to the Provider. | No |
| `acknowledged` | Provider accepted or acknowledged the Command. | No |
| `in_progress` | Command effect is pending or being monitored. | No |
| `succeeded` | Required effect has been verified. | Yes |
| `failed` | Evidence proves failure, or Provider reports a terminal failure and no physical effect was possible. | Yes |
| `timed_out` | The deadline elapsed where Provider submission or physical effect is known not to have occurred, or where the operation cannot produce a physical effect. | Yes |
| `cancelled` | Command was cancelled before terminal effect was possible or before terminal effect was verified by command-kind-specific cancellation evidence. | Yes |
| `unknown_result` | A submitted or possibly submitted state-changing Command has an effect that TSN DSS cannot determine. | Yes |

Valid Command transitions:

| Starting state | Event or condition | Allowed next state | Terminal | Required evidence |
|---|---|---|---|---|
| `requested` | Request shape invalid or unsupported | `rejected` | Yes | Validation error |
| `requested` | Request conflicts with an active nonterminal state-changing Command on the same Connection | `rejected` or `safety_blocked` | Yes | Active Command identity and conflict reason |
| `requested` | Request shape valid | `validated` | No | Validation record |
| `requested` or `validated` | Deadline elapses before Provider submission is attempted or possible | `timed_out` | Yes | Deadline record and evidence that no Provider submission occurred |
| `validated` | Required authorization or safety evidence missing, stale, contradictory, unknown or unsafe | `safety_blocked` | Yes | Safety evaluation record |
| `validated` | Required unresolved-uncertainty recovery evidence or operator clearance is absent | `safety_blocked` | Yes | Device Reference and Provider uncertainty record |
| `validated` | Safety gates pass and no conflicting active Command exists | `submitted` | No | Submission timestamp or possible-submission boundary |
| `submitted` | Provider rejects before acceptance and no physical effect was possible | `failed` | Yes | Provider rejection evidence |
| `submitted` | Provider rejects before acceptance but physical effect may have occurred | `unknown_result` | Yes | Provider rejection and uncertainty evidence |
| `submitted` | Provider acknowledges receipt or acceptance | `acknowledged` | No | Provider acknowledgement evidence |
| `submitted` | Deadline elapses or transport is lost before TSN DSS can determine whether Provider submission or physical effect occurred | `unknown_result` | Yes | Timeout or transport-loss evidence and possible-submission boundary |
| `submitted` | Deadline elapses and TSN DSS can prove Provider submission and physical effect did not occur, or the operation cannot produce a physical effect | `timed_out` | Yes | Timeout record and no-effect evidence |
| `acknowledged` | Effect requires monitoring | `in_progress` | No | Monitoring start evidence |
| `acknowledged` | Acknowledgement itself is the complete verified effect for a non-physical command | `succeeded` | Yes | Requirement-specific verification evidence |
| `acknowledged` or `in_progress` | Required effect is verified | `succeeded` | Yes | Fresh post-command evidence |
| `acknowledged` or `in_progress` | Host observes failed effect, or Provider reports terminal failure and no physical effect was possible | `failed` | Yes | Failure evidence; for a Provider-reported failure, the Provider's statement that no physical effect was possible |
| `acknowledged` or `in_progress` | Provider reports terminal failure but a physical effect may have occurred, and independent evidence does not prove failure or absence of effect | `unknown_result` | Yes | Provider failure report and uncertainty classification |
| `acknowledged` or `in_progress` | Deadline elapses and the state-changing effect cannot be determined | `unknown_result` | Yes | Deadline record and uncertainty classification |
| `acknowledged` or `in_progress` | Deadline elapses for an operation that cannot produce a physical effect | `timed_out` | Yes | Timeout record and no-physical-effect classification |
| `acknowledged` or `in_progress` | Transport is lost before effect is known | `unknown_result` | Yes | Transport-loss and last-known-command evidence |
| `submitted`, `acknowledged` or `in_progress` | Cancellation requested and accepted with evidence that terminal effect was not possible or was prevented | `cancelled` | Yes | Cancellation evidence and no-effect or controlled-stop evidence |
| `submitted`, `acknowledged` or `in_progress` | Cancellation races with possible physical execution and final effect is not known | `unknown_result` | Yes | Cancellation evidence and race assessment |

Rejection, validation failure and safety blocking are alternative outcomes. They **SHALL NOT** be modeled as sequential states that lead to successful execution.

Provider acknowledgement **SHALL NOT** be treated as verified physical success.

A timeout **SHALL NOT** be treated as proof of physical failure unless independent evidence proves failure.

A Provider-reported terminal failure of a Command whose physical effect may have occurred **SHALL NOT** by itself be treated as proof of failure. It **SHALL** be handled like a timeout after possible physical execution (REQ-046).

A deadline elapsed event **SHALL NOT** by itself determine the final Command outcome.

A timeout after possible physical execution **SHALL** produce `unknown_result` unless independent evidence proves success, proves failure or proves that no physical effect occurred.

Cancellation races **SHALL** preserve uncertainty. If a cancellation request overlaps with possible physical execution and the final effect is not known, the Command outcome **SHALL** be `unknown_result`.

Non-idempotent physical Commands **SHALL NOT** be automatically retried after timeout, transport loss or `unknown_result`.

------------------------------------------------------------------------

## 10. Safety and Authorization Invariants

State-changing Commands **SHALL** require explicit authorization.

Safety-sensitive Commands **SHALL** require fresh evidence for every physical state precondition they depend on.

Safety-sensitive Commands **SHALL** be rejected or safety-blocked when required state evidence is unavailable, stale, contradictory or unknown.

This contract distinguishes:

- known physical state,
- provider-reported state,
- host-observed evidence,
- stale state,
- unknown state,
- authorized operator clearance.

Provider-reported state MAY be used as evidence when the applicable procedure accepts it, but it **SHALL NOT** be represented as independently verified physical truth.

Authorized operator clearance MAY satisfy a safety gate when a command-kind-specific procedure permits it, but it **SHALL NOT** be represented as independent proof that a prior physical Command succeeded or failed.

Generic Provider software **SHALL NOT** claim physical clearance unless appropriate evidence or explicit operator confirmation exists.

Discovery, status reads, Capability Report reads, Telemetry reads and Preview reads **SHALL NOT** move hardware.

Commands that open, unfold, slew, park, stow, heat, cool, start acquisition, stop acquisition or otherwise affect physical equipment **SHALL** have command-kind-specific safety gates before Provider submission.

At minimum:

- open or unfold operations require known non-moving state and explicit clearance evidence or operator confirmation,
- park or stow operations require known non-moving state and evidence that conflicting preview/acquisition activity is stopped when required by the Provider,
- preview start operations require known compatible device state and separate verification of preview readiness,
- heater operations require readable current heater state and post-command verification when the Provider exposes such state,
- movement operations require known non-moving state and shall be deferred until movement-specific safety policy is defined.

Simulator Commands **SHALL** be clearly identified as simulated in Capability Reports, Telemetry, Preview and Command outcomes.

------------------------------------------------------------------------

## 11. Timeout, Cancellation, Disconnect and Recovery

A deadline elapsed event records that TSN DSS did not obtain required evidence within the allowed interval.

A deadline elapsed event **SHALL NOT** by itself prove physical success or physical failure.

The final Command outcome after an elapsed deadline **SHALL** be selected from Command evidence, possible-submission evidence, physical-effect possibility and command-kind policy.

Connection loss during a Command **SHALL** update Connection state according to Connection evidence and **SHALL** update the Command independently according to Command evidence.

Unexpected transport loss during `submitted`, `acknowledged` or `in_progress` **SHALL** preserve Command uncertainty when the effect cannot be determined.

Ordinary disconnect requests during an active nonterminal state-changing Command **SHALL** be rejected unless a command-kind-specific controlled shutdown or cancellation procedure is defined.

Unexpected transport loss is not an ordinary disconnect request.

If a Command's physical or runtime effect cannot be determined after timeout, cancellation race or connection loss, the Command outcome **SHALL** be `unknown_result`.

An `unknown_result` from a non-idempotent physical Command **SHALL** create an unresolved physical uncertainty condition associated with the Provider and Device Reference.

An unresolved physical uncertainty condition **SHALL** survive disconnect and reconnect, including creation of a new `connection_id`, within the running TSN DSS process.

Further safety-sensitive Commands for the affected Provider and Device Reference **SHALL** be safety-blocked until command-kind-specific recovery evidence or authorized operator clearance resolves the unsafe precondition.

Recovery from `unknown_result` MAY use later read-only evidence to classify actual observed state, but such later classification **SHALL NOT** retroactively erase the fact that the Command outcome was uncertain at the time.

Operator clearance **SHALL NOT** be represented as independent proof of the physical effect of an uncertain Command.

Automatic retry of non-idempotent physical Commands after `timed_out` or `unknown_result` is forbidden by this contract version.

This contract version keeps unresolved physical uncertainty runtime-only. Process restart MAY lose runtime-only uncertainty state and therefore **SHALL NOT** establish physical safety. Fresh safety evidence or authorized operator clearance is required before later safety-sensitive Commands.

------------------------------------------------------------------------

## 12. Simulator Conformance

A Simulator Provider **SHALL** implement the same Provider, Device Reference, Connection, Capability Report, Telemetry, Preview and Command concepts as a physical Provider when it claims conformance to this contract.

A Simulator Provider **SHALL** mark its Provider, Devices, Connections, Capability Reports, Telemetry, Preview and Command outcomes as simulated.

A Simulator Provider **SHALL** provide deterministic behavior sufficient for automated tests.

A Simulator Provider **SHALL NOT** bypass domain ownership boundaries merely because it is simulated.

A Simulator Provider MAY implement synthetic Commands and Telemetry that no physical Provider supports, provided those capabilities are identified as simulated and provider-specific.

------------------------------------------------------------------------

## 13. Provider-to-Domain Ownership Boundary

Provider runtime state **SHALL NOT** silently create, replace, weaken or redefine canonical TSN DSS domain identities.

Provider runtime state **SHALL NOT** own Project, Session, Observation, Target, SessionPlan, AcquisitionPlan, MosaicPlan, Capture, Frame, Dataset, ProcessingRun, SessionContextFact or SessionEvent semantics.

A Provider, Device Reference, Connection, Capability Report, Telemetry sample, Preview or Command **SHALL NOT** by itself prove that a Session occurred, an Observation occurred, a Capture exists, a Frame exists, a Dataset was curated or a ProcessingRun was executed.

Provider-native target names, coordinates, object identifiers or device labels **SHALL NOT** replace canonical Target identity.

Provider telemetry **SHALL NOT** become SessionContextFact without an explicit domain handoff.

Provider Command history **SHALL NOT** become SessionEvent without an explicit domain handoff.

Provider-produced files or preview frames **SHALL NOT** become Capture or Frame without an explicit Acquisition Execution or provenance handoff.

------------------------------------------------------------------------

## 14. Acquisition Execution Handoff Exclusion

Acquisition Execution is outside the scope of this contract.

This contract does not define:

- how an AcquisitionPlan is executed,
- when an Observation begins or completes,
- where provider-produced files are stored,
- when Capture or Frame records are created,
- how provider metadata is mapped into Frame metadata,
- how SessionContextFact or SessionEvent records are produced from provider activity,
- how Dataset or ProcessingRun records are created from acquired material.

A future Acquisition Execution contract or procedure **SHALL** preserve this contract's Provider runtime boundaries and the ownership semantics of DSS-CTR-001 through DSS-CTR-012.

------------------------------------------------------------------------

## 15. Compatibility and Extensibility

This contract is compatible with simulator-first implementation.

This contract is compatible with Seestar as an early physical Provider reference, but Seestar-specific RPC names, RTSP ports, authorization files, camera names and modes are implementation details and **SHALL NOT** define the provider-neutral core.

A Provider MAY support multiple Devices.

A Device MAY expose device-specific capabilities.

This contract version requires one active nonterminal state-changing Command per Connection and does not require global multi-client arbitration, persistent command queues or persistent command history.

Future revisions MAY define persistent Device identity, credential management, multi-client ownership, Acquisition Execution handoff, MCP exposure or HTTP/frontend behavior without redefining the runtime ownership boundaries established here.

------------------------------------------------------------------------

## 16. Normative Requirements

- **DSS-CTR-013-REQ-001:** Every Provider **SHALL** have a stable `provider_id` within a configured TSN DSS runtime.
- **DSS-CTR-013-REQ-002:** Every Provider **SHALL** declare provider kind, implementation label or version, simulation status and configuration status.
- **DSS-CTR-013-REQ-003:** Provider registration **SHALL NOT** create, replace or modify canonical TSN DSS domain records.
- **DSS-CTR-013-REQ-004:** Provider lifecycle **SHALL** describe the integration component rather than any one Device Connection.
- **DSS-CTR-013-REQ-005:** Provider lifecycle **SHALL NOT** include device-specific `connecting`, `connected`, `ready`, `busy` or `unknown_result` states.
- **DSS-CTR-013-REQ-006:** Device discovery **SHALL** return provider-native Device References and discovery evidence.
- **DSS-CTR-013-REQ-007:** Discovery **SHALL NOT** submit state-changing Commands or move hardware.
- **DSS-CTR-013-REQ-008:** A Device Reference **SHALL NOT** be treated as a canonical TSN DSS domain identity.
- **DSS-CTR-013-REQ-009:** Every Connection **SHALL** have a runtime `connection_id` controlled by TSN DSS.
- **DSS-CTR-013-REQ-010:** A Connection **SHALL** reference exactly one Provider and one provider-native Device Reference.
- **DSS-CTR-013-REQ-011:** Connection state and Command outcome **SHALL** be modeled independently.
- **DSS-CTR-013-REQ-012:** Connection lifecycle **SHALL NOT** include `unknown_result`; `unknown_result` is a Command outcome.
- **DSS-CTR-013-REQ-013:** A Capability Report **SHALL** distinguish Provider support from current availability.
- **DSS-CTR-013-REQ-014:** A Capability Report **SHALL** include observation time or freshness metadata.
- **DSS-CTR-013-REQ-015:** A Capability Report **SHALL NOT** be treated as proof that a Command will succeed.
- **DSS-CTR-013-REQ-016:** Telemetry and Preview reads **SHALL** be read-only runtime evidence.
- **DSS-CTR-013-REQ-017:** Telemetry **SHALL** distinguish provider-reported state from host-observed evidence when both exist.
- **DSS-CTR-013-REQ-018:** Preview data **SHALL NOT** automatically become Capture, Frame, Dataset, ProcessingRun, Observation or Session state.
- **DSS-CTR-013-REQ-019:** Provider telemetry **SHALL NOT** automatically become SessionContextFact or SessionEvent.
- **DSS-CTR-013-REQ-020:** Every Command **SHALL** have a TSN DSS controlled `command_id`.
- **DSS-CTR-013-REQ-021:** Every Command **SHALL** be explicitly requested by an authorized caller or approved procedure.
- **DSS-CTR-013-REQ-022:** Passive reads **SHALL NOT** create or submit Commands.
- **DSS-CTR-013-REQ-023:** Rejection, validation failure and safety blocking **SHALL** be modeled as alternative Command outcomes rather than sequential states that lead to execution.
- **DSS-CTR-013-REQ-024:** Provider acknowledgement **SHALL NOT** be treated as verified physical success.
- **DSS-CTR-013-REQ-025:** A timeout **SHALL NOT** be treated as proof of physical failure unless independent evidence proves failure.
- **DSS-CTR-013-REQ-026:** Connection loss during a Command **SHALL** leave the Command in `unknown_result` when the effect cannot be determined.
- **DSS-CTR-013-REQ-027:** Non-idempotent physical Commands **SHALL NOT** be automatically retried after timeout, transport loss or `unknown_result`.
- **DSS-CTR-013-REQ-028:** Safety-sensitive Commands **SHALL** be rejected or safety-blocked when required state evidence is unavailable, stale, contradictory or unknown.
- **DSS-CTR-013-REQ-029:** Generic Provider software **SHALL NOT** claim physical clearance without appropriate evidence or explicit operator confirmation.
- **DSS-CTR-013-REQ-030:** Simulator Providers **SHALL** identify simulated Providers, Devices, Connections, Capabilities, Telemetry, Preview and Command outcomes as simulated.
- **DSS-CTR-013-REQ-031:** Simulator Providers **SHALL** provide deterministic behavior sufficient for automated tests.
- **DSS-CTR-013-REQ-032:** Provider runtime state **SHALL NOT** silently create, replace, weaken or redefine canonical TSN DSS domain identities.
- **DSS-CTR-013-REQ-033:** Provider-produced files or preview frames **SHALL NOT** become Capture or Frame without an explicit Acquisition Execution or provenance handoff.
- **DSS-CTR-013-REQ-034:** Acquisition Execution **SHALL** remain outside this contract unless a later approved revision explicitly expands scope.
- **DSS-CTR-013-REQ-035:** Provider identity **SHALL NOT** depend solely on a discovered Device Reference, Connection identity, Command identity, Session identity, Observation identity, Project identity or provider-specific network endpoint.
- **DSS-CTR-013-REQ-036:** Provider discovery **SHALL** define deterministic outcomes for successful discovery, valid empty discovery, nonfatal discovery failure, fatal discovery failure, refresh from `available`, refresh from `degraded` and recovery after nonfatal failure.
- **DSS-CTR-013-REQ-037:** Zero discovered Devices **SHALL NOT** by itself mean Provider failure unless Provider configuration requires a specific Device.
- **DSS-CTR-013-REQ-038:** Connection identity **SHALL NOT** be treated as Provider identity, Device persistence identity, Session identity, Observation identity, Capture identity or Command identity.
- **DSS-CTR-013-REQ-039:** Each Connection **SHALL** permit at most one active nonterminal state-changing Command in this contract version.
- **DSS-CTR-013-REQ-040:** A conflicting second state-changing Command **SHALL** be rejected or safety-blocked before Provider submission unless command-kind policy allows explicit cancellation or controlled shutdown.
- **DSS-CTR-013-REQ-041:** Repeated connect and disconnect requests **SHALL** be deterministic and **SHALL NOT** create duplicate underlying provider calls.
- **DSS-CTR-013-REQ-042:** A completed `disconnected` Connection **SHALL NOT** be reactivated; reconnect **SHALL** create a new `connection_id`.
- **DSS-CTR-013-REQ-043:** Failed Connections **SHALL** be replaced rather than silently reset.
- **DSS-CTR-013-REQ-044:** Every Command **SHALL** identify the Provider and Connection against which it was requested.
- **DSS-CTR-013-REQ-045:** A deadline elapsed event **SHALL NOT** by itself determine physical success, physical failure or final Command outcome.
- **DSS-CTR-013-REQ-046:** A timeout after possible physical execution **SHALL** produce `unknown_result` unless independent evidence proves success, proves failure or proves that no physical effect occurred.
- **DSS-CTR-013-REQ-047:** Ordinary disconnect requests during an active nonterminal state-changing Command **SHALL** be rejected unless a command-kind-specific controlled shutdown or cancellation procedure is defined.
- **DSS-CTR-013-REQ-048:** An `unknown_result` from a non-idempotent physical Command **SHALL** create a runtime unresolved physical uncertainty condition associated with the Provider and Device Reference.
- **DSS-CTR-013-REQ-049:** Unresolved physical uncertainty **SHALL** survive disconnect and reconnect within the running TSN DSS process and **SHALL** safety-block further safety-sensitive Commands until recovery evidence or authorized operator clearance resolves the unsafe precondition.
- **DSS-CTR-013-REQ-050:** Process restart MAY lose runtime-only uncertainty state and therefore **SHALL NOT** establish physical safety; fresh safety evidence or authorized operator clearance is required before later safety-sensitive Commands.
- **DSS-CTR-013-REQ-051:** Every safety-sensitive Command kind **SHALL** define freshness predicates for each required Capability, Telemetry or physical-state item, and stale required evidence **SHALL** safety-block Provider submission.
- **DSS-CTR-013-REQ-052:** Provider runtime state **SHALL NOT** own Project, Session, Observation, Target, SessionPlan, AcquisitionPlan, MosaicPlan, Capture, Frame, Dataset, ProcessingRun, SessionContextFact or SessionEvent semantics.
- **DSS-CTR-013-REQ-053:** Provider Command history **SHALL NOT** become SessionEvent without an explicit domain handoff.
- **DSS-CTR-013-REQ-054:** Seestar-specific RPC names, RTSP ports, authorization files, camera names and modes **SHALL NOT** define the provider-neutral core.
- **DSS-CTR-013-REQ-055:** Discovery **SHALL NOT** create, replace or modify canonical TSN DSS domain records.
- **DSS-CTR-013-REQ-056:** Capability Reports **SHALL** identify the Provider and, when applicable, the Connection to which they apply.
- **DSS-CTR-013-REQ-057:** Static capability declarations **SHALL NOT** replace fresh runtime state when a safety-sensitive Command requires current physical evidence.
- **DSS-CTR-013-REQ-058:** Telemetry samples **SHALL** include host observation time and preserve unknown, stale or unavailable state rather than fabricating current values.
- **DSS-CTR-013-REQ-059:** Reading Telemetry or Preview **SHALL NOT** submit state-changing Commands or alter physical Device state.
- **DSS-CTR-013-REQ-060:** Safety-sensitive Commands **SHALL** require fresh evidence for every physical state precondition they depend on.
- **DSS-CTR-013-REQ-061:** Provider-reported state and authorized operator clearance **SHALL NOT** be represented as independent proof of physical truth or prior Command effect.
- **DSS-CTR-013-REQ-062:** Commands that affect physical equipment **SHALL** have command-kind-specific safety gates before Provider submission.
- **DSS-CTR-013-REQ-063:** Simulator Providers **SHALL** implement the same Provider, Device Reference, Connection, Capability Report, Telemetry, Preview and Command concepts as physical Providers when they claim conformance.
- **DSS-CTR-013-REQ-064:** A Provider, Device Reference, Connection, Capability Report, Telemetry sample, Preview or Command **SHALL NOT** by itself prove that a Session, Observation, Capture, Frame, Dataset or ProcessingRun occurred or exists.
- **DSS-CTR-013-REQ-065:** Provider-native target names, coordinates, object identifiers or device labels **SHALL NOT** replace canonical Target identity.
- **DSS-CTR-013-REQ-066:** Provider telemetry **SHALL NOT** become SessionContextFact without an explicit domain handoff.
- **DSS-CTR-013-REQ-067:** Future Acquisition Execution work **SHALL** preserve DSS-CTR-013 Provider runtime boundaries and DSS-CTR-001 through DSS-CTR-012 ownership semantics.

------------------------------------------------------------------------

## 17. Open Decisions and Deferred Capabilities

The following decisions are deferred:

- whether TSN DSS should later persist Device identity,
- whether Device identity should relate to existing Equipment records,
- how credentials and provider secrets should be configured or stored,
- how Acquisition Execution should convert provider-produced artifacts into Capture and Frame provenance,
- whether provider telemetry should be recorded as SessionContextFact,
- whether provider command history should be recorded as SessionEvent,
- whether MCP should ever expose device Commands,
- how global multi-client arbitration should work,
- whether unresolved physical uncertainty should later be persisted,
- which movement-specific safety policies apply to physical telescope and mount actions,
- whether persistent Command queues or audit logs are required,
- what safety policy is required for physical movement Commands.

Deferred capabilities include:

- real Seestar implementation,
- ASCOM, Alpaca or other Provider implementations,
- HTTP/API/frontend exposure,
- MCP exposure,
- persistent Device registry,
- hardware movement beyond explicitly gated Provider runtime experiments,
- Acquisition Execution.

------------------------------------------------------------------------

## 18. Acceptance Scenarios Required Before Implementation Freeze

A conforming implementation plan SHOULD include deterministic acceptance scenarios for:

- provider discovery success, valid empty discovery, nonfatal discovery failure, fatal discovery failure and recovery refresh,
- repeated connect and disconnect requests without duplicate provider calls,
- reconnect creating a new `connection_id`,
- failed Connection replacement,
- one active state-changing Command per Connection,
- timeout before Provider submission,
- timeout after possible physical execution,
- transport loss after submission and after acknowledgement,
- cancellation racing with possible physical execution,
- ordinary disconnect request during active Command,
- reconnect after `unknown_result` with blocked follow-up safety-sensitive Command,
- stale or contradictory required evidence causing safety blocking,
- passive reads not creating Commands,
- simulator marking across Provider, Device, Connection, Capability, Telemetry, Preview and Command outcomes,
- Provider runtime state not creating or mutating canonical domain records.

------------------------------------------------------------------------

## 19. Authority

Within the TSN DSS operating framework, this document is the authoritative definition of **Device & Provider Runtime**.

Where a dependent contract, policy, procedure, integration or implementation conflicts with the Device & Provider Runtime semantics established here, **DSS-CTR-013 takes precedence for Provider runtime semantics** unless superseded by a later approved revision.

DSS-CTR-013 does not supersede the domain ownership semantics of DSS-CTR-001 through DSS-CTR-012.
