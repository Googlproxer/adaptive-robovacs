# Plan: Durable Cleaning Lifecycle and Restart Recovery

## Goal

Keep room-cleaning records accurate through Home Assistant restarts and
outages while the battery-powered robots continue autonomously. The scheduler
must preserve the room, operation, lifecycle, and timing of every
scheduler-started clean, excluding starts from the native vacuum app.

This lifecycle will also be the shared foundation for future tracked manual
Home Assistant room cleans.

## Durable job checkpoint

Persist a checkpoint before dispatch and after every meaningful transition.
Each job records:

- source (`scheduler` or future `manual_home_assistant`);
- robot, room or rooms, operation, and requested mode;
- requested, accepted, observed-cleaning-start, and last-observed times;
- the room's expected duration at dispatch and a calculated expected end;
- lifecycle phase, completion confidence, and a bounded audit trail;
- optional baseline robot telemetry, such as a total-clean count, for
  confirmation after recovery.

The stored expected duration must not be changed by later dashboard edits.

## Adaptive duration learning

The configured expected duration is an initial prior, not a permanent truth.
The effective duration used for vacancy planning should learn from completed,
room-targeted jobs over time while keeping the user-configured value visible and
unchanged as a fallback.

1. Discover an optional duration-class `sensor` on the same device as each
   robot when its metadata identifies it as cleaning time. Do not hard-code a
   vendor entity ID.
2. Capture the timer value at job start and when the robot leaves the cleaning
   phase. Prefer the resulting robot-reported duration when it is plausible;
   otherwise use the observed cleaning-to-returning interval as a lower-quality
   sample.
3. Store bounded, high-confidence samples per room, operation, robot profile,
   and pass count. A vacuum-only clean, vacuum-and-mop clean, and double-pass
   clean must not train one another's estimates.
4. Do not train on a completion recovered solely from the persisted expected
   end time: that estimate is for continuity after an outage, not new evidence.
5. Until a room has at least three valid samples, keep using the configured
   duration. Thereafter use a robust upper percentile of recent samples (for
   example P80), with outlier rejection and gradual adjustment. This favours a
   room being clear for long enough over an optimistic average.
6. Calculate the vacancy requirement from the learned effective duration plus
   the existing clear-time safety margin. Save the effective duration snapshot
   in each job checkpoint, so a later setting change or newly learned sample
   cannot alter an already running job's expected end.

Expose the learned duration, sample count, sample source, and confidence as
room-sensor attributes. The dashboard may show them as secondary detail, but
the concise **Next Clean** state remains unchanged except for **`In Progress`**
while a job is active.

## Lifecycle and recovery

1. Save the dispatching checkpoint before calling the vacuum service.
2. On service acceptance, persist the accepted phase and calculated expected
   end time.
3. When the robot first reports cleaning, persist the observed start time. On
   ordinary online completion, use the robot state-transition timestamp as the
   completion time.
4. When Home Assistant starts again, retain the checkpoint while the robot is
   cleaning, returning, unavailable, or still loading. Do not schedule another
   room for that robot.
5. If Home Assistant returns after the expected end and the robot is
   docked/idle, recover a previously observed clean as completed at the saved
   **expected end time**. Never use the reconnect time as its completion time.
6. Where available, compare saved and current robot run telemetry to increase
   recovery confidence. Telemetry confirms a run but does not replace the
   persisted expected-end timestamp.
7. If a clean cannot be confirmed—for example, HA lost power before it ever
   observed the robot cleaning—retain an auditable `unconfirmed` outcome
   rather than falsely resetting the room's cadence.

## Next Clean status

While any active tracked job includes a room, that room's **Next Clean** sensor
state must be exactly **`In Progress`**. This applies to scheduler jobs and
future tracked manual Home Assistant jobs, including after a restart.

The sensor attributes should retain enough detail for the dashboard without
changing the concise status text:

- `active_job_source`;
- operation;
- robot;
- observed start time;
- expected end time;
- recovery / completion-confidence state.

Once the job completes, is unconfirmed, or is cancelled, the sensor returns to
its ordinary scheduling state and shows the appropriate result in attributes.

## Validation

Add automated lifecycle tests for:

- restart while dispatching, cleaning, returning, or unavailable;
- an outage that ends before and after the expected completion time;
- normal online completion using the actual state-transition time;
- recovered completion using the expected end rather than reconnect time;
- telemetry-confirmed and unconfirmed recovery paths;
- duration-sensor discovery, timer-delta capture, fallback state-duration
  capture, outlier rejection, and operation/pass-count separation;
- fallback-to-configured duration before three samples, robust learned-duration
  selection after sufficient samples, and exclusion of recovered estimates from
  training;
- no duplicate scheduling while a checkpoint is active;
- **`In Progress`** before, during, and after restart recovery; and
- native-app starts remaining outside room-level tracking.

## Acceptance criteria

- A scheduler-started room clean remains visible as **In Progress** during an
  outage/restart recovery.
- A confirmed clean that finishes while HA is offline updates only its
  recorded room(s), at the persisted expected end time.
- Home Assistant reconnect time never becomes the recorded clean time for an
  offline completion.
- Ambiguous outcomes do not silently advance a room's cleaning schedule.
