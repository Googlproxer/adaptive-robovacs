# Migrating to Adaptive RoboVacs 1.13.0

Version 1.13.0 requires Home Assistant 2026.9 or newer and Python 3.14.2 or newer.
The public entities, unique IDs, services, service fields and response keys,
dashboard attributes, and dynamic discovery behavior are unchanged.

## Durable state

The integration continues to use the existing scheduler Store key. On first
load, schemas 1 through 15 are parsed completely and migrated in memory to
schema 16. The first schema-16 write happens only after validation succeeds.
Running the migration again is a no-op.

Schema 16 retains:

- global, room, and robot settings;
- cadence anchors, deferrals, learned duration samples, and first-online time;
- active jobs, stage progress, occurrences, holds, cooldowns, and faults;
- manual, recovery, and room-decision audits;
- pending water confirmations and notification episodes;
- floor-plan rectangles, links, sensor markers, and revision;
- robot entity aliases used by existing entity unique IDs.

Legacy robot keys are matched to current entity-registry IDs through saved
aliases and registry discovery. Records that cannot be matched unambiguously
are retained under `unresolved_robot_references`. They cannot dispatch and an
actionable Home Assistant Repair identifies the legacy key. Restore the
original registry association or re-add the vacuum, then reload the entry.

## Storage-safe startup

Malformed current data and schemas newer than 16 are never replaced with an
empty state. Adaptive RoboVacs starts the affected entry in storage-safe
observe-only mode, exposes a diagnostic snapshot, and creates a persistent
Repair. Back up the Store before repairing it. Reload the config entry only
after the payload is valid or deliberately removed.

The independent map-capture Store is still separate. If it is malformed, map
capture and selection are unavailable and the original archive remains
untouched; ordinary scheduling continues subject to its normal safety state.

## Manual actions

1.13.0 makes one intentional behavior correction: a dashboard manual clean uses
the documented docked-state rule rather than scheduled readiness. It may
bypass cadence, windows, occupancy/transit, configured enablement, battery,
scheduler holds, and the scheduler halt. It still cannot bypass Party Mode,
observe-only mode, storage-safe mode, startup settling, or shutdown, and it
still requires compatible mapping/profile/water checks and confirmed start.

## Upgrade checklist

1. Back up Home Assistant, including `.storage`.
2. Confirm both vacuums are not cleaning.
3. Install the 1.13.0 release through HACS and restart Home Assistant.
4. Confirm Adaptive RoboVacs loads without a storage Repair.
5. Check that room and robot controls retain their entity histories and values.
6. Review unresolved-reference Repairs, if any, before disabling observe-only
   mode.
7. Run a dry-run evaluation and inspect the dashboard before allowing dispatch.

These steps apply to the `v1.13.0` release.
