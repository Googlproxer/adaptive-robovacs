# Plan: Vacuum adapters and Roborock native two-pass cleaning

**Target release:** v1.3.0.

**Implementation status:** Implemented for v1.3.0; release and live deployment
verification are recorded in the repository history and GitHub Release.

## Goal

Introduce an integration-owned vacuum adapter layer, then use its first vendor
adapter to support Roborock's native two-pass cross-hatched room cleaning. A
vacuum with no matching vendor adapter continues to use Home Assistant's
portable vacuum actions exactly as it does today.

This plan establishes the adapter contract for later vendor-specific
capabilities; it does not implement adapters as separately installed plugins.
New adapters are added as isolated modules in this integration and registered
explicitly.

## Confirmed product decisions

- The code uses the term **adapter**. Each adapter may discover vendor-specific
  capabilities, normalize them for the scheduler, and execute native commands.
- Adapter selection is automatic from stable Home Assistant registry metadata,
  primarily the vacuum entity's integration platform. Friendly names, entity
  IDs, model-name substrings, and user-maintained vendor switches are not
  matching inputs.
- The generic adapter is always available as the lowest-priority fallback. It
  uses the current standard `vacuum.clean_area`, `vacuum.set_fan_speed`, and
  discovered entity controls without vendor assumptions.
- A vendor adapter may override only the operations it improves and delegate
  the rest to generic behavior before dispatch. Once a native dispatch has
  been attempted, a failure must never trigger a generic retry because that
  could start duplicate work.
- Native commands may use vendor segment identifiers, but only after resolving
  the requested Home Assistant area through that vacuum entity's current Home
  Assistant area mapping. The user remains responsible for maintaining the
  mapping.
- Native segment identifiers are transient dispatch inputs. They are never
  hard-coded, copied into Store, written to active-job checkpoints, projected
  to entities, logged, or shown on the dashboard.
- Multipass means either one pass or exactly two native cross-hatched passes.
  The scheduler never emulates two passes with two dispatches and never
  silently downgrades a requested two-pass clean.
- Roborock is the first enhanced adapter because compatible hardware is
  available for live verification. A vendor adapter and even individual models
  may advertise different capabilities.
- User-actionable adapter failures are surfaced through Home Assistant Repairs
  and on the relevant dashboard card. Users must not need system-log access to
  understand the affected robot or room and the safe next action.

## Current behavior and technical boundary

Discovery currently builds one generic `RobotProfile` from entities belonging
to the vacuum's device. `HomeAssistantRuntime` applies discovered select
controls and dispatches every room through `vacuum.clean_area` using a Home
Assistant area ID. That portable path is the required generic fallback.

Home Assistant's current `vacuum.clean_area` action accepts
`cleaning_area_id` only. Internally Home Assistant reads the vacuum entity's
`area_mapping`, converts areas to segment IDs, and calls the vendor entity's
segment-cleaning method. Its Roborock implementation currently sends the
native segment command without a repeat value. Roborock accepts a native
`app_segment_clean` command containing the mapped segments and `repeat: 2`, but
the portable action has no field through which this integration can request
that behavior.

The adapter layer therefore belongs between discovery/scheduling and the
runtime service boundary. It must reuse Home Assistant's mapping as the source
of truth rather than create a second scheduler-owned map.

## Adapter schema

Add an `adapters` package with a small typed contract and explicit registry.
The names below describe responsibilities; exact Python names may be refined
during implementation without changing the contract.

### Adapter identity and selection

Each adapter declares:

- a stable adapter ID and schema version for diagnostics and checkpoints;
- the Home Assistant entity platform(s) it matches;
- a deterministic priority; and
- an asynchronous probe that can reject unsupported protocols or models.

The resolver returns exactly one vendor adapter or the generic adapter. A
matching vendor adapter that cannot enhance a particular request delegates
that request to generic behavior during preflight. Ambiguous vendor matches
are a discovery error and use the generic adapter with a safe diagnostic.

### Normalized capabilities

Adapters return an immutable, vendor-neutral capability object. The initial
schema includes:

- portable area cleaning availability;
- supported native pass counts, initially `{1}` or `{1, 2}`;
- supported cleaning operations;
- fan-speed choices;
- cleaning, mop-mode, and mop-intensity choices;
- normalized mop/water readiness signals for later plans; and
- whether an operation needs vendor-native area dispatch.

Unknown or stale evidence is represented explicitly and never interpreted as
support. The scheduler, entities, and dashboard consume only this normalized
object; they do not branch on `roborock` or another vendor name.

### Dispatch request and result

The scheduler creates a vendor-neutral request containing the vacuum entity,
Home Assistant area IDs, operation, requested/resolved pass count, and the
normalized cleaning profile. The active checkpoint records the adapter ID,
adapter schema version, area ID, operation, profile, and pass count, but no
native target IDs.

Immediately before dispatch, the selected adapter:

1. refreshes or rechecks its capability evidence;
2. validates the requested operation/profile;
3. resolves each requested Home Assistant area through the vacuum entity's
   current Home Assistant `area_mapping` when native dispatch is required;
4. validates and deduplicates the mapped targets without changing their order;
5. confirms the targets are safe for the active map/protocol; and
6. either delegates to the generic action before any native call or performs
   exactly one native dispatch.

The result is a typed accepted, unsupported, mapping-error, or dispatch-error
outcome. Complete technical context is logged without native target IDs;
dashboard users receive a stable generic reason. Unsupported work remains due
and is not marked unmapped merely because a vendor feature failed. Once the
scheduler has committed to starting a candidate, any preflight, dispatch, or
start-confirmation failure also raises the durable scheduler-wide fault below.

## Scheduler-wide start-failure latch

A scheduler-selected clean that fails to start is a system failure, not an
ordinary per-room deferral. The integration must immediately enter a durable
**dispatch halted** state and must not attempt to start any further clean on any
robot until a user explicitly resolves and resumes the scheduler.

### What counts as a start failure

- Adapter preflight rejects the selected job after allocation, including a
  missing, stale, malformed, ambiguous, or unsafe Home Assistant area mapping.
- Applying the selected cleaning profile or invoking the generic/vendor clean
  action raises, is rejected, or exceeds its bounded service timeout.
- Home Assistant accepts the action but the selected robot does not report
  `cleaning` within a bounded start-confirmation deadline and instead remains
  idle/docked, becomes unavailable, pauses/errors, or returns to its dock.
- The integration cannot safely determine whether the command started. An
  uncertain outcome fails closed: do not issue another command that could
  duplicate work.

Normal candidate ineligibility before allocation—occupancy, time window,
battery, a busy robot, Party Mode, observe-only mode, or no compatible robot—is
not a start failure because no start was attempted. User-initiated/native-app
commands are observations, not scheduler attempts, and cannot create this
fault.

### Fault behavior

- Store one typed scheduler fault containing a stable reason code, safe Home
  Assistant registry identity for the robot/room, first occurrence, current
  phase, and whether a native command may have been attempted. Never persist a
  raw exception, native command, payload, map ID, or segment ID.
- Persist and save the fault before any later evaluation can dispatch. Every
  dispatch entry point, including manual integration evaluation, must check the
  same latch. Restart recovery restores the halt before scheduling resumes.
- Clear the failed job's provisional checkpoint only when the integration can
  prove no clean started. For an uncertain outcome, retain a recovery
  checkpoint and let the robot's observed state remain authoritative while the
  global dispatch halt prevents another start.
- Do not stop a clean already in progress. If the failed/uncertain robot later
  reports `cleaning`, track that observed work safely, but keep future
  scheduler dispatch halted until explicit user resolution.
- Do not advance room cadence, record duration learning, or mark a clean
  complete merely because the user acknowledges the fault.
- Create one immediate persistent `IssueSeverity.ERROR` Repair for the config
  entry; there is no retry threshold for a start failure. The Repair identifies
  the affected friendly robot/room, the safe failure class, and the required
  next action without exposing raw integration details.
- Show **Scheduler halted** at the top of the global card and show the matching
  safe failure on the affected vacuum/room card. All previews must state that
  dispatch is halted even if otherwise eligible work exists.
- Dismissing or ignoring the Repair does not clear the latch. Resolution uses a
  dedicated Repairs flow or confirmed global-card **Recheck and resume** action
  that calls the same coordinator method.
- Recheck validates all non-dispatching prerequisites relevant to the fault,
  including entity availability, adapter capability, readiness, and current
  Home Assistant area mapping. It never sends a vacuum command. If checks pass,
  an explicit user confirmation clears the fault and Repair; the next normal
  scheduler evaluation may try work. If the next start fails, the latch and
  Repair are recreated immediately.

## User-visible failures and Home Assistant Repairs

Home Assistant Repairs is the primary notification surface when a failure
requires user intervention. Dashboard diagnostics remain the immediate local
status surface and cover non-actionable or transient states that do not belong
in Repairs.

### Failure classification

Normalize adapter failures into stable reason codes before they reach the
coordinator. The initial classes are:

- `area_mapping_missing`, `area_mapping_stale`, and
  `area_mapping_ambiguous`: actionable mapping failures. If encountered during
  an allocated start attempt, they trigger the scheduler-wide halt and its
  **error** Repair because the user can update the selected vacuum's Home
  Assistant area mapping. A discovery-time warning before allocation is shown
  on the target card without halting dispatch.
- `two_pass_no_longer_supported`: an actionable configuration failure when a
  saved two-pass room no longer has an eligible robot. Create an **error**
  Repair directing the user to restore a compatible robot or change the room
  to one pass/robot default.
- `profile_apply_failed`, `generic_dispatch_failed`,
  `native_dispatch_failed`, `start_confirmation_failed`, and
  `start_outcome_uncertain`: immediate system failures. They create or update
  the single scheduler-halted **error** Repair on the first occurrence; no
  automatic retry or consecutive-failure threshold applies. The Repair gives
  concrete checks: robot availability, vendor integration health, and Home
  Assistant area mapping.
- `adapter_probe_failed`, ambiguous adapter registration, and unexpected
  internal errors: show a safe dashboard diagnostic and log full developer
  context. Do not create a Repair unless the condition can be translated into
  a specific user action.

Party Mode, observe-only mode, ordinary occupancy blocking, a robot being busy,
and a room waiting for its window are normal scheduler states, not failures and
never create Repairs issues.

### Repair issue contract

- Create issues through `homeassistant.helpers.issue_registry` under the
  `adaptive_robovacs` domain. Use deterministic issue IDs derived from stable
  Home Assistant registry/config-entry identity plus the normalized reason;
  never include a friendly name, mutable entity ID, native map ID, or segment
  ID in the issue ID.
- Deduplicate pre-allocation/configuration issues by affected target and reason.
  Deduplicate all start failures into one scheduler-halted issue per config
  entry because the latch prevents a second scheduler start. Keep timestamps
  and safe local identity in issue `data`, not in user-facing identifiers.
- Use `IssueSeverity.ERROR`: the clean is currently blocked and requires
  attention. Reserve `CRITICAL` for Home Assistant-wide panic conditions and
  do not use `WARNING`, which Home Assistant defines for future breakage.
- Event-detected mapping and scheduler-halted issues are persistent across Home
  Assistant restarts. Recreate currently active, actionable issues during
  coordinator recovery without relying on raw exception persistence.
- Provide complete English issue translations in
  `custom_components/adaptive_robovacs/translations/en.json`, with a concise
  title and actionable description. Translation placeholders may contain the
  current robot/room friendly names and safe reason text, but never native
  targets or raw exception messages.
- Link non-fixable issues to a versioned troubleshooting section in the
  repository documentation. Do not direct ordinary users to system logs as the
  only remedy.
- Delete a configuration issue only after the underlying condition is verified
  resolved. Delete the scheduler-halted issue only through the explicit
  successful recheck-and-resume path; neither rediscovery nor a late observed
  `cleaning` state clears it automatically. This also gives a previously
  ignored issue the correct Home Assistant lifecycle.
- Unloading a config entry does not falsely resolve an active issue. Removing
  the config entry removes issues owned solely by that entry.

### Repair flows and dashboard behavior

- Add `repairs.py` with a recheck flow for mapping/capability issues and the
  scheduler-wide dispatch halt.
  The flow explains where the user maintains Home Assistant's vacuum segment
  mapping and re-runs adapter preflight; it completes only when the condition
  is actually valid. It never edits Home Assistant's mapping, exposes native
  IDs, or dispatches a clean.
- The scheduler-halted Repair is fixable only through non-dispatching recheck
  plus explicit user confirmation. The repair flow never performs a test clean;
  the scheduler performs the next real attempt later through normal safety
  gates.
- Project `failure_code`, a concise `failure_summary`, `failure_since`, and
  `repair_active` on the existing matching room or vacuum status entity. Keep
  raw exceptions and vendor payloads out of entity attributes.
- Render an error/status row before ordinary controls on the affected
  per-room or per-vacuum card. The row states what is blocked and the user
  action in plain language. The global card may show an aggregate count but
  must not duplicate every detailed failure.
- Acknowledge/dismiss actions in Repairs do not mark scheduler work complete,
  advance cadence, clear the dispatch-halted latch, or bypass a safety gate.

## Implementation plan

### Phase A: Adapter foundation

1. Add typed adapter identity, match context, normalized capabilities,
   cleaning profile, dispatch request, and dispatch result models. Keep pure
   compatibility and resolution decisions in `models.py` where practical.
2. Add an explicit adapter registry and deterministic resolver. Match from
   entity-registry platform/config-entry/device metadata, not names. Register
   the generic adapter last as the unconditional fallback.
3. Move current profile discovery and runtime behavior behind the generic
   adapter without changing dispatch, entity IDs, unique IDs, stored settings,
   or scheduler behavior for unadapted vacuums.
4. Support partial vendor adapters through preflight delegation. Prohibit a
   fallback or second call after native dispatch begins.
5. Project safe adapter ID, schema version, normalized capabilities, and
   diagnostic reason codes on the existing per-vacuum status entity. Do not
   expose vendor payloads, commands, map IDs, or segment IDs.
6. Add adapter contract tests using fake generic and vendor adapters. Cover no
   match, one match, ambiguous matches, rejected probe, partial delegation,
   native failure without retry, discovery refresh, and restart recovery.

### Phase B: Roborock adapter

7. Add a Roborock adapter selected from Home Assistant's `roborock` entity
   platform. Probe the actual protocol/model capabilities and advertise two
   passes only where the available native command is supported and can be
   mapped safely.
8. Read Home Assistant's current vacuum `area_mapping` at dispatch time. For a
   requested area, require a non-empty mapping owned by the selected vacuum,
   preserve mapping order, deduplicate targets, and reject missing, stale,
   malformed, cross-map, or otherwise ambiguous targets.
9. Normalize Home Assistant segment identifiers into the Roborock command's
   required target form. Replicate Home Assistant's active-map safety: if the
   adapter cannot prove that every mapped target belongs to the active map, it
   must reject two-pass dispatch. Initially restrict support to an unambiguous
   single/active map if that is the only reliable public evidence.
10. For supported two-pass work, call the vacuum's standard
    `vacuum.send_command` boundary with Roborock's native
    `app_segment_clean` payload and `repeat: 2`. Keep the exact payload builder
    isolated and unit tested. One-pass work continues through generic
    `vacuum.clean_area` unless hardware verification proves an explicit native
    one-pass command is required to clear sticky repeat state.
11. Check `VacuumEntityFeature.SEND_COMMAND`, adapter capability, current
    mapping, and robot readiness immediately before the call. A failed native
    call records a generic vendor-dispatch reason, safely resolves or retains
    the provisional checkpoint according to outcome certainty, leaves the room
    due, and engages the scheduler-wide dispatch halt before returning.
12. Add mocked tests for payload construction and prohibited data retention.
    Include numeric and compound segment formats, multiple mapped segments,
    duplicate targets, absent mapping, cross-map ambiguity, unsupported model,
    and a one-pass clean immediately after a two-pass clean.

### Phase C: Room multipass behavior

13. Add nullable `pass_count` to `RoomSettings`: `None` means **Robot
    default**, and explicit values are limited to 1 or 2. Migrate existing
    rooms to `None` without changing current active jobs or duration history.
14. Preserve the existing robot-wide `double_pass` entity and unique ID as the
    robot default. Resolve the effective count per candidate robot because a
    floor may contain robots with different adapters and defaults.
15. Filter eligible robots using normalized adapter capabilities. A room that
    explicitly requests two passes can be assigned only to a robot advertising
    `{1, 2}`. A robot-default request may resolve differently per eligible
    robot and is checkpointed only after allocation.
16. Store the resolved count in the active-job checkpoint and keep learned
    duration samples separate by operation and pass count. Recovery must use
    the checkpointed adapter ID/schema and pass count while treating the
    robot's observed state as authoritative.
17. Add one room-owned select when at least one enabled floor robot advertises
    native two-pass support. Choices are **Robot default**, **1 pass**, and
    **2-pass cross-hatch**. Preview attributes show the requested value and
    per-robot resolution when allocation is not yet known; active diagnostics
    show the assigned adapter and resolved count.
18. Update setup, adapter-authoring, migration, and dashboard documentation.
    Keep both dashboard JavaScript copies byte identical if frontend ordering
    changes.

### Phase D: Failure reporting and Repairs

19. Add typed failure classification and a coordinator-owned scheduler fault
    latch. Persist only the stable reason, affected Home Assistant registry
    identity, first occurrence, phase/outcome certainty, and user-resolution
    state needed for restart-safe deduplication. Do not persist native
    commands, payloads, map IDs, segment IDs, or raw exception strings.
20. Add a Repairs manager that creates, refreshes, and deletes translated
    issues according to the lifecycle above. Cover config-entry setup,
    rediscovery, recovery, unload, and removal without leaving orphan issues.
21. Add `repairs.py` recheck flows for mapping, unsatisfied capability, and the
    dispatch-halted fault. Add a confirmed global-card **Recheck and resume**
    button using the same coordinator method. Reuse adapter preflight in
    non-dispatching mode; never issue a vacuum command from either path.
22. Add safe failure attributes to existing room/vacuum projections and a
    deterministic error row to the matching dashboard card. Recompose cards
    when a failure begins, changes class, or clears, while ordinary state
    updates continue without rebuilding.
23. Add translated English issue titles/descriptions and versioned
    troubleshooting documentation for v1.3.0. Include mapping maintenance,
    compatibility changes, start failure, the scheduler-wide halt, explicit
    resume, uncertain outcomes, recovery, ignore/dismiss behavior, and when
    system logs are useful for an advanced bug report.
24. Add Repairs contract and lifecycle tests: translation completeness, stable
    IDs, immediate halt behavior, restart recreation, repair-flow/card recheck,
    explicit resume, verified deletion, ignored issue behavior, config-entry
    removal, and absence of sensitive/native data from issues and dashboard
    attributes.

## Future vendor adapter rules

- Add a new module and registry entry; never add vendor conditionals to the
  coordinator, projections, entities, dashboard, or generic adapter.
- Match stable Home Assistant registry/platform metadata and probe individual
  capability evidence. Manufacturer/model strings may refine a confirmed
  platform match but cannot establish one by themselves.
- Use Home Assistant area mappings as the only source for native room targets.
  Never ask users to duplicate segment IDs in Adaptive RoboVacs settings.
- Advertise only demonstrated capabilities and fail closed when telemetry,
  mapping, protocol, or active-map state is uncertain.
- Normalize vendor options and errors at the adapter boundary. Vendor payloads
  must not leak into Store schemas or public scheduler entities.
- Provide contract tests, mocked native-command tests, and hardware validation
  notes for every enhanced capability.

## Validation

- Generic parity tests prove an unadapted vacuum makes the same standard Home
  Assistant calls and receives the same scheduler decisions as before.
- Adapter resolver and schema tests cover deterministic selection, partial
  delegation, capability refresh, heterogeneous floors, and no fallback after
  a native attempt.
- Start-failure tests cover preflight rejection, profile failure, generic and
  native service errors/timeouts, accepted-but-never-cleaning, uncertain
  outcome, a late `cleaning` observation, all dispatch entry points, and two
  robots. Each first failure must prevent every later scheduler start until an
  explicit successful recheck and confirmation.
- Roborock tests prove that a requested Home Assistant area resolves only
  through that entity's current mapping and that only those transient targets
  enter the native repeat payload.
- Persistence and logging tests prove native segment/map identifiers never
  enter Store, checkpoints, Repairs issues, entity attributes, previews, or
  logs.
- Scheduler tests cover robot default, explicit one pass, explicit two passes,
  unsupported requests, allocation across mixed vendors, duration separation,
  and restart recovery during an active two-pass job.
- Hardware verification proves native cross-hatching, safe rejection of stale
  or ambiguous mappings, and a later one-pass clean that is not left in
  two-pass mode.
- Repairs tests prove each actionable failure appears once with translated,
  useful instructions, survives restart when appropriate, and that a
  scheduler-halted issue clears only after a successful non-dispatching recheck
  and explicit user confirmation—not dismissal, rediscovery, or late cleaning.
- Dashboard tests prove the affected room or vacuum shows the safe failure
  summary without raw integration errors and that normal scheduler states do
  not appear as failures.
- Run the full repository unit suite, frontend contracts when applicable, and
  compile every integration Python module.

## Acceptance criteria

- Every vacuum resolves to a typed adapter; unknown vendors behave exactly as
  before through generic Home Assistant functions.
- A compatible Roborock performs native two-pass cross-hatched cleaning for
  the segments currently mapped to the requested Home Assistant area.
- Changing or removing the Home Assistant mapping immediately changes or
  blocks the next native dispatch; Adaptive RoboVacs holds no second mapping.
- No native segment ID is hard-coded or persisted, and an unsafe mapping never
  results in a command for a different room.
- Unsupported robots cannot receive emulated or downgraded two-pass work.
- Preview, allocation, active state, recovery, and duration learning agree on
  the selected adapter and resolved pass count.
- An actionable mapping, compatibility, or clean-start failure is
  visible in Home Assistant Repairs and on the affected dashboard card without
  requiring access to system logs.
- Repairs remain deduplicated and actionable, do not expose native identifiers
  or raw exceptions, and disappear only when the underlying failure has been
  verified resolved.
- The first scheduler-selected clean that fails or cannot be confirmed to start
  persistently halts all further scheduler dispatch across every robot. Only a
  successful non-dispatching recheck plus explicit user confirmation resumes
  scheduling; automatic evaluation, restart, Repair dismissal, or a late robot
  state change cannot clear the halt.

## Release scope

This adapter foundation, Roborock two-pass support, room multipass control, and
failure/Repairs experience ship together as **v1.3.0**. Implementation must
bump `manifest.json` from the then-current 1.2.x version to `1.3.0`, create the
annotated `v1.3.0` tag and full GitHub Release, install that exact release
through HACS, restart Home Assistant, and verify the generic fallback,
Roborock native path, dashboard diagnostics, and Repairs lifecycle live.

## Upstream references

- [Home Assistant vacuum platform and area mapping](https://github.com/home-assistant/core/blob/dev/homeassistant/components/vacuum/__init__.py)
- [Home Assistant vacuum action schema](https://github.com/home-assistant/core/blob/dev/homeassistant/components/vacuum/services.yaml)
- [Home Assistant Roborock vacuum implementation](https://github.com/home-assistant/core/blob/dev/homeassistant/components/roborock/vacuum.py)
- [Roborock native repeat command example](https://github.com/home-assistant/core/issues/115476)
- [Home Assistant Repairs developer contract](https://developers.home-assistant.io/docs/core/platform/repairs/)
- [Home Assistant repair-issue quality rule](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/repair-issues/)
- [Custom integration translations](https://developers.home-assistant.io/docs/internationalization/custom_integration/)
