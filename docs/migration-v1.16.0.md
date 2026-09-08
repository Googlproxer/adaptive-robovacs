# Migrating to v1.16.0

Room adjacency protection activates the direct two-way links already saved in
the floor-plan editor. Existing and new rooms default to **Night only**, using a
shared **23:00–09:00** interval in Home Assistant's timezone. Review each room's
**Adjacency protection** selector and the global **Adjacency night start/end**
controls. Rooms without links are unaffected. The existing **Night** cleaning
period remains 00:00–06:00 and does not follow the new protection interval.

An occupied or unresolved neighbour prevents scheduled stages from starting
while protection is active. A neighbour's own cleaning enablement, schedule or
adjacency setting does not suppress its occupancy. Disabled bedrooms can protect
hallways. Rooms without sensors remain non-blocking; the usual radar and fallback
resolution still applies. Manual Clean retains its override and running stages
finish normally. These restrictions do not describe robot navigation routes.

Store schema 17 advances to 18 within the unchanged Home Assistant Store key and
envelope. Supported older schemas migrate automatically. Existing graph geometry,
links, sensor markers, occurrences, checkpoints, holds, room recovery episodes,
cadence, duration learning and audit history are retained. Malformed or newer
payloads remain untouched and use storage-safe observe-only behaviour.

The new native select entities have stable entry/area identities. Existing public
entities and their unique IDs are preserved. Room Status adds adjacency metadata
and robot previews show blockers. No dashboard YAML changes are needed for the
integration-owned cards. Keep the served and standalone JavaScript copies aligned.

Install the exact v1.16.0 release through HACS. A Home Assistant restart is needed
to load the new backend and migrate the Store. If installation is performed
without restarting, the running backend and its existing entities continue until
the next user-controlled restart; new controls are not available yet. Update the
dashboard resource cache key to `?v=1.16.0` and reload the browser when appropriate.
Check both vacuums before a later restart, as described in the release procedure.
