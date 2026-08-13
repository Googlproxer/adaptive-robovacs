# Plan: Full-project review remediation

## Status

Implemented for integration version 1.4.4 on 2026-08-13 from the full-project
review performed against the v1.4.3 baseline. Regression, Python compilation,
frontend validation, release, and deployment follow the repository's approved
release procedure.

## Goal

Resolve the eight actionable review findings without weakening scheduler
safety, losing durable work, changing public entity IDs, or making saved robot
configuration dependent on mutable Home Assistant entity IDs.

The work covers:

- restart recovery for interrupted jobs;
- stable robot identity and Store migration;
- robot-specific duration forecasting;
- coordinator task shutdown and config-entry removal;
- strict Store decoding and safe-mode fallback;
- truthful unset profile controls; and
- metadata-driven vendor capability discovery.

## Review findings

| Priority | Finding | Primary location |
| --- | --- | --- |
| P1 | Docked held jobs cannot reach offline recovery because the held transition returns early | `coordinator.py::_async_reconcile_active_jobs` |
| P1 | Durable robot settings and checkpoints use mutable entity IDs instead of registry identity | `coordinator.py::_robot_settings` and entity unique-ID construction |
| P1 | The final vacancy forecast uses pooled duration history before a robot-specific duration is known | `coordinator.py::_room_candidate` |
| P1 | Evaluator tasks are not gated or drained during config-entry unload | `coordinator.py::async_shutdown` and task creation sites |
| P2 | Invalid persisted daily times bypass `StateSchemaError` and storage safe mode | `state.py::GlobalSettings.from_mapping` |
| P2 | Config-entry removal reads `runtime_data` after successful unload and does not remove all owned data | `integration_core.py::async_remove_entry` |
| P2 | An unset robot profile field is displayed as the first advertised vendor option | `select.py::_RobotSelect.current_option` |
| P2 | Mop controls are discovered from option text while available registry metadata is ignored | `discovery_core.py::_find_profile` |

## Safety and migration constraints

- Keep room occupancy, adjacency, bedroom-transit, Party Mode, and observe-only
  gates authoritative and repeat them immediately before every physical stage.
- Keep observed robot state authoritative over persisted estimates after
  restart. Recovery must never replay a command merely because Store says a
  job was active.
- Use registry IDs for durable identity, but resolve current entity IDs only at
  runtime for Home Assistant service calls and observations.
- Preserve existing public entity IDs and stable unique IDs. Any identity
  migration must update storage and entity-registry references deliberately
  rather than allowing duplicate adaptive entities to appear.
- Treat corrupt Store data as a storage-safe-mode condition. Do not silently
  coerce malformed safety windows into a usable schedule.
- Do not allow shutdown, migration, or cleanup work to dispatch a clean.
- Keep both dashboard JavaScript copies and both icon files byte identical.

## Implementation plan

### 1. Establish regression coverage

Add failing tests for each finding before changing production behavior:

1. Restore a held job whose robot is docked after an offline interval and
   verify that expected-duration classification completes or cancels it and
   clears the hold.
2. Rename a vacuum entity while keeping the same registry entry and verify that
   robot settings, disabled state, program, active occurrence, and adaptive
   entities retain their identity.
3. Give a fast and slow robot different duration histories on the same floor
   and verify that the selected robot's forecast controls final eligibility.
4. Queue and start evaluations during unload and verify that shutdown drains or
   cancels them without a post-unload service call.
5. Decode malformed global times and out-of-range numeric settings and verify
   that initialization enters storage safe mode.
6. Remove an already-unloaded entry and verify that all Stores and Repair issue
   families are removed without reading `runtime_data`.
7. Expose supported profile fields whose saved value is null or stale and
   verify that no vendor default is presented as saved.
8. Discover mop controls whose options contain no `water` or `mop` wording but
   whose registry metadata identifies mop intensity or mop mode.

Keep pure recovery, identity, duration, and decoding decisions in `models.py`
or `state.py` where practical and cover them directly. Add coordinator,
entity, discovery, and integration lifecycle tests at their actual boundaries.

### 2. Repair restart recovery

- Move docked held-job classification ahead of the generic `held` early return,
  or make the transition result explicitly distinguish an online hold from an
  offline docked recovery candidate.
- Classify the persisted stage using observed docked state, the offline
  interval, and expected duration.
- Apply one deterministic terminal transition: complete the stage when the
  offline interval is sufficient, otherwise cancel it safely.
- Clear hold metadata and persist the resulting occurrence atomically.
- Never dispatch or replay a clean as part of recovery.

### 3. Migrate durable robot identity

- Define one canonical durable robot key based on the Home Assistant registry
  entry ID already exposed as `DiscoveredRobot.registry_id`.
- Continue to carry the current entity ID as transient runtime data for state
  reads and service calls.
- Add a versioned Store migration that remaps robot settings, active jobs,
  occurrence assignments, duration samples, and other robot-keyed structures.
- Resolve legacy entity-ID keys through the entity registry. Preserve
  unresolved legacy data in a safe, diagnosable form rather than attaching it
  to the wrong robot.
- Audit adaptive entity unique IDs. Introduce an entity-registry migration where
  required so existing entities keep their public entity IDs and history.
- Make the migration idempotent and test repeated startup, entity rename before
  migration, entity rename after migration, and removed robots.

### 4. Make eligibility robot-specific

- Split room-level gates from robot-specific gates so the chosen or pinned
  robot is known before duration-dependent forecasting.
- Calculate learned duration with the selected robot's stable identity and the
  exact operation/pass count.
- Rerun window, vacancy, occupancy, adjacency, transit, readiness, and profile
  compatibility immediately before preparation and again before dispatch where
  current architecture requires it.
- Ensure occurrence previews and physical dispatch use the same resolved
  duration and robot identity.
- Preserve pure forecast logic in `models.py` and test heterogeneous duration
  histories, pinned occurrences, stale candidates, and safety changes between
  selection and dispatch.

### 5. Make coordinator shutdown deterministic

- Route coordinator-owned tasks through config-entry task tracking rather than
  untracked `hass.async_create_task` calls.
- Add a closing state set before listeners are removed. Reject new evaluations,
  refreshes, dispatches, and delayed callbacks once closing begins.
- Serialize shutdown with the coordinator lock, then cancel or drain tracked
  tasks and timers before the entry unload completes.
- Make dispatch check the closing state immediately before any profile or clean
  service call.
- Avoid awaiting or cancelling the currently executing shutdown task itself.
- Test unload while evaluation is queued, while it owns the lock, during a
  delayed callback, and just before dispatch.

### 6. Harden Store validation

- Validate every global daily-time field with the same strict `HH:MM` contract
  used by runtime window resolution.
- Validate numeric cadence, duration, timeout, pass, and threshold fields
  against their documented bounds instead of accepting negative or nonsensical
  values.
- Raise `StateSchemaError` with safe field context for invalid persisted data so
  `async_initialize` activates storage safe mode.
- Keep migration coercion narrowly scoped to explicitly supported historical
  formats; current-schema corruption must not be normalized silently.

### 7. Complete config-entry removal

- Make removal independent of `entry.runtime_data`, which is unavailable after
  successful unload.
- Construct and remove the integration Store from the config-entry ID.
- Delete scheduler-failure, program-compatibility, notification-delivery,
  cleaning-profile, and any other integration-owned Repair issue families.
- Prefer issue IDs that are enumerable from the entry ID and stable registry
  IDs so cleanup does not require live discovery.
- Make cleanup idempotent for partial setup, failed unload, normal removal, and
  a second removal attempt.

### 8. Represent unset profile values truthfully

- Add an explicit `Not configured` option for supported robot-default fields
  and map it to `None` in the Store.
- Do not substitute `options[0]` when the saved value is null or no longer
  advertised.
- Preserve a stale saved value long enough to show an actionable compatibility
  state or Repair rather than silently changing the user's configuration.
- Keep exact vendor values unchanged and scoped to robots that currently
  advertise them.
- Update entity and dashboard contract tests for null, stale, dynamically
  added, and dynamically removed capabilities.

### 9. Prefer metadata-driven mop discovery

- Use same-device entity-registry metadata such as platform, translation key,
  original name, and adapter description key to identify mop intensity,
  mop mode, cleaning mode, and water-flow controls.
- Keep normalized option heuristics only as a fallback for integrations that do
  not publish usable metadata.
- Require device ownership and appropriate entity domain before accepting a
  match; reject maintenance and unrelated mode selectors.
- Add fixtures for Roborock-style values, alternative vendors, incomplete
  metadata, late-loading entities, and ambiguous selectors.

## Recommended implementation and commit order

1. Add the regression tests and strict Store validation.
2. Add stable robot identity and the versioned Store/entity-registry migration.
3. Fix held-job recovery and robot-specific forecasting on top of the stable
   identity model.
4. Add deterministic task tracking, shutdown, and removal cleanup.
5. Correct unset select semantics and metadata-driven discovery.
6. Update documentation, version metadata, and release notes.

The stable identity migration should land before forecasting and recovery start
persisting any new robot-keyed data. Lifecycle cleanup should land before
testing final removal of migrated Stores and issues.

## Validation

Run the required repository checks:

```powershell
python -m unittest discover -s tests -v
Get-ChildItem custom_components\adaptive_robovacs\*.py | ForEach-Object { python -m py_compile $_.FullName }
node --test tests\test_dashboard.mjs
```

Also verify:

- both dashboard JavaScript copies are byte identical;
- `icon.png` is byte identical to the integration brand icon;
- every legacy Store fixture migrates once and reloads without further change;
- entity rename creates no duplicate adaptive entities and changes no public
  entity IDs;
- no Home Assistant service call occurs after closing begins;
- recovery and migration logs contain safe registry context but no raw
  integration errors or deployment-specific data; and
- the worktree contains no unrelated changes.

## Acceptance criteria

- A docked interrupted job reaches a deterministic terminal state after restart
  and never remains held indefinitely solely because recovery returned early.
- Renaming a vacuum entity preserves every robot-owned setting, checkpoint, and
  adaptive entity identity.
- A robot starts work only when its own learned duration fits the current safe
  vacancy window.
- Config-entry unload cannot leave an evaluator capable of dispatching work,
  and config-entry removal deletes all integration-owned durable data and
  Repairs without accessing unloaded runtime state.
- Malformed Store windows and bounded values activate storage safe mode rather
  than crashing setup or permitting an invalid schedule.
- Unconfigured profile fields visibly remain unconfigured, while unsupported
  saved values remain diagnosable instead of becoming an implicit default.
- Mop controls are discovered reliably from registry metadata without matching
  unrelated selectors.
- The full validation suite passes and all repository safety, identity, parity,
  and release contracts remain satisfied.

## Release and deployment

These fixes change the custom integration and its durable state, so the release
must include a semantic `manifest.json` version bump, matching annotated tag,
full GitHub Release, successful CI, HACS installation of the exact tag, and a
Home Assistant restart after confirming both vacuums are not cleaning.

The release notes must call out the robot-identity migration and restart
recovery changes, include validation results, and state that existing public
entity IDs are preserved. After deployment, verify the integration is loaded,
HACS reports the expected version, robot observations remain authoritative,
and renamed or migrated robots retain their settings and entity history.
