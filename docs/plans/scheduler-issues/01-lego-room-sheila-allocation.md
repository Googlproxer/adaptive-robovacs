# Plan: Assign the eligible LEGO Room clean to its compatible vacuum

## Observed behaviour

The live scheduler preview listed LEGO Room as due, inside its configured
night window, mapped, and unoccupied. The room was shown as ready, yet the
preview contained no assignment and reported “no ready compatible robot”.
The vacuum serving that floor reported ready. The preview's room-level
candidate is therefore not applying the same final eligibility result as the
per-robot allocator.

## Goal

When a room is due and safe, assign the discovered compatible vacuum that
serves its floor. For the current live case that is Sheila, but the
implementation must discover the vacuum and must not hard-code a room or
vacuum name.

## Design

1. Treat a room as scheduler-ready only after its candidate has passed the
   same robot-specific compatibility and vacancy checks used for allocation.
   A preliminary room candidate may remain useful internally, but the public
   “ready now” state must not promise dispatch when every compatible robot
   rejects it.
2. Keep the existing occupancy requirements intact. A confirmed occupied
   room, insufficient clear period, failed vacancy forecast, incompatible
   profile, unavailable mapping, low battery, or held robot remains a
   legitimate rejection.
3. Return a structured, safe rejection reason from the robot-candidate
   resolver. The scheduler preview should identify whether the rejected
   condition is a vacancy forecast, profile/pass incompatibility, map
   incompatibility, or robot readiness condition; it must not expose native
   map IDs or vendor errors.
4. Select the highest-priority resolved robot only after all compatible
   candidates have been evaluated. Preserve one-robot-per-evaluation
   behaviour and the recheck immediately before dispatch.

## Implementation steps

1. Extract the pure per-robot eligibility decision from the coordinator's
   candidate resolution path into models.py or an equivalent typed pure
   result. It should carry allowed, reason code, safe summary, and forecast
   confidence.
2. Make the room preview derive its displayed state from that result. A room
   with one compatible ready robot is assignable; a room with no eligible
   robot is blocked with the most useful safe reason.
3. In the allocator, consume the same result rather than reimplementing
   forecast and profile gates. Maintain the existing final state and
   occupancy rechecks before creating an occurrence.
4. Add a concise scheduler-preview diagnostic for each compatible discovered
   robot: eligible or safely rejected. Keep it to stable room/robot labels and
   reason codes.
5. Update the room dashboard to distinguish “ready to dispatch” from
   “candidate awaiting a safe vacancy” and show the safe block reason.
   Keep the integration-served and standalone JavaScript copies byte-identical.

## Tests and acceptance criteria

- Add model tests for a due, unoccupied, mapped room with one compatible
  ready vacuum and a sufficient vacancy forecast; it produces exactly one
  assignment.
- Add negative tests for each rejection class, especially a room that is
  initially due but fails the robot-specific vacancy forecast. Its public
  state must not be “ready now”.
- Add a coordinator test using neutral fixture room and robot identifiers that
  verifies the allocator and preview agree.
- Verify an occupied room and a bedroom-transit room are never relaxed by the
  fix.
- Run the required unit and compilation checks, frontend tests, and the
  byte-for-byte dashboard-copy check.

## Rollout verification

After release, inspect the scheduler preview during an eligible night window.
The room should either receive one assignment to the discovered floor vacuum
or display the exact safe reason it cannot. Do not dispatch a test clean merely
to verify the preview.
