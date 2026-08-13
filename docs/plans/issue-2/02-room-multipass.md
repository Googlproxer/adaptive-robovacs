# Plan: Vacuum adapters and Roborock native two-pass cleaning

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
dashboard users receive a stable generic reason. Unsupported or failed work
remains due and is not marked unmapped merely because a vendor feature failed.

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
    call clears only the provisional dispatch checkpoint, records a generic
    vendor-dispatch error, and leaves the room due.
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
- Roborock tests prove that a requested Home Assistant area resolves only
  through that entity's current mapping and that only those transient targets
  enter the native repeat payload.
- Persistence and logging tests prove native segment/map identifiers never
  enter Store, checkpoints, entity attributes, previews, or logs.
- Scheduler tests cover robot default, explicit one pass, explicit two passes,
  unsupported requests, allocation across mixed vendors, duration separation,
  and restart recovery during an active two-pass job.
- Hardware verification proves native cross-hatching, safe rejection of stale
  or ambiguous mappings, and a later one-pass clean that is not left in
  two-pass mode.
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

## Upstream references

- [Home Assistant vacuum platform and area mapping](https://github.com/home-assistant/core/blob/dev/homeassistant/components/vacuum/__init__.py)
- [Home Assistant vacuum action schema](https://github.com/home-assistant/core/blob/dev/homeassistant/components/vacuum/services.yaml)
- [Home Assistant Roborock vacuum implementation](https://github.com/home-assistant/core/blob/dev/homeassistant/components/roborock/vacuum.py)
- [Roborock native repeat command example](https://github.com/home-assistant/core/issues/115476)
