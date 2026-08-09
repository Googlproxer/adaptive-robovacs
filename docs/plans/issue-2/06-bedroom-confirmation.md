# Plan: Assign bedroom users and confirm each clean

## Goal

Assign a Home Assistant user and Companion-app phone to each scheduled bedroom,
then require that person to approve each scheduler-owned bedroom clean through
an actionable notification. Approval starts a fresh evaluation and never
bypasses occupancy, timing, Party Mode, observe-only mode, robot readiness, or
profile compatibility.

## Product decision

The first release stores exactly one assigned user and one phone notification
target per bedroom. Confirmation is per bedroom and per run. An enabled
bedroom without a complete assignment is blocked safely and does not dispatch.
Multiple recipients and escalation chains are deferred.

Initial timing policy:

- approval is valid for 30 minutes;
- **Skip** defers the room until its next daily desired-window start;
- timeout does not change cadence and suppresses another prompt for 24 hours;
- a new request is required if the operation or cleaning profile changes.

## Current behavior and gap

Bedrooms default to disabled and, once enabled, dispatch like ordinary rooms
when other gates pass. The integration has no bedroom-to-user/phone assignment,
pending approval state, notification action listener, expiry, or prompt
throttling.

Home Assistant Companion actionable notifications return actions through the
`mobile_app_notification_action` event. Each request needs unpredictable action
IDs so a stale or unrelated notification cannot approve a clean. See the
[actionable notification contract](https://companion.home-assistant.io/docs/notifications/actionable-notifications/).

## Configuration model

Persist a bedroom assignment containing only registry/configuration references:

- bedroom Home Assistant area ID;
- assigned Home Assistant user ID; and
- one discovered Companion-app `notify.mobile_app_*` service name.

Never persist credentials, phone identifiers obtained outside Home Assistant,
or a deployment target in source. The dashboard editor resolves user display
names and available mobile notification services at runtime.

## Confirmation behavior

- When an assigned bedroom first becomes an otherwise safe candidate, persist
  one pending request before sending **Clean now** and **Skip** actions to that
  bedroom's assigned phone. Do not reserve a robot or advance cadence.
- **Clean now** opens a 30-minute authorization and schedules a new evaluation.
  Dispatch occurs only after every normal gate passes again and a final runtime
  recheck succeeds.
- **Skip**, expiry, delivery failure, and an invalid response never count as a
  clean. They follow the defer/throttle policy above.
- Do not notify during dry runs, Party Mode, or observe-only mode. Previews show
  the pending requirement without external side effects.
- Backend approve/skip services use the same request validation so the custom
  dashboard remains a fallback response surface.

## Durable state

Add a bounded per-bedroom record containing:

- unpredictable request ID, action IDs, and bedroom area ID;
- assigned user and notify service snapshot;
- operation, due time, resolved cleaning profile, and prompt time;
- status (`pending`, `approved`, `skipped`, `expired`, or `send_failed`);
- approval expiry, next-prompt time, and last safe outcome; and
- response context user ID when Home Assistant supplies one.

Persist before delivery to prevent duplicate prompts across restart. The robot
is never reserved in this record.

## Implementation plan

1. Add pure decisions in `models.py` for prompt eligibility, action matching,
   approval validity, user validation, skip deferral, timeout, throttling, and
   profile equivalence. Test boundaries, duplicates, stale actions, and restart.
2. Extend typed state with bedroom assignments and pending confirmations. Bump
   the Store schema. Migrate existing enabled bedrooms to unassigned/blocked so
   an upgrade cannot send to the wrong person or clean without confirmation.
3. Add a configuration service and dashboard editor with Home Assistant's
   native area and user selectors plus runtime-discovered mobile-app notify
   services. Only labeled bedroom areas are valid targets.
4. Validate that the selected notification service exists and belongs to the
   intended Companion-app integration at configuration and send time. Keep
   service names as configuration, never constants in source.
5. Add `approve_bedroom_clean` and `skip_bedroom_clean` services. Authenticated
   dashboard calls must have a context user matching the assigned user. The
   handlers update confirmation state, save, and request evaluation; they never
   dispatch directly.
6. Listen for `mobile_app_notification_action`. Match only the random action ID
   for the active request. If an event provides `context.user_id`, require it
   to match the assigned user; otherwise the possession of the request-bound
   action on the assigned phone is the authorization signal. Ignore and safely
   log stale, duplicate, unknown, or mismatched responses.
7. Insert confirmation after ordinary room safety/cadence evaluation but before
   robot assignment. On approval, rebuild candidate and assignment data and
   recheck occupancy, water, profile, timing, and global safety immediately
   before dispatch.
8. On notification failure, log safe target/request context, mark `send_failed`,
   expose a generic dashboard reason, and keep cadence due. Never dispatch
   because delivery failed.
9. Add assignment status, pending state, prompt/expiry times, and last outcome
   to room projections. Add bedroom-only approve/skip dashboard controls using
   the same backend services. Keep both dashboard copies identical.
10. Close requests deterministically on dispatch, disablement, exclusion,
    operation/profile change, skip, expiry, completion, and recovery. Prune old
    terminal records to keep Store size bounded.
11. Document assignment, action semantics, privacy, timing policy, and the fact
    that approval never overrides a scheduler safety rule.

## Validation

- Test that an enabled unassigned bedroom is blocked and sends nothing.
- Test that a due assigned bedroom creates exactly one durable request for its
  assigned phone and does not dispatch.
- Test no notifications during dry runs, Party Mode, or observe-only mode.
- Test matching approval, assigned-user validation, occupation after approval,
  forged/stale/duplicate actions, skip, timeout, send failure, and restart.
- Test 30-minute approval, next-window skip, and 24-hour reprompt boundaries.
- Test that non-bedrooms and manual Home Assistant cleans do not use this path.
- Test backend and notification actions share the same validation.
- Run the repository unit tests and compile every integration Python module.

## Acceptance criteria

- Every scheduler-owned bedroom clean requires a matching unexpired approval
  sent to that bedroom's assigned phone.
- An unassigned bedroom fails closed, with a clear dashboard setup message.
- Approval cannot bypass occupancy, windows, adjacency, bedroom transit, Party
  Mode, observe-only mode, robot readiness, water, passes, or fan speed.
- Restart does not duplicate or forget a request, and prompts obey the throttle.
- Skip, timeout, mismatch, and delivery failure never advance clean history.

