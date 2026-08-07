# Plan: Track Room-Targeted Manual Home Assistant Cleans

## Goal

Record a manual clean when a user starts `vacuum.clean_area` from Home
Assistant for one of the integration's managed robots and mapped areas. Keep
native-app detection explicitly out of scope.

This work depends on the shared
[durable cleaning lifecycle and restart-recovery plan](cleaning-lifecycle-recovery-plan.md).

## Scope and rules

- Accept only `vacuum.clean_area` calls with a Home Assistant user context,
  a managed vacuum target, and one or more discovered Home Assistant area IDs.
- Exclude scheduler-originated calls, calls without a user context (including
  automations), native-app starts, and whole-home `vacuum.start` calls because
  they do not reliably identify a room.
- Do not apply the scheduler's cleaning profile or mode controls to a manual
  job.
- Preserve the existing policy: a confirmed manual clean defers scheduled work
  by one day only when that room's work was due within the next 24 hours. It
  must not reset the normal room-cleaning cadence.

## Implementation steps

1. Register an `EVENT_CALL_SERVICE` listener during coordinator startup and
   remove it during shutdown.
2. Filter for user-context `vacuum.clean_area` events, then validate the robot
   target and `cleaning_area_id` values against live discovery.
3. Ignore calls that match an existing scheduler-owned active job. Record other
   matching calls as manual Home Assistant jobs, including their source, robot,
   rooms, requested operations, and Home Assistant context ID.
4. Reconcile manual jobs with the robot's actual state. Apply the one-day
   deferral only after the job has entered cleaning and then completed; retain a
   non-disruptive audit event if it never starts or is cancelled.
5. Surface the source and room names in robot activity so the dashboard can
   distinguish scheduler work from a manual Home Assistant clean.
6. Add tests for service-event filtering, multiple selected areas,
   scheduler-originated call exclusion, no-user-context exclusion, completion,
   cancellation, and the within-24-hours deferral boundary.
7. Publish the change and verify it with a manually initiated, room-targeted
   `vacuum.clean_area` action in Home Assistant. Do not test through the native
   vacuum app.

## Acceptance criteria

- A Home Assistant user starts a room-targeted clean and the dashboard shows
  the correct robot, room, and `manual_home_assistant` source.
- The integration does not duplicate or override a scheduler-started job.
- Native-app and whole-home starts remain untracked at room level.
- After confirmed completion, only near-due scheduled work receives the
  existing one-day manual deferral.
