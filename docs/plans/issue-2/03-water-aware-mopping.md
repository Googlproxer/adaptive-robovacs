# Plan: Water-aware ordered cleaning programs

## Implementation status

Implemented in Adaptive RoboVacs v1.4.0 and corrected for late-loading vendor
capabilities in v1.4.1. The release line includes the Store schema
v5 migration, ordered per-room cleaning occurrences, robot defaults and room
overrides for cleaning programs, independent vacuum/mop pass controls,
authoritative Roborock water readiness, the one-hour explicit confirmation
fallback, all-target notifications, and translated Repairs diagnostics.

## Goal

Replace independently scheduled vacuum and mop work with one room-cleaning
occurrence whose configured program may contain a vacuum stage, a mop stage,
or both in a chosen order. Dispatch a mop stage only when the selected vendor
adapter reports mop support and water readiness is established either by
authoritative telemetry or a fresh explicit user confirmation. Allow any vacuum
stage to proceed when water is unavailable or unobservable.

## Confirmed product model

- Each robot owns an explicit default cleaning program: **Vacuum only**,
  **Mop only**, **Vacuum then mop**, or **Mop then vacuum**.
- Each room may inherit the selected robot's program or choose one of those
  programs as an override. The cleaning-profile plan owns these controls and
  compatibility rules.
- A room has one cleaning cadence. The configured program defines everything
  attempted for that scheduled occurrence; there are no separate vacuum and
  mop cadences.
- An ordered two-operation program is two distinct physical starts. An adapter
  may use native commands for either stage, but it must not collapse the two
  stages into a simultaneous vacuum-and-mop command because that changes the
  requested order.
- Every later stage repeats all normal scheduler safety checks. If the room or
  an adjacent/transit room becomes occupied, persist the remaining stage and
  wait for a newly valid safe window rather than continuing under the first
  stage's eligibility.
- Water unavailability skips the mop stage for that occurrence. It never
  blocks a configured vacuum stage, never engages the system failure latch,
  and does not keep the occurrence due merely to retry mopping. Mopping is
  considered again at the room's next scheduled occurrence.
- Water readiness is decided only when the mop stage is actually eligible to
  start. For **Vacuum then mop**, unavailable water at the beginning does not
  pre-skip the later mop: if water becomes ready while vacuuming, the fresh
  mop-stage preflight allows mopping to run.
- A robot with a verified mop function but no authoritative water telemetry is
  conditionally mop-capable. When its mop stage becomes current, notify all
  resolvable users and require one explicit **Confirm water** action within one
  hour. **Cancel mopping**, a reported dismissal, timeout, delivery ambiguity,
  or any other absence of explicit confirmation cancels that mop stage.
- A failure while applying a stage profile, sending a stage clean command, or
  confirming that a stage started is a system failure. Persist the incomplete
  occurrence, cancel its remaining automatic work, and start no more cleans on
  any robot until the existing Repair/recheck/resume flow succeeds.

## v1.3.2 baseline and gap

The v1.3 adapter snapshot already carries `supported_operations`, mop-control
option lists, and a placeholder `water_readiness` value. Discovery still
infers generic mop support from the presence of mode controls, `_mop_ready` is
a boolean robot setting check, and adapters receive no typed same-device sensor
evidence or watched observation sources.

Rooms currently store `vacuum_interval` and `mop_interval`, choose one
`vacuum`, `mop`, or `vac_and_mop` operation from independently due histories,
and checkpoint only one active operation. There is no persisted multi-stage
occurrence, water-skip outcome, or all-user notification resolver. This plan
evolves those foundations without adding Roborock checks to `coordinator.py`.

## Roborock observation contract

The inspected Home Assistant instance contains one Roborock device with three
same-device binary signals and another robot without them. The portable
Roborock entity-description keys are defined by Home Assistant's
[Roborock binary-sensor platform](https://github.com/home-assistant/core/blob/dev/homeassistant/components/roborock/binary_sensor.py):

| Entity-description key | Required safe observation |
| --- | --- |
| `water_box_carriage_status` | Mop attached is on |
| `water_box_status` | Water box attached is on |
| `water_shortage` | Water shortage/problem is off |

These keys are adapter metadata, not deployment entity IDs. A Roborock without
one unambiguous same-device match for every key may use the user-attested path
only if its adapter independently verifies a supported mop operation. A robot
with neither authoritative telemetry nor a verified mop operation remains
vacuum-only.

## Adapter schema evolution

- Extend the adapter match/discovery context with the vacuum device registry ID
  and a typed, transient tuple of same-device entity evidence: registry entity
  ID, domain, platform, entity-description/translation key, device class, and
  current state. Local unique IDs may be inspected only when Home Assistant
  exposes no safer stable key; they must never leave this transient context.
- Return an adapter snapshot containing normalized capabilities plus transient
  watched entity IDs. Watched IDs refresh discovery/evaluation and are never
  stored, logged, checkpointed, exposed in Repairs, or projected to dashboard
  entities.
- Replace the free-form water string with a typed readiness result that
  distinguishes `sensor_ready`, `sensor_blocked`, `confirmation_required`,
  `confirmed`, and `unsupported`, with stable reason codes and normalized
  booleans/unknowns. Scheduler code consumes only this vendor-neutral result.
- Add an adapter operation-readiness method used during evaluation and again
  before every mop-stage dispatch checkpoint. Adapter `async_preflight`
  repeats the check and distinguishes a normal `blocked` result from an
  adapter/configuration `error`.
- Advance the generic and Roborock adapter schema versions when the contract is
  implemented. Existing v1 active-job checkpoints remain observational only;
  they are never replayed as new commands after upgrade.

## Single cadence and migration

- Replace the room's two interval settings with one `cleaning_interval` and one
  next-occurrence calculation. Seed it from the existing vacuum cadence because
  that was the primary room-cleaning schedule; retire the mop-cadence entity
  and document the dashboard/automation migration.
- Seed the unified last-occurrence anchor from the most recent successful
  scheduler-owned vacuum or mop completion so upgrading does not immediately
  duplicate a recent clean. Retain historical vacuum/mop timestamps and
  operation-specific duration samples where they remain useful diagnostics;
  they no longer drive separate due decisions.
- Replace the robot `mopping_enabled` behavior with an explicit cleaning
  program. A disabled legacy setting migrates to **Vacuum only**. An enabled
  setting migrates to **Vacuum then mop** when the adapter verifies the mop
  operation. Complete telemetry uses automatic readiness; missing telemetry
  uses the confirmation-required path. It reconciles to **Vacuum only** with an
  actionable compatibility diagnostic only when no mop operation is verified.
- New room program overrides migrate to **Robot default**. Existing room pass
  values become vacuum-pass overrides, while new mop-pass overrides inherit the
  new one-pass robot default, as defined jointly with plan 5.
- A scheduled occurrence becomes cadence-complete only after every configured
  stage has reached a terminal outcome: successfully completed or deliberately
  skipped for sensed no-water, canceled/expired confirmation, or unconfirmed
  water. An ordinary safety wait, incompatible saved program, or system failure
  does not advance the occurrence.

## Durable occurrence lifecycle

Persist one bounded occurrence record containing the area, chosen robot,
resolved program/profile fingerprint, ordered stages, current stage, terminal
stage outcomes and per-stage resolved pass counts, schedule time, adapter
ID/schema, and safe timestamps. It contains no native area/segment targets or
transient water entity IDs.

1. Resolve a compatible robot and immutable ordered stages for the due room.
2. Before the first stage, evaluate all global, time-window, occupancy,
   adjacency, bedroom-transit, battery, capability, approval, profile, and
   mapping gates.
3. Run normalized water readiness only when a mop stage reaches the front of
   the sequence and every preceding stage is terminal. For **Mop only** or
   **Mop then vacuum**, that is the initial evaluation. For **Vacuum then mop**,
   defer the decision until vacuuming completes and the later stage passes a
   fresh normal evaluation. Water that becomes ready during vacuuming therefore
   permits the mop stage. For a telemetry-backed robot, water still unavailable
   at final mop preflight marks only that stage skipped. For a robot without
   telemetry, create or validate its explicit one-hour user confirmation before
   allowing final preflight.
4. Apply and dispatch only the current stage. Observe completion before making
   the next stage eligible. The same robot remains assigned to the occurrence,
   but is not reserved while the sequence waits.
5. Re-enter normal scheduling before every remaining stage. A new safe window
   means a later evaluation that independently satisfies the room's effective
   daily window and every current safety gate; it may be later in the same
   daily interval if a genuinely new safe vacancy is available.
6. Never replay a completed stage after restart. Resume only a safely known
   pending stage, with robot observation authoritative over the checkpoint.
7. If any attempted stage fails to start, leave that stage and the occurrence
   incomplete under the durable global halt. After explicit successful resume,
   re-evaluate that pending stage; do not repeat completed stages.

For **Mop only** with unavailable water, no clean command is sent. The mop stage
is marked `skipped_no_water`, the occurrence advances to its next scheduled
time, and the same notification policy applies.

For an unobservable-water robot, **Mop only** or **Mop then vacuum** creates the
confirmation request when the occurrence is otherwise eligible. A canceled or
unconfirmed mop is terminal for that occurrence; a following vacuum stage
remains eligible. **Vacuum then mop** creates no water prompt until vacuuming is
complete and mop is the current stage.

## Water-state behavior

- The Roborock adapter determines mop-operation support from the required
  controls/native capability independently from water observation. A complete
  same-device sensor trio enables authoritative readiness; verified mopping
  without the trio enables only the confirmation-required path.
- Attached mop, attached water box, and no shortage means ready. A missing
  member, duplicate match, `unknown`, or `unavailable` is never ready.
- A robot that has never exposed an authoritative water set but has an
  adapter-verified mop operation is `confirmation_required`, not unsupported.
  It exposes mop programs with a clear **Water confirmation required**
  diagnostic. A robot without a verified mop operation is vacuum-only. A saved
  mop program whose operation capability disappears creates an auto-clearing
  compatibility Repair and blocks that incompatible occurrence before any
  stage.
- Removal, shortage, `unknown`, or `unavailable` from an otherwise supported
  observation is a normal no-water episode. Skip only the mop stage, record a
  safe reason, and continue/complete the occurrence as defined above.
- Do not persist a no-water decision for a future stage. A preview may show its
  current readiness, but only the final preflight when that mop stage is next
  may create `skipped_no_water` or send the notification.
- Water loss after a mop starts is observed and logged. The scheduler does not
  stop a running robot solely because a sensor changed; observed robot state
  remains authoritative.
- Water state is not part of the program compatibility fingerprint. It is a
  transient stage precondition and cannot invalidate or block a vacuum stage.

## User-attested water confirmation

- When a current mop stage belongs to a robot with verified mopping but no
  authoritative water telemetry, persist one request before sending it to every
  resolvable user's active Companion-app targets. The copy uses current safe
  friendly names, for example: **Sheila wants to mop the kitchen, but cannot
  check whether water is onboard. Confirm water before mopping starts.** Never
  hard-code that example robot or room name.
- Provide exactly two request-bound actions: **Confirm water** and **Cancel
  mopping**. Use cryptographically unpredictable action identifiers,
  `authenticationRequired` where supported, a unique request tag, and a
  dedicated non-critical `Adaptive RoboVacs - Mop confirmation` Android
  channel. Companion actionable notifications return explicit action events as
  documented by the [Home Assistant actionable-notification contract](https://companion.home-assistant.io/docs/notifications/actionable-notifications/).
- Persist `sent_at` and `expires_at = sent_at + 1 hour`. An explicit valid
  **Confirm water** event must be recorded before `expires_at`, and the mop
  command must start before the same deadline. Confirmation schedules a fresh
  evaluation; it never dispatches directly, bypasses a safety check, or remains
  reusable for another room, occurrence, robot, or mop stage.
- The first valid terminal response wins atomically across all notified users
  and devices. A first explicit confirmation authorizes the stage and clears
  every copy. A first explicit cancel or correlated dismissal cancels the stage
  and clears every copy. Late, duplicate, forged, mismatched, or post-terminal
  events are ignored safely.
- Listen for Android's
  [`mobile_app_notification_cleared` event](https://companion.home-assistant.io/docs/notifications/notification-cleared/)
  and treat a correlated user dismissal as **Cancel mopping**. Because other
  platforms and some dismissal paths do not report a cleared event, the absence
  of a recorded explicit confirmation at expiry is always cancel. Merely
  opening, receiving, or viewing the notification never confirms water.
- Auto-dismiss at the deadline by setting Android `timeout: 3600` and sending
  `clear_notification` with the request tag to every target after atomically
  expiring the request. Send the same clear immediately after confirm/cancel.
  The [Companion clearing contract](https://companion.home-assistant.io/docs/notifications/notifications-basic/#clearing)
  notes platform delivery limitations; server-side expiry remains authoritative
  even if a client fails to remove its visible copy.
- Persist the request before delivery and do not resend it after restart.
  Restart restores the original deadline; an already expired or ambiguously
  delivered request becomes canceled. Record no push token, notify service
  name, raw action token, or local device/user identifier in public state,
  Repairs, or logs.
- Cancel, dismissal, timeout, and no explicit response mark the mop stage
  `skipped_unconfirmed_water`; they are normal safe outcomes, not system
  failures or Repairs. If the program has a vacuum stage, it may continue under
  fresh normal evaluation. Mopping is offered again only at the room's next
  scheduled occurrence.
- The actionable request itself is the user notification for an unconfirmed
  stage. Do not immediately send the separate 24-hour `skipped_no_water`
  broadcast when that request is canceled or expires.
- If no target resolves or every delivery fails, cancel immediately and create
  or update the local actionable notification-delivery Repair. Partial delivery
  leaves the request pending for successfully targeted devices. A user disabling
  the confirmation channel simply results in no confirmation and therefore a
  safe cancel.
- An explicit confirmation is a user attestation, not sensor evidence. All
  adapter/profile/map/occupancy checks still apply. Once an actual profile or
  clean command is attempted, its failure uses the global scheduler halt.

## All-user skipped-mop notifications

- When a supported robot skips a mop stage for unavailable water, notify every
  Home Assistant user who has at least one currently resolvable Companion-app
  notification target. Send to all active targets for those users, deduplicate
  endpoints, and resolve registry/config-entry identities at delivery time.
  Users without a registered notification target cannot be reached and are
  reported only as a safe aggregate count.
- Use a stable, non-critical Android notification channel such as
  `Adaptive RoboVacs - Mop skipped`. Android users can customize or disable the
  channel in system notification settings, as documented by the
  [Home Assistant Companion notification-channel contract](https://companion.home-assistant.io/docs/notifications/notifications-basic/#notification-channels).
  Do not set importance repeatedly or recreate a channel to defeat a user's
  choice.
- Android-style channels do not exist on iOS. iOS targets receive the same
  ordinary non-critical notification, but per-channel opt-out is unavailable;
  do not misrepresent an iOS category or group as an equivalent opt-out.
- Use a stable per-entry/per-room notification tag so a repeat replaces the
  previous visible alert instead of accumulating duplicates. The message names
  the safe room/robot display labels, explains that mopping was skipped, states
  whether vacuuming still ran, and says mopping will be tried at the next
  scheduled occurrence.
- Create one notification episode per room while the same no-water condition
  persists. Broadcast immediately on the first skipped occurrence, suppress
  repeats for 24 hours, and allow one reminder after each additional 24 hours
  if another scheduled occurrence is skipped. Close the episode when water is
  ready again, mopping is removed from the effective program, or the room is
  disabled.
- Persist only the room-scoped episode reason and timestamps needed for restart
  safety. Never persist push tokens or generated notify service names.
- A partial target-delivery failure is logged with aggregate safe context and
  does not block cleaning. If no configured recipient can be resolved or all
  deliveries fail, create/update one actionable notification-delivery Repair;
  this remains separate from the clean-start failure latch.

## Dashboard and Repairs experience

- The vacuum card shows adapter mopping support, current water readiness, safe
  reason text including **User confirmation required**, effective robot
  cleaning-program default, and compatible mop profile controls.
- The room card shows its inherited/overridden cleaning program, single
  cadence, separate effective vacuum/mop pass counts, occurrence/stage progress,
  pending water-confirmation deadline/status, last terminal outcome, water-skip
  reason, and next notification eligibility. Carpet remains an independent
  stronger exclusion for mop stages.
- Do not retain separate mop-due/vacuum-due controls or status rows. Historical
  stage timestamps may remain clearly labeled diagnostics.
- Capability changes use the existing dynamic discovery signal. Unsupported or
  malformed adapter capability produces a translated, deduplicated Repair only
  when user action is required. Ordinary empty-water skips and unconfirmed
  water cancellations produce mobile status/notifications, not Repairs or the
  global scheduler halt.
- Both dashboard JavaScript copies remain byte-identical; no local water entity
  ID, mobile target, user ID, or native adapter target is exposed.

## Implementation plan

1. Add typed adapter discovery evidence, watched sources, water readiness, and
   operation-preflight contracts. The generic fallback may advertise a verified
   standard Home Assistant mop operation, but without authoritative telemetry
   it must return `confirmation_required`; it never returns sensor-ready from
   mop-related controls alone.
2. Implement Roborock discovery of the three stable same-device sensor keys and
   reject missing, duplicate, cross-device, or ambiguous matches safely.
3. Add pure cleaning-program expansion, stage-specific pass resolution,
   unified due calculation, stage transition, just-in-time water-skip,
   confirmation eligibility/expiry/first-response decisions,
   cadence-completion, safe-window, and notification throttle decisions in
   `models.py`.
4. Replace dual room cadence and single-operation Store state with the unified
   interval and bounded occurrence, water-confirmation, and notification-episode
   records. Implement the explicit migration above and preserve historical
   diagnostics.
5. Add watched water entities to discovery signatures and the coordinator watch
   set so readiness changes refresh cards/previews and close notification
   episodes without exposing the entity IDs.
6. Refactor candidate, assignment, preview, duration, dispatch, completion, and
   recovery flows around one occurrence and its current stage. Recheck all
   safety gates before every stage and never replay a completed stage.
7. Put stage-specific profile application and dispatch behind the adapter
   boundary. Preserve typed `blocked` versus `error` results and the existing
   global failure latch for actual profile/command/start failures.
8. Add a registry-driven all-user Companion notification resolver; request-bound
   confirm/cancel and cleared-event handlers; one-hour timeout/clear behavior;
   skipped-mop channel/tag payload; 24-hour room-episode throttle; redacted
   delivery diagnostics; and a local delivery Repair for complete delivery
   failure.
9. Update vacuum/room entities and cards for cleaning programs, one cadence,
   occurrence status, water readiness, skipped-stage outcomes, and notification
   status while preserving unaffected public IDs and stable ownership roles.
10. Document the Store/entity migration, Android/iOS opt-out boundary, future
    vendor water contract, and system-failure versus water-skip behavior.

## Validation

- Test discovery of one complete same-device Roborock trio and rejection of
  missing, duplicated, cross-device, ambiguous, unknown, and unavailable
  evidence. A robot with verified mopping but no complete telemetry becomes
  confirmation-required; a robot with no verified mop operation remains
  vacuum-only.
- Test all four programs, robot defaults, room overrides, one cadence, program
  expansion, carpet exclusion, and capability compatibility.
- Test **Vacuum then mop** with water unavailable initially but restored during
  vacuuming: the later fresh preflight must dispatch mop. Also test water still
  unavailable after vacuum, water lost before mop, and water restored only
  after that occurrence; only an actually skipped mop waits until the next
  scheduled occurrence rather than becoming separately due.
- Test **Mop then vacuum** with unavailable water: mop is skipped at its
  just-in-time initial preflight and vacuum remains eligible.
- Test **Mop only** with no water sends no clean command but advances the
  occurrence with `skipped_no_water`.
- Test occupancy/adjacency/transit changes between stages, later same-window
  recovery, next-window recovery, restart, robot removal, and that completed
  stages never replay.
- Test profile, adapter command, and start-confirmation failures on either stage
  engage the durable global halt, cancel remaining automatic work, and preserve
  the pending-stage checkpoint for explicit resume.
- Test all-user target resolution, multiple devices, deduplication, users with
  no target, Android channel/tag payloads, iOS-safe payloads, first alert,
  24-hour reminders, recovery/reset, restart, partial delivery, total delivery
  failure, and redaction.
- Test no-sensor confirmation for every program: request persisted before send,
  exact copy/actions, valid confirm immediately before the one-hour boundary,
  confirm at/after expiry, explicit cancel, Android dismissal, unreported
  dismissal, body open, timeout, automatic clear, restart, and no duplicate
  prompt.
- Test concurrent recipient events atomically: first confirm permits one fresh
  evaluation; first cancel/dismiss prevents mop; late events cannot reverse the
  terminal outcome. No event other than explicit valid confirm may authorize.
- Test Store migration from dual cadence/history, public entity migration,
  independent vacuum/mop pass settings, dynamic card membership, Repair
  translation placeholders, and dashboard-copy equality.
- Run the complete repository tests and compile every integration Python
  module.

## Acceptance criteria

- Each due room produces one cleaning occurrence from its robot default and
  optional room override, with one cadence and an explicit ordered stage list.
- Every stage receives a fresh full safety evaluation; occupancy between stages
  persists the remainder until a new safe window without replaying completed
  work.
- A mop stage starts only with adapter-confirmed operation support plus either
  ready authoritative telemetry or an unexpired explicit user confirmation at
  its just-in-time final preflight. A preceding vacuum stage gives water time to
  become ready; initial unavailability cannot pre-cancel the mop.
- For an unobservable-water robot, cancel, dismissal, timeout, ambiguous action
  state, or failure to record an explicit confirmation cancels only that mop
  stage and cannot issue a mop command.
- No-water conditions skip mopping for that occurrence, allow vacuuming, notify
  all resolvable users under the throttled channel policy, and retry mopping
  only at the next scheduled occurrence.
- An attempted stage that cannot be started engages the existing durable
  system-wide halt and prevents every robot from receiving further scheduler
  cleans until explicit successful resume.
- Future vendors can implement the same normalized observation/readiness and
  stage dispatch contracts without changing scheduler orchestration.
