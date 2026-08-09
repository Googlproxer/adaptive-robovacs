# Plan: Adjacent-room occupancy blockers

## Goal

Let users declare that two discovered rooms are adjacent. Occupancy in either
room then prevents the scheduler from starting a clean in the other room.

## Product decision

Adjacency is symmetric, not directional. If rooms A and B are linked, A being
occupied blocks B and B being occupied blocks A. Only direct, single-hop
neighbors are evaluated; adjacency is not transitively expanded. The first
release includes a dashboard room-list editor.

## Current behavior and gap

Ordinary rooms evaluate only their own radar/fallback sources. Bedroom-transit
rooms additionally require every labeled bedroom to be clear. There is no
durable room-adjacency graph or diagnostic that distinguishes local occupancy
from an adjacent-room block.

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
- Do not stop an already-running job when an adjacent room later becomes
  occupied, matching the existing start-safety model.

## Implementation plan

1. Add pure graph normalization and occupancy-scope decisions in `models.py`.
   Return `allowed`, conservative confidence, stable reason codes, and direct
   blocking area IDs. Cover local, neighbor, unresolved, missing-reference,
   and bedroom-transit combinations in `tests/test_models.py`.
2. Add `adjacency_edges: list[tuple[str, str]]` to durable scheduler settings.
   Normalize each edge lexically, deduplicate it, reject self-edges, and retain
   temporarily missing IDs. Migrate existing Stores to an empty graph.
3. Add `adaptive_robovacs.set_room_adjacency` with one target area and a
   multiple-area selector. On save, replace all edges touching the target in
   one atomic Store update, validate same-floor discovered rooms, and run a dry
   preview.
4. Add a per-room multi-area editor to the custom dashboard. Populate it from
   Home Assistant's area registry, exclude the room itself and other floors,
   show missing saved references, and save explicitly through the service.
5. Build the effective occupancy scope once per evaluation, then reuse the
   existing radar-preferred resolver for every member. Cache room resolutions
   within the evaluation so reciprocal edges do not duplicate state reads.
6. Include blocker kind (`local`, `adjacent`, or `bedroom_transit`), adjacent
   room names, missing references, and confidence in schedule/preview
   projections. Keep names presentation-only and durable references area-ID
   based.
7. Subscribe through the normal occupancy entity watch set. A neighbor's state
   change should update both rooms' previews and cause an ordinary safe
   evaluation, never a direct dispatch.
8. Document symmetric, direct-only behavior and keep both dashboard JavaScript
   copies byte identical.

## Validation

- Test that adding A-B creates one canonical edge and both rooms report each
  other; removing it from either editor removes both directions.
- Test occupied A blocks B and occupied B blocks A.
- Test A-B and B-C does not make C a blocker for A unless A-C is also stored.
- Test duplicate/self/cross-floor writes, missing saved areas, and Store
  migration/restart.
- Test interactions with desired windows, unresolved occupancy, Party Mode,
  observe-only mode, and the stricter bedroom-transit aggregate.
- Test the dashboard multi-area editor and dashboard-copy equality.
- Run the repository unit tests and compile every integration Python module.

## Acceptance criteria

- Users can edit a room's direct neighbors from the dashboard using discovered
  Home Assistant rooms.
- A link behaves symmetrically and survives restart without duplicate edges.
- Occupancy in any direct neighbor prevents a new clean in the target room.
- Missing references and unavailable observations are visible and never
  silently treated as known vacant.
- Existing safety gates remain independent and mandatory.

