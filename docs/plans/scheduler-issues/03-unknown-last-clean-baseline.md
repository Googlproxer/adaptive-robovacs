# Plan: Establish a hidden, restart-safe baseline for rooms with no clean history

## Observed behaviour

Entry has no recorded last-clean value. A missing history currently cannot
distinguish “never observed by this scheduler” from “overdue indefinitely”,
and its cadence calculation can be affected by transient deferrals.

## Goal

For every room with no real scheduler-owned completion, calculate cadence from
the first successful time this scheduler entry came online. Persist that
baseline once and never reset it on a Home Assistant restart. Do not expose the
baseline as a real last-clean timestamp to users.

## Design

1. Persist one immutable first_scheduler_online_at timestamp in the
   integration Store, scoped to the config entry. Set it only after discovery,
   durable-state restoration, and initial scheduler setup have succeeded.
2. When a room has no recorded cleaning completion, use that timestamp only
   as an effective cadence anchor. Keep the last-cleaned sensor state unknown,
   and never write the baseline into the room's cleaning-completed field.
3. Existing genuine scheduler, dashboard, or confirmed manual completions
   always supersede the hidden baseline.
4. A Home Assistant restart restores the original baseline before room
   evaluation. It must not postpone every unknown room by another cadence.
5. For an existing entry that predates the field, set the baseline once at
   the first successful post-upgrade scheduler initialisation. Document this
   one-time adoption rule.

## Implementation steps

1. Add the nullable baseline field, schema migration, validation, and
   serialization to the Store state model.
2. Set it atomically during initial coordinator start only when absent, then
   save before any automatic scheduler evaluation.
3. Add a small pure helper that selects real completion or hidden baseline
   for cadence calculation. Route only room due-time calculation through it.
4. Keep sensor projections and dashboard values based on real completion so
   unknown remains visibly unknown.
5. Add a safe diagnostic attribute indicating that an internal baseline is
   being used, without returning its timestamp.

## Tests and acceptance criteria

- A room with no completion becomes due one cadence after the persisted
  baseline, while its last-cleaned sensor remains unknown.
- A coordinator restart at a later time preserves the original baseline and
  due time.
- A true completion replaces the effective baseline immediately.
- Store migration accepts older payloads and produces a valid new payload.
- Verify no test fixture, attribute, diagnostic, or dashboard text leaks the
  hidden baseline timestamp.

## Rollout verification

Use a test config entry with an unknown room history and restart Home Assistant
before its cadence expires. Confirm the due time is stable across restart while
the displayed last-clean value remains unknown.
