# Plan: Assign bedroom users and confirm each clean

## Goal

Assign one Home Assistant user and Companion-app phone to each scheduled
bedroom, then require that person to approve every scheduler-owned bedroom
clean through an actionable notification. Approval starts a fresh evaluation;
it never dispatches directly, reserves a robot, or bypasses a safety gate.

## Product decision

The first release stores exactly one assigned user and one phone target per
bedroom. Confirmation is per bedroom and per run. An enabled bedroom without a
complete valid assignment fails closed. Multiple recipients, escalation chains,
and weekday/weekend-specific confirmation policy are deferred.

Initial timing policy:

- approval is valid for 30 minutes;
- **Skip** defers the room until its next daily desired-window start;
- timeout does not change cadence and suppresses another prompt for 24 hours;
  and
- a new request is required if the cleaning program, ordered stages, vacuum or
  mop pass count, room profile, or tentative adapter-resolved profile changes.

## v1.4.4 baseline and gap

Bedrooms default to disabled and, once enabled, currently dispatch like other
rooms when their existing gates pass. Room cards are target-scoped and already
own room schedule, occupancy, pass, and failure rows. The scheduler now also
has adapter capabilities, water/profile preflight, a durable system-wide
dispatch-failure halt, and translated Home Assistant Repair fix flows.

Plan 3 already provides registry-discovered Companion-app delivery, random
request-bound actions, one-hour persisted water-confirmation state,
action/cleared event listeners, timer restoration, all-target notification
clearing, redacted public status, and shutdown-aware cleanup. There is still no
bedroom assignment, assigned-recipient approval state, bedroom timing policy,
or room-card assignment dialog. Bedroom confirmation should reuse the proven
transport and action-validation infrastructure without sharing authority,
tokens, recipients, or expiry with water confirmation.

The review release also makes robot identity registry-stable, performs final
vacancy forecasting only after compatible robot assignment, strictly decodes
schema-v6 Store state, derives public state in `projections.py`, and gates/drains
config-entry-owned work during unload. New prompts must respect those
boundaries and the global scheduler halt: a halted, closing, or storage-safe
scheduler may show existing safe confirmation status but must not send another
prompt or start a bedroom clean.

Plan 5 is implemented before this plan and adds `manual_dashboard` room
occurrences. Those actions are integration-owned bedroom runs, not external
manual observations, so they require this plan's assigned-recipient approval.
Pressing **Manual clean**, **Manual vacuum only**, or **Manual mop only** is not
itself bedroom authorization and does not bypass the phone workflow.

Home Assistant Companion actionable notifications return actions through the
`mobile_app_notification_action` event. Each request needs unpredictable action
IDs so stale or unrelated actions cannot approve a clean. See the
[actionable notification contract](https://companion.home-assistant.io/docs/notifications/actionable-notifications/).

## Durable assignment model

Persist registry/configuration identities rather than a generated notify
service name:

- bedroom Home Assistant area ID;
- assigned Home Assistant user ID;
- selected Companion-app config entry/device registry ID; and
- notify entity registry ID where the installed Home Assistant/mobile-app
  version provides one.

At send time, resolve those references to the current supported notification
surface: a `notify` entity/action when available or the matching legacy
`notify.mobile_app_*` service. This survives friendly-name/service-name changes
and keeps deployment-specific targets out of source. Never persist credentials,
push tokens, webhook secrets, or device names as authority.

Any tentative robot reference stored with a request uses the vacuum's stable
entity-registry ID. Its current entity ID is resolved only when rebuilding live
candidate state; an entity rename must not invalidate or duplicate an otherwise
valid request. The retained Adaptive RoboVacs entity alias is not a recipient or
authorization identity.

If a legacy installation has no registry-backed notify entity, persist the
mobile-app config-entry/device identity as primary and a validated service name
only as a compatibility hint. Re-resolve and validate it on every send.

## Configuration experience

- Add **Configure bedroom recipient** to each labeled bedroom card. It opens a
  custom dialog using Home Assistant's native user selector and a discovered
  Companion-app phone/notify selector. Non-bedroom room cards do not show it.
- Save through one backend assignment service that validates the selected
  entry, bedroom area, user, mobile-app device, and notify surface atomically.
  Keep the service available for Developer Tools/automation use.
- Project safe display names and validation status to the selected room card,
  but never expose raw user IDs, config-entry IDs, device IDs, action tokens, or
  notify service names in public state.
- Missing, stale, or invalid assignments create a translated, deduplicated
  room-scoped Repair and dashboard diagnostic. They auto-clear when the
  assignment becomes valid or the bedroom is disabled/excluded.

## Confirmation behavior

- When an assigned bedroom becomes an otherwise safe candidate, select a
  tentative compatible robot, cleaning program, ordered stages, and fully
  configured profile. Apply that robot's operation/pass-specific duration to
  the final vacancy forecast before persisting one request and sending **Clean
  now** and **Skip** actions. Do not reserve the robot, create an active stage,
  or advance cadence.
- The request fingerprint contains the cleaning program, ordered stage kinds,
  separate vacuum/mop pass counts, requested room profile, tentative resolved
  profile, adapter ID/schema, and relevant room configuration version. It
  contains no native map/segment targets.
- **Clean now** records a 30-minute authorization and schedules a new
  evaluation. Assignment is rebuilt. Dispatch may use only a robot whose
  freshly resolved profile matches the approved fingerprint; otherwise the
  request is invalidated and a new confirmation is required.
- A renamed robot with the same registry identity remains the same tentative
  robot. A different compatible robot may be selected only when its program,
  complete requested/resolved profile, adapter contract, and pass fingerprint
  are identical to what was approved; its own duration forecast and every live
  gate are still rerun. Approval is never evidence that another robot fits the
  current safe window.
- Every normal gate is rechecked before the first stage and every later stage:
  scheduler halt, Party Mode, observe-only, daily window,
  local/adjacent/bedroom-transit occupancy, robot readiness, telemetry-backed
  water readiness or explicit water confirmation when the current stage is mop,
  profile compatibility, operation-specific pass support, and current Home
  Assistant area mapping/preflight.
- One approval authorizes the immutable scheduled occurrence, not arbitrary
  future room work. A later stage may proceed without another prompt while the
  same approval remains valid. If occupancy or another gate defers the sequence
  beyond the 30-minute authorization or into a later safe window after expiry,
  send a new confirmation for the remaining-stage fingerprint. Completed
  stages are never replayed.
- A water-unavailable mop stage is skipped under plan 3 and does not require a
  replacement approval. Any configured vacuum stage may still proceed. The
  all-user skipped-mop notification is separate from this bedroom's assigned
  recipient confirmation and carries no approval authority.
- A no-sensor robot's all-user **Confirm water / Cancel mopping** request is also
  separate from bedroom authorization. Send the bedroom's assigned-recipient
  request first. Only after valid bedroom approval and a fresh otherwise-safe
  evaluation may plan 3 send the water request. The mop must start before both
  independent authorizations expire; neither action can satisfy the other.
- **Skip**, expiry, invalid response, and delivery failure never count as a
  clean. They follow the defer/throttle policy and leave cadence due.
- Dry runs and schedule previews never send. No new prompt is sent while Party
  Mode, observe-only/storage-safe mode, config-entry shutdown, or the global
  scheduler halt is active.
- Dashboard approve/skip actions use the same backend validation and are a
  fallback response surface, not a separate authorization path.
- A plan 5 manual bedroom request bypasses cadence and the room desired window
  only after approval; every other bedroom gate remains mandatory. If initial
  non-approval safety checks fail, reject the press without sending or queuing
  a prompt. Once otherwise eligible, persist the manual mode/profile fingerprint
  and send the normal assigned-phone request before creating an active stage.

## Action validation and privacy

- Generate cryptographically unpredictable request and action IDs and match
  only the currently active request.
- When an action event supplies a Home Assistant context user, require the
  assigned user. When it supplies a Companion device identifier, require the
  assigned device. If a platform omits one field, require every identity it did
  supply plus possession of the request-bound random action.
- Ignore and safely log stale, duplicate, unknown, expired, wrong-user, or
  wrong-device responses. Logs contain stable safe context, never action tokens
  or notification payload secrets.
- Manual Home Assistant or vendor-app cleans are not confirmation requests and
  continue through the existing observation/manual-tracking path.

## Durable request state

Add one bounded record per bedroom containing:

- request status and timestamps;
- bedroom area reference and an assignment-version reference;
- cleaning-program/ordered-stage/vacuum-pass/mop-pass/requested-profile/
  resolved-profile fingerprint and, for a continuation, completed/pending
  stage indexes;
- tentative stable robot registry ID and adapter ID/schema, without the current
  robot entity ID or native targets;
- approval expiry and next-prompt time;
- last safe outcome; and
- response user/device match results as booleans, not copied identifiers.

Persist the record before delivery so restart cannot duplicate a prompt. Keep
random action IDs in Store only for the active request, redact them from
diagnostics, and remove them when the request becomes terminal. The robot is
never reserved in this record.

Add the record through the typed Store codec with an explicit idempotent
migration from the schema current at implementation time. Current-schema
structural, token, timestamp, or bounded-value corruption enters storage safe
mode without overwriting the saved payload. Recovery reconciles a request with
the observed active/held job and ordered occurrence; it never replays a prompt,
profile, or clean merely because Store says authorization was active.

## Failure and Repairs behavior

- Missing assignment, stale registry references, or notification delivery
  failure occur before a cleaning attempt. Block only that bedroom, leave its
  cadence due, and create/update an actionable room-scoped Repair. Other rooms
  and robots remain schedulable.
- Repair translations use `issues.<key>.fix_flow` and receive assignment-safe
  placeholders. The fix flow refreshes discovery and directs the user to the
  bedroom card; it does not send a notification or clean.
- After an approval, any stage profile-application, adapter-dispatch, or
  start-confirmation failure is an actual failed scheduler clean and therefore
  uses the existing durable system-wide halt and scheduler-failure Repair. No
  remaining stage or unrelated clean may start until explicit successful
  resume.
- Dismissing a bedroom-assignment/delivery Repair does not authorize a clean.
- Assignment/delivery Repair IDs include stable entry and area context and are
  included in config-entry removal. Cleanup must work after unload without live
  coordinator discovery or `entry.runtime_data`.
- Notification delivery, action handling, expiry timers, and requested
  evaluations are config-entry-owned work. They check the closing gate before
  send/mutation, are cancelled or drained during unload, and cannot emit a
  notification or evaluation after platform teardown begins.

## Implementation plan

1. Add pure decisions in `models.py` for prompt eligibility, whole-occurrence
   and remaining-stage fingerprints, action matching, approval validity,
   user/device validation, skip deferral, timeout, throttling, and invalidation.
2. Extend typed Store state with bedroom assignments and bounded confirmation
   records, using stable area/user/mobile-app/notify registry identities and a
   tentative robot registry ID. Bump from the schema current at implementation
   time with strict current-schema validation and an idempotent migration.
   Existing enabled bedrooms migrate to unassigned/blocked and send nothing
   until the user configures them.
3. Extract/reuse plan 3's registry-driven mobile-app resolver, delivery,
   action-token, clear, and timer infrastructure for current notify
   entities/actions and validated legacy services. Keep bedroom request state
   and authority separate from water confirmation, keep transport details out
   of scheduling decisions, and redact them from every projection/log.
4. Add the assignment service and per-bedroom-card native-selector dialog.
   Validate only discovered areas labeled as bedrooms and preserve target/card
   ownership attributes.
5. Add approve/skip services. Require authenticated dashboard callers to match
   the assigned user; handlers only mutate/save confirmation state and request
   evaluation through config-entry-owned task tracking.
6. Listen for `mobile_app_notification_action` and apply random action,
   user/device, status, expiry, and assignment-version validation. Never
   dispatch from the event handler.
7. Insert initial prompt creation after ordinary candidate gates and tentative
   program/profile resolution and the tentative robot's exact duration forecast,
   but before occurrence/active-stage creation. Add the same authorization
   check before a deferred remaining stage and suppress side effects in dry run,
   Party Mode, observe-only/storage-safe, closing, and scheduler-halted states.
8. On approval, rebuild candidate/assignment/program/profile data and require
   the approved whole-occurrence or remaining-stage fingerprint. Resolve the
   current entity from stable robot identity, recalculate duration for the
   selected robot, and repeat every current safety/preflight check immediately
   before the dispatch path.
9. Implement local assignment/delivery Repairs and diagnostics separately from
   the global scheduler-failure latch. Make the issue family enumerable and
   removable from entry ID/Store area data without live runtime state.
10. Add bedroom-card assignment status, pending/approved state, prompt/expiry
    times, and approve/skip controls from safe `projections.py` data. Keep both
    JavaScript copies identical and do not prefix every row with the bedroom
    name.
11. Consume action tokens on approval/skip and retain only the bounded
    authorization metadata needed by an in-progress occurrence. Close requests
    deterministically on disablement, exclusion, assignment/program/profile
    change, skip, expiry, occurrence completion, and recovery. Prune terminal
    records and action tokens. Reconcile reviewed held/offline job outcomes
    without replaying completed stages or retaining authorization for cancelled
    work.
12. Route notification sends, timers, action-triggered evaluations, and cleanup
    through the coordinator shutdown gate and config-entry task tracker. Test
    and document assignment, privacy, action semantics, timing, shutdown, and
    the fact that approval never overrides a scheduler safety rule.

## Validation

- Test that an enabled unassigned bedroom is blocked, sends nothing, and shows
  one actionable assignment Repair/room diagnostic.
- Test that one otherwise-safe assigned bedroom persists exactly one request
  before sending to its resolved phone and does not reserve or dispatch a robot.
- Test no notifications during dry runs, Party Mode, observe-only, or the
  system-wide scheduler halt.
- Test matching approval, assigned-user/device validation, occupancy after
  approval, adapter/profile changes, forged/stale/duplicate actions, skip,
  timeout, send failure, and restart.
- Test a vacuum entity rename with unchanged registry identity before and after
  approval. The request remains attached once, public adaptive entities do not
  duplicate, and any later live call uses the current entity ID.
- Test fast and slow compatible robots with an otherwise identical approved
  fingerprint: prompting and post-approval dispatch each use the tentative or
  newly selected robot's own exact operation/pass duration and rerun all gates.
- Test notify entity resolution, legacy compatibility, rename/reload behavior,
  missing registry references, and redacted diagnostics.
- Test 30-minute approval, next-window skip, 24-hour reprompt boundaries, and
  request pruning.
- Test both ordered cleaning programs: a second stage within the valid approval
  needs no duplicate prompt; occupancy blocks it; continuation after approval
  expiry requires a new remaining-stage confirmation; completed stages never
  replay; water becoming ready during vacuum permits the approved mop stage;
  and a skipped no-water mop does not invalidate an approved vacuum.
- Test no-sensor bedroom mopping with two independent requests: assigned-user
  bedroom approval precedes the all-user water request; confirm/cancel actions
  cannot cross-authorize; and expiry of either authorization blocks mopping
  while preserving any safe configured vacuum stage.
- Test all three plan 5 manual actions for an enabled bedroom: the button press
  alone never dispatches, the assigned-phone approval fingerprint includes the
  manual mode and resolved stages/profile, skip/timeout leaves cadence unchanged,
  and an approved request retains only its documented cadence/window bypass.
- Test that changing either vacuum or mop pass count invalidates the applicable
  approval fingerprint, while unrelated live water-state changes do not.
- Test that assignment/delivery failures are room-local but a real post-approval
  clean-start failure engages the existing global halt/Repair.
- Test a held/offline bedroom stage recovering to complete or cancelled:
  completed stages are not replayed, cancelled occurrences lose their approval,
  and no recovery path sends a new prompt directly.
- Test unload while a delivery is queued, while an action handler owns the
  coordinator lock, and while an expiry callback is pending. No notification,
  state mutation, evaluation, profile call, or clean may occur after closing
  begins. Test entry removal clears assignment/delivery Repairs and Store data
  without `runtime_data`.
- Test room-card target ownership, native selector dialog, translated Repair
  placeholders, strict current-schema rejection/storage safe mode,
  dashboard-copy equality, and that non-bedrooms/manual cleans never enter this
  path.
- Run the complete repository tests and compile every integration Python
  module.

## Acceptance criteria

- Every scheduler-owned bedroom occurrence has a matching unexpired approval
  sent to the bedroom's currently resolved assigned phone, and no separately
  dispatched stage starts after that authorization expires without a fresh
  remaining-stage confirmation.
- Plan 5 manual bedroom actions follow the same assigned-recipient rule; a
  dashboard press is a request, not authorization.
- An unassigned, stale, or undeliverable bedroom fails closed with actionable
  room-card and Repairs guidance while unrelated rooms remain schedulable.
- Approval cannot bypass scheduler halt, windows, local/adjacent/transit
  occupancy, robot readiness, water, profile, operation-specific passes, or
  final area-mapping preflight.
- Restart neither duplicates nor forgets a request, and no active action token
  appears in logs, public state, Repairs, or diagnostics.
- Vacuum renames do not invalidate requests, while a replacement robot must
  reproduce the approved behavior and independently fit the current safe
  window.
- Storage corruption and config-entry shutdown fail closed without overwriting
  saved state, sending a late notification, or dispatching a clean.
- Skip, timeout, mismatch, and delivery failure never advance clean history.
