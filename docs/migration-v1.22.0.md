# Migration to v1.22.0

Requires Home Assistant Core 2026.11.0 or newer. Store schema 22 migrates to 23;
the Home Assistant Store envelope remains version 1. Public entity IDs and
unique IDs remain stable.

## Cleaning and docking occupancy

An observed physical clean suppresses new occupancy detections in its known
rooms until the robot is observed docked. Integration-owned scheduled and manual
jobs provide their dispatched area IDs, including robots without compatible
native location reporting. Future queued rooms are excluded. External cleans
use fresh supported location evidence resolved through existing registry area
mappings; an unknown external room does not exempt its floor.

If the room is clear at docking, its valid pre-clean vacancy anchor continues.
If its sensor remains occupied, a new blocking episode starts at docking even
without an on/off transition. Only occupancy after that boundary counts toward
the new episode; a later clear begins a new vacancy interval. Unavailable inputs
or observation gaps censor the interval rather than producing a training sample.

The home room receives an additional 180-second cooldown from docking. The
vacuum entity's explicit Home Assistant area, or its owning device's inherited
area, supplies this room. No new mapping setting is needed. At expiry, unchanged
occupied signals become blocking from expiry onward. Already-docked startup and
repeated docked updates do not start or extend cooldown. A new departure retains
only the remaining previous cooldown while its new clean runs. Overlapping
robots resume a room only after all applicable coverage ends.

Established human occupancy stays blocking. A pause, error or lost physical
evidence removes room exemptions and censors continuity; the remembered clean
can still trigger home-room cooldown at a later observed dock. New work is
blocked in covered rooms, including reserved stage continuation and final
dispatch checks. Normal mode, recovery, adjacency and raw-sensor behavior remain.
An already-running physical clean is not stopped.

A person entering and leaving entirely during a covered clean or cooldown can
be excluded together with robot detections. Persistent occupancy is checked at
the applicable boundary. This policy does not identify individual occupants.

## Learning and restart

When a room first participates in session or home-room handling, its vacancy
learning version advances to 3 and older vacancy samples are retired once.
Those records cannot reliably separate long cleans from human arrivals. Current
valid vacancy continuity is preserved; last-cleaned records, cadence, duration
and battery learning, jobs and unaffected rooms remain intact. Vacancy fallback
continues while samples rebuild. Repeated sessions do not reset new samples.

Checkpoints contain bounded registry identities and area IDs. Their saved room
attribution carries the vacancy anchor without duplicating it per robot. Startup
discards all continuous vacancy anchors across downtime. Only an unexpired dock
cooldown with a previously checkpointed docking timestamp survives; an offline
dock is never assigned an invented timestamp or completed vacancy sample.

Take a matching Home Assistant backup before an eventual deployment. Earlier
versions cannot load schema 23; rolling back requires the matching older Store
as well as the earlier integration code. Prepare and publish the annotated
release tag, wait for passing CI, install that exact tag through HACS, validate
configuration and confirm both robots are not cleaning before restarting.

## Diagnostics and cost

Raw occupancy sensors keep their raw state. Existing room diagnostics expose
`cleaning` and `dock_cooldown` classifications, responsible robots and cooldown
expiry. Both dashboard copies explain these states. Session and cooldown
coverage also blocks neighbour starts when adjacency protection applies.

The implementation uses the existing FIFO command path and one earliest
deadline timer. It makes no additional map requests, history scans or periodic
polls. Unchanged location heartbeats remain exempt from scheduler evaluations,
Store saves and snapshot publications. See [performance measurements](performance.md).
