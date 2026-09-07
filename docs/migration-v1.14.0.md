# Migrating to Adaptive RoboVacs 1.14.0

Version 1.14.0 removes bedroom-transit handling. Home Assistant 2026.9 and
Python 3.14.2 remain the minimum supported versions. This is a breaking release
because it removes supported controls and a room attribute.

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

The scheduler keeps its Store key, Store envelope version, and schema 16.
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

## Deployment

The local 1.14.0 change is prepared for review and has not been published or
installed. Deployment requires explicit approval. Existing live transit
selectors remain until the new integration is installed and set up.

For a subsequently approved release, follow the repository release procedure:
validate, publish the matching annotated tag and full release, verify CI, install
the exact version through HACS, and restart only after confirming both vacuums
are not cleaning. Verify that the integration loads, obsolete controls are
absent, and retained room and robot state remains intact.
