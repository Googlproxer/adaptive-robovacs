# Migrating to Adaptive RoboVacs 1.14.0

Version 1.14.0 adds room-scoped robot-error recovery and removes bedroom-transit
handling and the map snapshot/recovery feature. Home Assistant 2026.9.1 and
Python 3.14.2 are the minimum supported versions; the pinned CI interpreter is
Python 3.14.7. This is a breaking release
because it removes supported services, controls, and attributes.

## Removed behavior and interfaces

- The `robovac-bedroom-transit` label (`robovac_bedroom_transit` registry ID)
  no longer affects scheduling. A room's eligibility no longer depends on
  whether every bedroom is clear or on a separate transit daytime window.
- The `hall_start` and `hall_end` configuration fields and global settings are
  removed, along with the **Bedroom-transit start** and **Bedroom-transit end**
  select entities. Their unique IDs ended in `_global_hall_start` and
  `_global_hall_end`; installations may have renamed their entity IDs.
- Room schedule attributes no longer include `bedroom_transit`.

Remove references to these controls and attributes from custom automations,
templates, scripts, and dashboards before upgrading. Delete the obsolete label
through Home Assistant's label registry; deleting it also removes assignments.
Deleting the label while an older integration version is installed can lift
that version's transit restrictions when discovery refreshes.

Previously labeled rooms follow the same room-local occupancy, effective daily
window, and unresolved-occupancy policy as other rooms. The `robovac-bedroom`
label, bedroom defaults, saved room settings, manual-clean behavior, and all
other scheduling safeguards remain. Adjacency is separate future work; this
release does not use floor-plan links for scheduling decisions.

## Automatic cleanup and durable state

The scheduler keeps its Store key and Store envelope version, and advances the
internal payload from schema 16 to schema 17. Schema-16 data is validated before
migration; schema 17 adds a dedicated collection of room recovery episodes.
Obsolete global settings are ignored, even if their retired values are invalid.
An otherwise-valid payload containing them is marked for a validated rewrite
without those keys. All other durable state is preserved, including active jobs,
occurrences, cadence, histories, learned durations, water confirmations,
floor-plan data, and unresolved robot references. Repeated loading is a no-op
after cleanup. Malformed retained data or newer schemas still enter storage-safe
observe-only mode without overwriting the original Store.

After application initialization, setup removes retired keys from config-entry
data and options and removes only that entry's obsolete select registry records.
Matching uses domain, integration platform, stable unique ID, and config-entry
ownership, so renamed controls are retired without changing unrelated entities.
Other entities keep their IDs and histories. Cleanup does not purge recorder data.

## Map snapshot removal

Capture buttons, capture-status sensors, preview selectors and cameras, and
retained-map activation/confirmation dashboard actions are removed. The
`adaptive_robovacs.capture_map_snapshot`, `list_retained_maps`,
`activate_retained_map`, and `confirm_map_selection` services are no longer
registered. The robot hold attribute `requested_map_id` is also removed.

Remove references from custom automations, scripts, templates, and dashboards
before installing this version. The integration dashboard removes these controls
automatically. Historical entity IDs may have been renamed; retired unique IDs
have the prefix `<entry_id>_robot_<original_robot_fragment>_` and these suffixes:

| Entity domain | Retired suffix |
| --- | --- |
| button | `capture_map_snapshot` |
| sensor | `map_recovery` |
| select, camera | `map_recovery_preview` |

Setup removes only matching records owned by that entry, including renamed
entities and robots no longer discovered. It also deletes that entry's obsolete
`adaptive_robovacs.map_recovery.<entry_id>` archive through the Store API without
reading its contents. This deletes archived captures and previews, including
corrupt archives. Missing archives are harmless; filesystem failures are logged
and retried on the next setup. No other entry's archive, scheduler Store,
recorder history, robot-retained map, or floor-plan editor data is deleted.

Old `map_recovery_pending` holds stay blocked across restart. A compatibility
Repair requires confirmation that the robot is correctly localized and its Home
Assistant room mapping is correct. It refreshes discovery, validates each mapped
room, and requires a docked/idle robot with no tracked active job. Opening or
dismissing the Repair does not acknowledge it. Submitting it clears only the
matching hold after a successful save, sends no physical command, and starts no
clean. Stale confirmations and failed saves cannot release another hold. Other
faults, room recoveries, and normal dispatch safeguards remain authoritative.

Obsolete `requested_map_id` fields are ignored and stripped from valid scheduler
payloads, including quarantined robot holds. Schema 17 and the scheduler Store
envelope remain unchanged. Invalid retained data and newer payload schemas
still enter storage-safe mode without overwriting the scheduler Store.

## Recovering interrupted rooms

An error during an integration-owned, single-room scheduled or dashboard clean
creates a persistent, fixable room Repair. The room schedule displays
**Room blocked — recovery confirmation required** with a safe reason. Mapping
and profile faults remain separate and require their own resolution.

The original job and robot hold remain until fresh observations show ten
continuous seconds docked, terminal-ready, and free of robot errors. Startup
settling, unavailable or ambiguous configured diagnostics, and readiness flaps
reset this interval. Adapters without separate error diagnostics use their
existing vacuum and readiness observations. Dock servicing and water readiness
remain subject to normal operation-specific rules.

After that interval, one saved transition detaches the job, releases its robot
hold, retains the occurrence, and resets only its interrupted stage to pending.
The room remains blocked, while the robot can clean other eligible rooms.
Returning to dock after an unresolved error does not count as successful
cleaning, advance cadence, or produce a duration sample. A physical resume
observed before detachment instead resumes tracking and removes that episode's
Repair.

Submit the room Repair to permit the unfinished stage to retry later. Opening
or dismissing it does not acknowledge the interruption. Confirmation verifies
the current episode, detached job, room, and occurrence, clears saved manual
bypasses, and sends no physical command. It can be accepted while the robot
cleans elsewhere. The subsequent scheduler evaluation still checks occupancy,
battery, cleaning windows, water, mapping, profiles, Party Mode, and observe-only
mode. Completed stages, passes, profiles, and the assigned robot are retained.

Existing matching `robot_error` checkpoints are reconstructed on startup using
fresh observations, including errors saved by schema 16. No elapsed-time
estimate establishes completion. An ambiguous association retains its robot
hold and gets a robot Repair requiring safe docking and explicit abandonment
of the old checkpoint. No cleaning is credited by that confirmation.

## Release status

Version 1.14.0 is prepared as a draft GitHub release. It has not been deployed
to Home Assistant. Draft creation does not install it through HACS, change Home
Assistant configuration, reload the integration, or restart Home Assistant.

Before any later deployment, audit and update consumers of the removed controls
and services, publish the approved release, and follow the repository's exact-tag
HACS and safe-restart procedure. Verify integration loading, removal of obsolete
controls and archives, and preservation of room and robot state after upgrade.
