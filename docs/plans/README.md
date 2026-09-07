# Remaining plans

Reviewed on 2026-09-07 against repository commit `a777365` (the unreleased
v1.14.0 code, Store schema 17). This summary replaces the older feature,
review-remediation, Q10, and scheduler-issue plans. Implementation status is
based on current source, existing tests, release documentation, and the user's
field observations recorded below; this review did not run live Home Assistant
or hardware checks.

The recent [application package refactor](application-package-refactor.md)
remains a separate, unchanged implementation plan.

## Open work

| Item | Current status | Remaining outcome |
| --- | --- | --- |
| [Adjacent-room occupancy](#adjacent-room-occupancy) | Topology storage and editing service exist; scheduling gate is absent. | Direct neighbors block new scheduled and dashboard-manual stages. |
| [Bedroom confirmation](#bedroom-confirmation) | Water-notification infrastructure exists; bedroom authorization is absent. | One assigned user and phone approve each bedroom occurrence. |
| [Occupancy investigation](#occupancy-investigation) | Typed vacancy diagnostics and a bounded decision audit exist. | Explain missed windows from evidence; complete audit/UI gaps before considering a correction. |

### Adjacent-room occupancy

The existing [floor-plan model and reducers](../../custom_components/adaptive_robovacs/floor_plans.py)
already store canonical, symmetric area-ID edges and support atomic neighbor
replacement through `adaptive_robovacs.set_room_adjacency`. The
[v1.12.0 release](../releases/v1.12.0.md) explicitly makes those links visual
only. The [v1.14.0 notes](../releases/v1.14.0.md) still identify scheduling
adjacency as future work. Reconcile this existing topology with the new safety
meaning and document migration/activation; do not recreate the service or
silently assume saved diagrams already authorize occupancy blocking.

Remaining requirements:

- Evaluate the target room and its direct, same-floor neighbors symmetrically.
  A-B and B-C must not make C a blocker for A without an A-C edge. Local
  occupancy remains mandatory.
- Apply the same gate to scheduled work, initial dashboard-manual requests,
  and every later stage. An initially blocked manual request creates no latent
  clean; an already-started sequence retains only its unfinished work. A new
  occupied neighbor must not stop an already-running stage.
- Repeat the full occupancy/vacancy check after robot-specific duration
  resolution and at preparation/dispatch boundaries. Reuse the current
  unresolved-observation policy for discovered neighbors; missing saved rooms
  fail closed. Missing or cross-floor references need safe diagnostics and
  auto-clearing, room-scoped Repairs.
- Add **Edit adjacent rooms** to each room card using a native multiple-area
  selector, with explicit save, same-entry/floor validation, and separate
  display of missing references. Show local versus adjacent block reasons.
- Route graph changes and neighbor observations through the typed application
  queue. Preserve strict Store validation, shutdown gating, and cleanup.

Acceptance: reciprocal add/remove, direct-only blocking, missing references,
restart/migration, scheduled/manual continuations, robot-specific forecasts,
Repair lifecycle, and unload behavior are covered. Both dashboard copies agree.

### Bedroom confirmation

There is no bedroom-recipient assignment, bedroom approval record, or assignment
dialog in the current implementation. Reuse
[notification delivery](../../custom_components/adaptive_robovacs/notifications.py)
and [water-confirmation lifecycle patterns](../../custom_components/adaptive_robovacs/application/water.py)
while keeping the two authorizations independent.

Retained product decisions:

- Assign exactly one Home Assistant user and one Companion-app phone per
  bedroom through a room-card dialog and validated backend service. Persist
  registry/config-entry identities and resolve the current delivery target at
  send time. An enabled bedroom without a valid assignment fails closed.
- Send **Clean now** / **Skip** only after otherwise-safe eligibility and
  tentative robot/profile resolution. Approval lasts 30 minutes and triggers
  fresh evaluation; it neither reserves a robot nor starts cleaning directly.
  **Skip** defers until the next daily desired-window start. Timeout leaves
  cadence unchanged and suppresses another prompt for 24 hours.
- Bind approval to the room configuration, requested and resolved profiles,
  program, ordered stages, independent pass counts, and adapter contract.
  Changed behavior or expired approval requires another request. A replacement
  robot must reproduce that fingerprint and pass its own duration forecast.
  Later stages need valid approval for the remaining work; completed stages
  are never replayed.
- Apply bedroom approval to integration-owned dashboard-manual requests too;
  pressing a clean button is not approval. External Home Assistant/vendor-app
  cleans remain observed external work. Preserve all applicable safety gates.
- Request bedroom approval before any separate water confirmation. A mop stage
  requires both authorizations to remain valid; neither response substitutes
  for the other. Skipping unavailable-water mopping leaves vacuum work eligible.
- Persist one bounded request before delivery, restore expiry/throttle state
  across restart, and use unpredictable request-bound actions. Validate every
  supplied user/device identity against the assignment and ignore stale,
  duplicate, expired, or mismatched replies. Clear terminal action material;
  public state and logs must not reveal recipient IDs or action secrets.
- Provide safe assignment/pending/expiry/outcome UI and validated fallback
  approve/skip controls. Missing assignments or delivery failures block only
  that bedroom and create recoverable Repairs. Preview, Party Mode,
  observe-only/storage-safe mode, applicable faults, and shutdown must prevent
  inappropriate prompts or dispatch. Repairs never authorize a clean.

Acceptance: recipient and fingerprint validation, timing, manual requests,
ordered-stage continuation, water independence, persistence, redaction,
translation/Repair cleanup, and unload are covered. Implement after adjacency.

### Occupancy investigation

The old diagnosis plan is partly implemented:
[application policy](../../custom_components/adaptive_robovacs/application/policy.py)
exposes typed vacancy evidence and records changes in eligibility reasons;
[schedule sensors](../../custom_components/adaptive_robovacs/sensor.py) expose
vacancy diagnostics, per-robot eligibility, and the latest decision. The typed
Store retains a bounded audit, with existing planning/state coverage.

Remaining work:

- Collect evidence across several missed desired windows for the affected
  bedroom. Compare occupancy-source transitions, contiguous clear time,
  robot-specific required duration, forecast confidence/sample counts, and the
  final allocation reason. Existing code is not evidence that the historical
  incident has been explained.
- Complete the original audit requirement for a full missed window even when
  its rejection reason is unchanged: `_record_room_decision` currently
  deduplicates unchanged reasons. Add concise room-card diagnostics beyond the
  existing sensor attributes and duration rows if needed to expose the cause.
- Correct a source or forecast only if that evidence proves a defect. Preserve
  immediate occupied blocking; any justified debounce may delay a clear state,
  never a person-present block. Otherwise record the legitimate cause.

Acceptance: each reviewed window has either a safe assignment or an auditable
rejection. Test audit bounds/restart and any demonstrated correction. Keep live
room identifiers, raw sensor payloads, and personal presence history out of the
repository.

## Implemented and closed work

These items are implemented or explicitly closed by the user. Historical release
records remain in [releases](../releases/) and Git history. Partial plans have
their remaining work retained above.

On 2026-09-07, the user confirmed that the Q10 implementation is working well:
two-pass cleans have been observed completing successfully, with no issues
caused by the integration. This supersedes the old plan's outstanding hardware
verification item; Q10 implementation and operational verification are complete
for this backlog.

| Former plan or implemented portion | Evidence retained in the repository |
| --- | --- |
| Per-room daily windows (v1.2.0) | Typed room settings and effective-window policy; current cleaning-period controls also appear in [v1.10.0](../releases/v1.10.0.md). |
| Vendor adapters and native multipass (v1.3.0) | [Release notes](../releases/v1.3.0.md), adapter implementations, and adapter contract tests. |
| Water-aware ordered programs (v1.4.0–v1.4.3) | [Release notes](../releases/v1.4.0.md), durable occurrences, mop-only water gates, and water-confirmation tests. |
| Full-project review remediation (v1.4.4) | [Release notes](../releases/v1.4.4.md); registry identity, strict Store codec, robot-specific forecasting, recovery, lifecycle and presentation implementations/tests. |
| Robot defaults, room profiles and manual actions (v1.5.0) | [Release notes](../releases/v1.5.0.md), profile resolution, room controls, and manual application tests. |
| Q10 two-pass/depth implementation (v1.6.0–v1.6.5) | [Two-pass release](../releases/v1.6.0.md), [depth release](../releases/v1.6.2.md), adapter/profile tests, and user-confirmed successful two-pass operation recorded above. |
| Scheduler allocation/preview agreement | [Pure planner](../../custom_components/adaptive_robovacs/planner.py) consumes per-robot eligibility for assignment and block reasons; planner/application tests and sensor diagnostics cover the shared result. |
| Last-cleaned display and refresh | `format_last_cleaned_age`, timestamp projections, card attribute rendering, and model/dashboard tests. The remaining refresh item was closed at the user's request on 2026-09-08. |
| Hidden initial cadence baseline | `first_scheduler_online_at` is restored/set once and saved before evaluation; due-time policy uses it only without real completion, while the displayed last clean remains unknown. State serialization and model coverage remain. |
| Vacuum despite unavailable water | [Occurrence preparation](../../custom_components/adaptive_robovacs/application/dispatch.py) gates only the mop stage, records separate outcomes and re-evaluates remaining work; dispatch removes mop-only controls from vacuum profile application. |
| Cancellation cadence isolation | Robot-scoped cooldowns replace floor rebasing; typed room deferrals retain provenance, bounded due-time handling, and non-dispatching list/selective-clear services for legacy state. Recovery/planning/service tests remain. |
| Occupancy diagnostic foundation | Typed vacancy results, bounded persisted decision audit, schedule attributes and planning/state tests; investigation and audit/UI completion remain open above. |

## Deferred extensions

The old plans explicitly deferred weekday/weekend and multiple daily cleaning
intervals; multiple bedroom recipients, escalation and day-specific approval
policy; and a third Q10 pass. These remain separate product extensions, not
requirements for completing the initial features above. Additional enhanced
vendor capabilities need their own adapter contract and hardware evidence.

## Implementation baseline

Use the [current architecture](../architecture.md) and [agent guide](../../AGENTS.md)
when work resumes. Extend typed application commands, pure decisions,
Store models/migrations, and immutable projections; coordinator/entities remain
presentation boundaries. Migrate from the schema then present rather than the
obsolete schema numbers in the removed plans.

The old documents also predate current scoped faults, manual-clean behavior,
and [v1.14.0 removals/recovery](../migration-v1.14.0.md). Reconcile those policies
explicitly when implementing new gates; do not restore the old global failure
latch or removed bedroom-transit behavior from obsolete instructions. Preserve
observed robot authority, fresh stage checks, restart-safe pending work,
no automatic retry after uncertainty, shutdown safety, and stable public IDs.
Integration changes follow the normal validation/release procedure; this
documentation cleanup itself makes no integration or deployment change.
