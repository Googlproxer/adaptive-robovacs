# Plan: Adjacent-room occupancy blockers

## Goal

Let users declare that two discovered rooms are adjacent. Occupancy in either
room then prevents the scheduler from starting a clean in the other room.

## Product decision

Adjacency is symmetric, not directional. If rooms A and B are linked, A being
occupied blocks B and B being occupied blocks A. Only direct, single-hop
neighbors are evaluated; adjacency is not transitively expanded. The first
release includes an editor launched from each target room card.

## v1.4.4 baseline and gap

Ordinary rooms evaluate only their own radar/fallback sources. Bedroom-transit
rooms additionally require every labeled bedroom to be clear. There is no
durable room-adjacency graph or diagnostic that distinguishes local occupancy
from an adjacent-room block.

The scheduler now loads through the typed schema-v6 `SchedulerState` codec,
uses registry identity for durable robot-owned data, selects a robot before its
duration-dependent vacancy forecast, and gates/drains coordinator-owned tasks
during config-entry unload. Adjacency is a room-level gate and does not depend
on robot identity, but it must participate in both the pre-assignment candidate
decision and every post-assignment safety recheck. Its state, services,
projections, timers, and Repairs must use the reviewed boundaries rather than
adding new direct mutations to the compatibility runtime dictionary.

The dashboard is no longer one aggregate panel: each room has its own
`custom:adaptive-robovacs-room` card, which currently renders integration-owned
entities in one native entities card. Home Assistant has no multi-area entity
domain, so adjacency must not be represented as comma-separated text or a
single-choice select merely to fit the existing renderer.

## Proposed behavior

- Store a canonical undirected graph as sorted area-ID pairs, not duplicated
  one-way settings. Area IDs come from Home Assistant's area registry.
- A room's mandatory occupancy scope is itself, its direct neighbors, and, for
  bedroom-transit areas, the existing all-bedroom scope.
- Any observed occupied room blocks a new clean and identifies the blocker in
  safe status data. A custom adjacency cannot weaken local or bedroom-transit
  rules.
- Reuse the scheduler's existing unresolved/no-sensor policy for a discovered
  adjacent room. A saved reference that no longer resolves to a discovered
  scheduler room fails closed until discovery recovers or the edge is removed.
- A missing saved area or cross-floor relationship is a recoverable
  configuration problem. Show it on both affected room cards and create one
  translated, deduplicated Repair for user action. Auto-clear that Repair when
  discovery recovers or the edge is removed.
- Do not stop an already-running stage when an adjacent room later becomes
  occupied, matching the existing start-safety model. Every later stage in an
  ordered cleaning occurrence is a new start: it must recheck adjacency and,
  when blocked, persist the remaining sequence until a newly valid safe window.
- Adjacency occupancy is a pre-dispatch safety gate, not a failed clean start.
  A block never engages the v1.3 system-wide dispatch halt.
- A pooled duration estimate must not be used to approve adjacency/vacancy as a
  final dispatch decision. After a robot is selected, rerun the complete scope
  with that robot's operation/pass-specific duration before profile preparation
  and again at the final dispatch boundary.
- Discovery refreshes, neighbor state changes, dialog saves, and Repair fix
  flows may request an evaluation only through the config-entry-owned task
  gate. Once unload begins they may update nothing and dispatch nothing.

## Implementation plan

1. Add pure graph normalization and occupancy-scope decisions in `models.py`.
   Return `allowed`, conservative confidence, stable reason codes, and direct
   blocking area IDs. Cover local, neighbor, unresolved, missing-reference,
   and bedroom-transit combinations in `tests/test_models.py`.
2. Extend typed `SchedulerState` with canonical adjacency edges and strict
   decoding. Normalize each edge lexically, deduplicate it, reject self-edges,
   and retain temporarily missing area IDs for diagnosis. Bump from the Store
   schema current at implementation time, migrate existing Stores to an empty
   graph, make the migration idempotent, and treat malformed current-schema
   edge structures as storage-safe-mode errors rather than silently repairing
   them.
3. Add `adaptive_robovacs.set_room_adjacency` with one target area and a
   multiple-area selector. On save, replace all edges touching the target in
   one coordinator-locked typed-state mutation, validate same-floor discovered
   rooms, persist once, and request a side-effect-free dry preview. Reject the
   mutation while storage safe mode or config-entry shutdown is active.
4. Add an **Edit adjacent rooms** action to each room card. It opens a custom
   dialog backed by Home Assistant's native multiple-area selector, populated
   from the selected entry's discovered scheduler rooms. Exclude the room
   itself and other floors, show missing saved references separately, and save
   explicitly through the backend service. Keep the service available for
   scripts/Developer Tools; do not store display names from the dialog.
5. Build the effective occupancy scope once per evaluation, then reuse the
   existing radar-preferred resolver for every member. Cache room resolutions
   within the evaluation so reciprocal edges do not duplicate state reads.
   Apply the same gate to an occurrence's first stage and every resumed stage,
   then repeat it after robot-specific duration resolution and immediately
   before profile application/dispatch.
6. Include blocker kind (`local`, `adjacent`, or `bedroom_transit`), adjacent
   room names, missing references, and confidence in room status and schedule
   preview data produced by `projections.py`. Add stable room-owned entity roles
   so only the selected room card receives them. Keep names presentation-only
   and durable references area-ID based; entity code must consume coordinator
   accessors/projections rather than mutable Store data.
7. Subscribe through the normal occupancy entity watch set. A neighbor's state
   change should update both rooms' previews and cause an ordinary safe
   evaluation through the config-entry task tracker, never a direct dispatch.
   No queued refresh or delayed callback may run after shutdown begins.
8. Add translated missing-reference Repairs under the corrected
   `issues.<key>.fix_flow` schema. The non-dispatching fix flow refreshes
   discovery and directs the user to the affected room cards; it never clears
   or resumes an unrelated scheduler dispatch fault. Give each issue stable
   entry/area data and include the family in config-entry removal so cleanup
   does not require live discovery or `runtime_data`.
9. Document symmetric, direct-only behavior and keep both dashboard JavaScript
   copies byte identical.

## Validation

- Test that adding A-B creates one canonical edge and both rooms report each
  other; removing it from either editor removes both directions.
- Test occupied A blocks B and occupied B blocks A.
- Test A-B and B-C does not make C a blocker for A unless A-C is also stored.
- Test duplicate/self/cross-floor writes, missing saved areas, and Store
  migration/restart. Test that malformed current-schema edges enter storage
  safe mode without overwriting the saved payload, while an older valid payload
  migrates exactly once.
- Test interactions with desired windows, unresolved occupancy, Party Mode,
  observe-only mode, and the stricter bedroom-transit aggregate.
- Test adjacency becoming occupied between ordered vacuum/mop stages: the
  running stage is not stopped, the remaining stage does not start, and it
  resumes only through a fresh safe-window evaluation.
- Test target/entry validation, the per-room native multi-area dialog, room-card
  ownership, missing-reference Repair lifecycle/translation placeholders, and
  dashboard-copy equality.
- Test unload with a neighbor-triggered evaluation queued and with the
  adjacency service holding the coordinator lock; no callback, projection
  update, profile call, or dispatch may occur after closing begins. Test
  config-entry removal clears the adjacency Repair family without runtime data.
- Run the repository unit tests and compile every integration Python module.

## Acceptance criteria

- Users can edit a room's direct neighbors from that room's card using
  discovered Home Assistant rooms and a native multiple-area selector.
- A link behaves symmetrically and survives restart without duplicate edges.
- Occupancy in any direct neighbor prevents a new clean in the target room.
- Each separately dispatched stage counts as a new clean for adjacency safety.
- Missing references and unavailable observations are visible and never
  silently treated as known vacant.
- Robot-specific duration and final safety reevaluation can still veto a room
  after the initial adjacency check; adjacency never becomes a reservation.
- Shutdown and corrupt Store handling fail closed without losing or rewriting
  the user's saved graph.
- Existing safety gates remain independent and mandatory.
