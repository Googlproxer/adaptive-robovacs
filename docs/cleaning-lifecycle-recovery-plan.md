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
