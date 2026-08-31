# Plan: Prevent a cancelled robot from delaying unrelated room cadences

## Observed behaviour

Six enabled rooms shared an almost identical delay between their recorded
last-clean timestamp plus configured cadence and their internal due time.
The rooms are on the same floor. The current cancellation recovery path
rebases every enabled room on a physically cancelled robot's floor, preserving
queue spacing by writing room deferrals. This matches the observed common
cadence drift.

## Goal

A physical cancellation may protect the cancelled robot and its active
occurrence, but it must not rewrite the cadence of unrelated rooms. Each
room's due time should remain last completed plus cadence unless that room has
its own explicit, bounded deferral.

## Design

1. Replace floor-wide cadence rebasing with a robot-scoped cooldown and an
   active-room recovery record. The robot may be temporarily unavailable after
   a physical cancellation, but other rooms retain their natural due times.
2. Limit durable room deferrals to explicit manual work or the affected
   room's own cancellation. Store provenance for every deferral: source,
   creation time, target room, and expiry.
3. The due-time model accepts a deferral only when its provenance applies to
   that room and it remains within the established one-cadence bound.
4. Surface safe scheduler diagnostics distinguishing “robot cooling down”
   from “room manually deferred”, so a delayed clean is explainable.
5. Do not silently erase ambiguous historic deferrals. Offer a reviewable,
   non-dispatching repair or migration report so existing users can choose to
   clear only legacy floor-rebase state.

## Implementation steps

1. Introduce a typed deferral record in durable state and migrate legacy
   timestamp-only values conservatively.
2. Change cancellation handling to record a robot cooldown without iterating
   unrelated rooms or writing their cleaning deferrals.
3. Preserve an interrupted scheduled occurrence for the active room and
   re-evaluate it after robot recovery, subject to all normal safety checks.
4. Update due_at and room projections to ignore stale or incorrectly scoped
   deferrals while retaining manual-clean semantics.
5. Add diagnostics and a repair/recheck path for legacy deferrals, with no
   automatic cleaning command.

## Tests and acceptance criteria

- A cancelled robot affects its active room and its own readiness, but leaves
  other same-floor rooms at their original cadence due times.
- Manual deferral still postpones only the explicitly targeted near-due room.
- A restart preserves an active-room recovery record and expires robot
  cooldown safely.
- Legacy timestamp-only deferrals neither cause an unbounded delay nor get
  silently discarded without a migration decision.
- Add pure models tests for provenance, expiry, and room isolation.

## Rollout verification

After release, simulate a physical cancellation in tests or a non-live fixture.
Confirm preview output marks only the robot/active room as delayed and that
unrelated rooms on the same floor keep their original due timestamps.
