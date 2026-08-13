# Plan: Per-room fan speed and adapter-applied cleaning profiles

## Goal

Allow each room to request a fan/suction speed and the other supported
room-cleaning behaviors. Resolve one complete profile for the selected robot
and apply it before dispatch so settings from one room cannot leak into the
next job.

## Product decision

**Power level** means the vacuum's advertised fan speed. Vacuum cards own robot
defaults. Room cards own optional exact-value overrides for fan speed, cleaning
mode, mop mode, mop intensity, and the already implemented pass-count setting.
Maintenance or administrative controls such as child lock, do-not-disturb, or
dust-bin actions are not room profile fields.

## v1.3.2 baseline and gap

The adapter capability snapshot already publishes per-robot raw fan-speed,
cleaning-mode, mop-mode, mop-intensity, operation, and pass options. Vacuum
cards already expose robot-owned defaults, and room cards already expose
**Robot default / 1 pass / 2 passes**. Runtime profile application currently
reads only robot settings and directly calls common Home Assistant services;
there are no room overrides, no whole-profile compatibility filter, and no
guarantee that a **Robot default** room resets a value changed for the preceding
room.

Both inspected robots advertise fan speed but with different option sets.
Their other options also differ. Every option therefore remains an exact raw
Home Assistant value associated with the robots that advertise it; there is no
hard-coded common list or semantic translation.

## Adapter profile contract

This plan consumes the adapter schema evolution from the water-aware mopping
plan. If implemented independently, it must introduce the same versioned
extension rather than adding vendor branches to runtime orchestration.

- Keep normalized option sets in `AdapterCapabilities` and add typed
  `RequestedCleaningProfile` and `ResolvedCleaningProfile` values.
- Add adapter methods to validate a resolved profile without side effects and
  to apply it without starting a clean. The generic adapter implements standard
  Home Assistant fan-speed and discovered select actions. A vendor adapter may
  override or extend application while preserving the same normalized result.
- Keep dispatch separate: profile application performs no area clean or native
  segment command, and dispatch receives the resolved profile in its request.
- Adapters return stable result codes and never expose native targets or raw
  exceptions. Roborock continues to use native repeat-2 dispatch only when two
  passes are requested; shared runtime does not also toggle a pass select for a
  native request.
- Record the adapter ID/schema and requested/resolved profile in the active-job
  checkpoint. After restart, observed robot state remains authoritative and no
  profile or command is replayed automatically.

## Defaults and leakage prevention

- Preserve every existing v1.3 robot mode/mop/fan setting and entity/unique ID
  as that robot's default. Migrate new room override fields to null (**Robot
  default**) so upgrades do not alter behavior.
- An unset robot field is visibly **Not configured**; the UI must not display
  the first advertised option as though it were saved.
- Before a room may save or use an explicit override for a field, an eligible
  robot must advertise the value and have an explicit robot default for that
  field. This guarantees that a later **Robot default** job can restore a known
  value instead of inheriting the prior room's override.
- Resolve all applicable fields for every dispatch and reapply them in a stable
  order, including defaults. Do not apply only the fields that differ from the
  scheduler's last estimate.
- If eligible robots differ, assignment filters to robots supporting the entire
  resolved profile before battery/readiness ranking. Never partially apply,
  translate, or silently substitute an unsupported value.

## Mopping and pass interaction

- Room mop-mode/intensity overrides appear only when a floor has an adapter
  that supports scheduler mopping under the authoritative water contract.
- Mop work remains subject to live water readiness and carpet exclusion during
  evaluation and final pre-dispatch checks.
- Pass count remains the existing room-owned setting and participates in the
  same compatibility/profile fingerprint; do not create a second pass entity.
- A capability regression or saved value that no eligible robot can apply is a
  normal fail-closed configuration block. Show it on the room card and create a
  translated, deduplicated Repair that auto-clears when the profile is edited or
  compatibility returns. It does not engage the global dispatch halt because no
  clean was attempted.

## Failure behavior

Profile service calls are not atomic. Apply the full resolved profile before
the adapter dispatch and checkpoint the phase. If any call raises, times out, or
returns an error:

- do not send the clean command or advance cadence;
- log safe robot, room, adapter, profile-field, and requested-value context;
- retain no native target or raw exception in Store/entities/Repairs;
- engage the existing durable system-wide scheduler halt with
  `profile_apply_failed`; and
- use the scheduler-failure Repair/global recheck flow to validate current
  entities/options without issuing a test clean before explicit resume.

The next successful dispatch after resume reapplies the complete profile, so a
partially changed robot is not trusted.

## Dashboard experience

- Vacuum cards retain the existing robot defaults and add clear configured,
  current-observation, and compatibility status where needed.
- Each room card gains room-owned profile selects. Choices are **Robot default**
  plus the exact live union for eligible robots on that floor, with supporting
  robot names as presentation-only compatibility detail.
- A room card explains missing defaults, incompatible saved values, water
  gating, and which fields are inherited. It never repeats the room name on
  every row and never displays another room's controls.
- Dynamic capability/friendly-name changes update option membership and labels
  through the existing discovery signal. Both JavaScript copies remain
  byte-identical.

## Implementation plan

1. Add typed requested/resolved room profiles and pure default-resolution and
   whole-profile compatibility helpers in `models.py`.
2. Extend `RoomSettings` with nullable fan-speed, cleaning-mode, mop-mode, and
   mop-intensity overrides. Bump from the Store schema current at implementation
   time; preserve current robot defaults and existing room pass values.
3. Extend the versioned adapter contract with profile validation/application.
   Implement the standard Home Assistant path in the generic adapter and make
   Roborock delegate unless a vendor-specific override is required.
4. Refactor candidate/assignment construction to resolve a complete profile per
   robot, discard incompatible robots before ranking, and carry the chosen
   typed profile into preview and dispatch.
5. Refactor runtime profile application into the adapter boundary. Apply
   cleaning mode, fan speed, mop mode/intensity, and non-native pass controls in
   deterministic order; repeat the capability/water checks immediately before
   checkpoint and application.
6. Reuse the existing system fault latch for application errors. Add a separate
   auto-clearing compatibility Repair for saved profiles that cannot reach the
   application phase.
7. Add stable room-owned controls/status roles to the selected room card and
   improve existing vacuum-default status without replacing public entity IDs
   or unique IDs.
8. Store requested/resolved profiles and adapter version in previews and active
   checkpoints. Keep robot observations authoritative during recovery.
9. Document exact-value compatibility, required defaults, water gating, pass
   handling, and Repair/resume behavior.

## Validation

- Test standard generic fan-speed/select calls and vendor delegation without a
  clean command during profile application.
- Test consecutive rooms with different speeds followed by a **Robot default**
  room; every job receives an explicit resolved value.
- Test missing robot defaults, heterogeneous robots, exact raw values, and
  whole-profile compatibility filtering.
- Test that mop fields never make a robot without authoritative water telemetry
  mop-capable and two passes are unavailable without adapter support.
- Test compatibility Repairs auto-clear, while an actual profile-application
  failure aborts dispatch and durably halts all scheduler work until explicit
  successful recheck/resume.
- Test Store migration, active checkpoint/restart behavior, dynamic room/vacuum
  card membership, stable unique IDs, translation placeholders, and dashboard
  copy equality.
- Run the complete repository tests and compile every integration Python
  module.

## Acceptance criteria

- Two rooms can reliably request different fan speeds on the same robot, and a
  following inherited room restores the configured robot default.
- Room cards show only capability-compatible cleaning behaviors and derive raw
  options from live adapter snapshots.
- An incompatible profile blocks before dispatch and is actionable without
  creating a false global failure.
- Any failure while applying a selected profile prevents the clean and uses the
  existing durable system-wide halt/Repair workflow.
- Future vendors can validate and apply profiles through the adapter contract
  without scheduler changes.
