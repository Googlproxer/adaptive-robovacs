# Adaptive RoboVacs feature plans

These documents break [GitHub issue #2, **Features**](https://github.com/Googlproxer/adaptive-robovacs/issues/2)
into one implementation plan per checklist item. They are planning artifacts;
none of the issue items is complete until its plan has been implemented,
validated, released, installed through HACS, and verified in Home Assistant.
Plan 5 also absorbs [issue #4, **Manual operation
triggering**](https://github.com/Googlproxer/adaptive-robovacs/issues/4), because
manual room actions depend on the same effective-profile and adapter dispatch
work.

## Baseline

Plans 1 through 3 are implemented. The remaining plans target the architecture
deployed in integration version 1.4.4:

- v1.1 introduced one global card, one card per vacuum, and one card per room;
- v1.2 introduced independently inherited per-room daily windows;
- v1.3.0 introduced the typed generic/vendor adapter contract, Roborock native
  two-pass dispatch, room pass controls, system-wide dispatch-failure halt, and
  Home Assistant Repairs integration;
- v1.3.1 made adapter capability/profile discovery dynamically add controls;
- v1.3.2 corrected the Repair translation and placeholder contract; and
- v1.4.0 introduced water-aware ordered cleaning occurrences, one cadence per
  room, robot cleaning-program defaults with room overrides, independent
  vacuum/mop pass settings, authoritative Roborock water checks, and explicit
  one-hour water confirmation for mop-capable robots without telemetry;
- v1.4.1 made program controls and late-loading vendor capabilities refresh
  dynamically;
- v1.4.2 added a bounded post-start discovery refresh for vendor entities whose
  initial state transition precedes Adaptive RoboVacs' watcher registration;
- v1.4.3 made adapter mopping verification consume the transient same-device
  operation-selector options directly as well as the normalized profile; and
- v1.4.4 migrated durable robot identity to registry IDs, made duration
  forecasts robot-specific, repaired offline held-job recovery, and hardened
  Store validation and config-entry shutdown/removal.

Pull request [#3](https://github.com/Googlproxer/adaptive-robovacs/pull/3)
introduced the typed Store codec and separated runtime service calls, job
lifecycle mutations, and dashboard projections. The remaining plans use that
refactored shape:

- durable configuration and migrations in `state.py`;
- pure safety decisions in `models.py`;
- Home Assistant observations and service calls in `runtime.py`;
- entity-facing data in `projections.py`; and
- orchestration in `coordinator.py`.

The current durable scheduler payload is Store schema v6. Each remaining plan
must migrate from the schema present when it is implemented rather than assume
that it will receive a particular future schema number.

The v1.4.4 review also establishes implementation contracts that apply to all
remaining plans:

- persist robot-owned configuration and fingerprints against the vacuum's
  entity-registry ID; resolve its current entity ID only for live state reads
  and Home Assistant service calls;
- extend the typed `SchedulerState` codec with an explicit, idempotent migration
  and strict current-schema validation; malformed or newer payloads must remain
  in storage safe mode and must never be overwritten by a fresh schedule;
- perform duration-dependent eligibility only after a compatible robot is
  selected, using that robot's registry-keyed operation/pass history, then
  repeat current safety checks before preparation and physical dispatch;
- create delayed work and evaluation callbacks through config-entry-owned task
  tracking, reject them once shutdown begins, and drain them before unload;
- derive dashboard/entity state in `projections.py` and mutate job lifecycle
  state through the existing typed coordinator/job boundaries; and
- give every new Repair stable, enumerable issue data so config-entry removal
  can delete it without reading unloaded runtime state.

## Plans

| Issue item | Confirmed direction | Plan |
| --- | --- | --- |
| Per-room cleaning windows | One repeating daily interval; weekday/weekend schedules are deferred | [Implemented in v1.2.0](01-per-room-cleaning-windows.md) |
| Multipass support | Implemented for v1.3.0: generic/vendor adapter contract, Roborock mapped native two-pass cross-hatching, dashboard diagnostics, and actionable Home Assistant Repairs; corrected through v1.3.2 | [Implemented plan](02-room-multipass.md) |
| Mopping when water is available | Implemented in v1.4.0 and corrected through v1.4.3: telemetry-backed live readiness, ordered stages, one cadence, and explicit one-hour all-user confirmation when water telemetry is absent | [Implemented plan](03-water-aware-mopping.md) |
| Cross occupancy detection via room list | Symmetric adjacency: occupancy in either room blocks the other | [Adjacent-room occupancy blockers](04-cross-room-occupancy.md) |
| Power level settings per room | Robot-owned program/profile defaults with per-room overrides for program, fan speed, modes, intensity, and independent vacuum/mop passes | [Robot defaults, per-room profiles, and manual room cleans](05-room-power-levels.md) |
| Issue #4: manual operation triggering | One room-owned action for the configured program, one for vacuum only, and one for mop only; no dedicated multipass action | [Combined into plan 5](05-room-power-levels.md) |
| Confirm with message before bedrooms | Assign one user and phone to each bedroom and send an actionable notification for each run | [Bedroom confirmation](06-bedroom-confirmation.md) |

## Live capability findings

The Home Assistant instance was inspected through 2026-08-13 to resolve the issue's
capability questions. These documents intentionally record only portable
integration metadata and behavior, never local entity IDs, device IDs, room
names, map details, or notification targets.

- One Roborock device exposes a complete same-device set for mop attachment,
  water-box attachment, and water shortage. The other robot exposes no water
  telemetry. If its adapter verifies its mop operation, it is conditionally
  mop-capable through explicit per-occurrence user confirmation; otherwise it
  remains vacuum-only.
- Both robots expose Home Assistant's standard fan-speed feature, but their
  advertised values differ. Cleaning mode values also differ, while mop mode
  and mop intensity are available only where supported.
- The v1.3 adapter capability snapshot already carries operation, pass-count,
  fan-speed, cleaning-mode, mop-mode, mop-intensity, and water-readiness fields.
  The remaining plans extend that snapshot and its transient observation
  sources instead of adding vendor checks to scheduler orchestration.
- The current Home Assistant `vacuum.clean_area` action accepts an area but no
  pass count. Home Assistant retains the user-maintained area-to-segment
  mapping, and the Roborock implementation omits the native repeat value when
  cleaning those segments. The revised multipass plan adds an integration-owned
  adapter layer: the Roborock adapter may resolve Home Assistant's mapping at
  dispatch time and issue the native repeat command without persisting a
  second mapping.

## Shared constraints

Every implementation must retain these repository contracts:

- Discover live robots, areas, floors, capabilities, occupancy sources, and
  notification targets from Home Assistant. Never add deployment-specific
  entity IDs, area IDs, device IDs, native segment IDs, service names, or
  credentials to source.
- Home Assistant areas remain the scheduler's public target. Vendor adapters
  may use native commands only after resolving the requested area through the
  selected vacuum's current Home Assistant area mapping. Native target IDs are
  transient and must never be hard-coded, persisted, logged, or projected.
- Keep Party Mode and observe-only mode non-dispatching. Occupancy and
  bedroom-transit rules remain mandatory and are rechecked immediately before
  dispatch.
- Persist scheduler-owned settings, pending work, and migrations through Home
  Assistant `Store`. Robot observations remain authoritative over saved
  estimates after a restart.
- Keep durable vacuum references registry-ID based. Current vacuum entity IDs
  are transient runtime aliases, while existing Adaptive RoboVacs unique IDs
  retain their saved alias so an entity rename cannot duplicate controls or
  detach history.
- Extend the typed Store codec instead of editing serialized dictionaries in
  feature code. Current-schema structural/type/bound violations activate
  storage safe mode; compatibility coercion belongs only in explicit historical
  migrations.
- Put reusable scheduling decisions in `models.py` and cover them in
  `tests/test_models.py`. Add focused state, runtime, service, entity, and
  dashboard contract tests where the implementation boundary warrants them.
- Build dashboard choices from live registries and advertised capabilities.
  Put robot defaults and robot diagnostics on the selected vacuum card. Put
  room overrides, adjacency, assignment, and room diagnostics on the selected
  room card. Keep both dashboard JavaScript copies byte identical and preserve
  existing public entity and unique IDs.
- Log complete dispatch, profile, and notification failures with safe context
  while exposing only generic dashboard errors. No failure may permanently
  exclude a room.
- Surface user-actionable failures as translated, deduplicated Home Assistant
  Repairs issues and safe target-card diagnostics. Do not create Repairs noise
  for normal scheduler waits or transient conditions that need no user action.
- Treat one due room as one durable cleaning occurrence with one cadence and an
  ordered list of independently dispatched stages. Recheck all safety gates
  before every stage, persist remaining work across waits/restarts, and never
  replay a completed stage.
- Calculate vacancy duration with the selected robot's stable identity and the
  exact operation/pass count. A pooled room estimate may be shown before
  assignment, but it must never be the final dispatch eligibility decision.
- Resolve vacuum and mop pass counts independently from robot defaults and room
  overrides. Validate each count against the adapter's support for that exact
  operation rather than assuming vacuum repeat support implies mop repeat
  support.
- Treat any scheduler-selected clean that fails or cannot be confirmed to start
  as a durable system-wide dispatch halt. Create an immediate Repairs error and
  start no further cleans on any robot until the user completes a successful
  non-dispatching recheck and explicitly resumes the scheduler.
- Distinguish normal pre-dispatch waits from system failures. Occupancy,
  adjacency, unavailable water, missing approval, and similar fail-closed gates
  do not constitute an attempted clean and do not engage the global halt.
  Unavailable water skips only the current occurrence's mop stage and never
  blocks a vacuum stage. Profile-application, adapter-dispatch, and
  start-confirmation failures do engage the halt.
- For a verified mop-capable robot without authoritative water telemetry, only
  a valid explicit **Confirm water** action within the request's one-hour window
  authorizes its current mop stage. Cancel, dismissal, timeout, delivery
  ambiguity, or any inability to prove that action is a normal safe cancellation
  and never authorizes mopping.
- Put fix-flow translations directly under `issues.<translation_key>.fix_flow`,
  pass issue translation placeholders into every Repair form, and test the
  served English translation bundle. Repairs that represent recoverable
  configuration gaps must auto-clear when discovery/configuration recovers;
  the scheduler-failure Repair clears only after explicit successful resume.
- Route every integration-owned timer, listener-triggered evaluation, and
  notification/profile callback through the coordinator's shutdown gate. No
  remaining feature may send a notification, apply a profile, or dispatch a
  clean after config-entry unload begins.
- Treat dashboard manual room actions as integration-owned occurrences, not as
  observed external service calls. They may bypass cadence and the room desired
  window, but never occupancy/transit, Party Mode, observe-only/storage-safe/
  halted/closing state, readiness, water/approval, profile, mapping, or
  start-confirmation gates.
- Treat each shipped item as an integration release: bump `manifest.json`, run
  the complete validation suite, create the matching annotated semantic tag
  and GitHub Release, update through HACS, and restart Home Assistant only after
  confirming both vacuums are not cleaning.

## Recommended implementation order

Plans 1 through 3 and the shared refactor are complete. For the remaining work:

1. Complete plan 5's remaining per-room fan-speed, cleaning-mode, mop-mode,
   and mop-intensity overrides together with issue #4's three manual room
   actions. Version 1.4.0 already supplies the shared cleaning-program
   ownership, cadence, independent pass resolution, adapter readiness, and
   ordered-stage checkpoint model; v1.4.4 supplies stable robot identity and
   robot-specific final forecasting. This makes plan 5 the smallest next step
   and gives later safety gates one shared scheduled/manual dispatch path.
2. Implement symmetric room adjacency as an independent occupancy gate and
   per-room-card editor using the typed Store/projection/shutdown boundaries
   established by v1.4.4. Apply it to scheduled and plan 5 manual occurrences.
3. Add durable bedroom assignments and confirmation after adjacency, water,
   and profile gates are stable. Reuse the water-confirmation transport and
   config-entry-owned lifecycle infrastructure without sharing authorization.
   Approval authorizes a fresh evaluation; it never bypasses a safety gate or
   reserves a robot.

The features may be released separately. Each release includes only the
migrations and public controls needed by that release.

## Resolved product decisions

1. The first cleaning-window release supports one daily start/end interval per
   room. Weekday/weekend schedules may be added later.
2. Multipass means Roborock's native two-pass cross-hatching. Roborock is the
   first vendor adapter. Adapters may run native commands, but native targets
   must be resolved from the user-maintained Home Assistant area mapping for
   every dispatch and must never be persisted by Adaptive RoboVacs. Vacuums
   without an adapter retain the portable Home Assistant behavior. This work
   targets v1.3.0 and includes user-visible dashboard diagnostics and Home
   Assistant Repairs issues for actionable adapter failures.
3. Scheduler mopping requires a verified mop operation plus either authoritative
   water/mop telemetry or a fresh explicit one-hour user attestation when water
   is unobservable. Each room has one cadence and uses an effective cleaning
   program of vacuum only, mop only, vacuum then mop, or mop then vacuum. A
   no-water condition skips mopping for that occurrence, still permits
   vacuuming, and retries mopping only at the next scheduled occurrence. For
   vacuum-then-mop, water is evaluated when mop becomes eligible, so water that
   becomes available during vacuuming permits the mop stage. A
   robot with no supported water signal may mop only after the request-bound
   all-user confirmation; lack of an explicit confirm cancels that stage.
4. Cross-room occupancy models undirected adjacency. If either adjacent room
   is occupied, a new clean cannot start in the other.
5. Power means fan speed. Vacuum cards own robot defaults for cleaning program,
   fan speed, cleaning/mop modes, mop intensity, vacuum passes, and mop passes;
   room cards expose **Robot default** plus capability-compatible overrides.
   Existing pass settings migrate as vacuum passes and new mop-pass settings
   default to one. Adapters own capability normalization and stage profile
   application; the generic adapter uses standard Home Assistant actions. Each
   room card also exposes **Manual clean**, **Manual vacuum only**, and **Manual
   mop only**. There is no dedicated multipass manual action; the requested
   operation uses its current effective pass setting.
6. Each bedroom is assigned a Home Assistant user and Companion-app phone. The
   durable assignment uses registry/config-entry identities and resolves the
   current notify target at send time. The assigned phone receives a per-run
   actionable confirmation request.
7. A skipped mop caused by unavailable water sends a non-critical notification
   to every resolvable user's Companion-app targets. One room/water episode is
   notified immediately and at most once per additional 24 hours while it
   persists. Android uses a dedicated user-disableable notification channel;
   iOS receives the notification but has no equivalent per-channel opt-out.
8. Every physical stage repeats window and occupancy safety. If occupancy
   appears between stages, the running stage is observed normally and the
   remaining stage waits for a newly valid safe window. Any attempted stage
   that fails to start engages the durable global scheduler halt.
9. A no-sensor water request contains exactly **Confirm water** and **Cancel
   mopping**, expires and is cleared after one hour, and uses first-terminal
   response semantics across recipients. Only explicit confirm authorizes;
   cancel, a reported dismissal, timeout, or an indeterminate response cancels
   the mop stage while leaving configured vacuum work eligible.
