# Plan: Per-room daily cleaning windows

**Status:** Implemented for the v1.2.0 release.

## Goal

Allow every discovered room to inherit the scheduler's existing daily desired
cleaning window or provide its own daily start and end times. Preserve
overnight-window behavior, occupancy safeguards, bedroom-transit restrictions,
and the per-room **Ignore desired cleaning window** override.

## Product decision

The first release supports exactly one repeating daily interval per room.
Weekday/weekend and multiple-interval schedules are explicitly deferred. Start
and end inherit independently from the current global values so a user can, for
example, override only the start time without duplicating the global end time.

## Current behavior and gap

The integration stores one global desired start/end pair under the historical
`unresolved_start` and `unresolved_end` keys. Every room uses that pair unless
its `ignore_desired_window` switch is on. The same pair controls the limited
unresolved-occupancy exception. The requested feature needs room-specific
bounds without renaming existing global entities or weakening that safety rule.

## Proposed behavior

- Add nullable `desired_window_start` and `desired_window_end` to each room.
  An unset bound inherits the corresponding global value.
- Treat the daily window as a half-open interval and allow it to cross
  midnight. Equal start/end values are invalid rather than all day.
- Keep `ignore_desired_window` as a preference bypass for known-vacant rooms.
  It does not bypass unresolved occupancy or bedroom-transit restrictions.
- Show configured and effective bounds, inheritance, and the next effective
  start in room schedule attributes and the dashboard.
- Keep all existing global select and room switch entity IDs stable.

## Implementation plan

1. Add pure helpers in `models.py` to resolve inherited bounds, validate
   `HH:MM`, evaluate ordinary and cross-midnight intervals, and calculate the
   next usable start. Cover boundaries, partial inheritance, and invalid equal
   bounds in `tests/test_models.py`.
2. Extend typed `RoomSettings` with the nullable bounds. Bump the Store schema
   and migrate old rooms to `None`, preserving existing behavior exactly.
3. Use each room's effective bounds in desired-window and unresolved-occupancy
   decisions. Pass resolved values into pure decisions rather than reading Home
   Assistant state from `models.py`.
4. Project configured values, effective values, inheritance flags, and next
   start. Retain the existing `desired_window_start` timestamp attribute for
   dashboard compatibility; do not change its type silently.
5. Add two room-owned selects with stable new unique IDs. Each offers
   15-minute values plus **Use global**. A change saves only that room and runs
   a dry preview.
6. Add the controls to each room section in the registry-driven dashboard and
   document inheritance, overnight behavior, and interaction with the ignore
   switch. Keep both dashboard JavaScript copies identical.
7. Design the Store field as a versioned daily-window object so a future
   weekday/weekend migration can add schedule groups without reinterpreting the
   first release's values.

## Validation

- Test ordinary and overnight windows before, inside, at the end boundary, and
  after the interval.
- Verify existing Store data migrates with all rooms inheriting the global
  pair and that overrides survive restart.
- Verify one room's override does not affect another room's preview.
- Verify **Ignore desired cleaning window** cannot bypass unresolved occupancy,
  Party Mode, observe-only mode, or bedroom-transit safety.
- Run the repository unit tests and compile every integration Python module.

## Acceptance criteria

- Two due rooms can have different daily windows, and only a room inside its
  effective interval becomes a candidate.
- A room with no override behaves exactly as it did before the release.
- Overnight windows and 15-minute boundaries are represented correctly.
- Restarting Home Assistant does not lose or reinterpret room overrides.
- The first release does not expose weekday/weekend or multiple-interval UI.
