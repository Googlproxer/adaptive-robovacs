# Plan: Adapter-owned water-aware mopping

## Goal

Dispatch mop-only or vacuum-and-mop work only when the selected vendor adapter
reports authoritative mop/water support and its current normalized observation
is ready. Keep vacuum work independent so a blocked mop never prevents an
otherwise due vacuum clean.

## Product decision

A robot without a supported water sensor set is vacuum-only through this
scheduler. The robot-owned `mopping_enabled` switch cannot override missing,
unknown, unavailable, ambiguous, or unsafe water telemetry. Roborock is the
first vendor adapter to supply this contract. The generic adapter and future
vendors without an equivalent implementation advertise only vacuum operation,
even if Home Assistant exposes mop-related selects.

## v1.3.2 baseline and gap

The v1.3 adapter snapshot already carries `supported_operations`, mop-control
option lists, and a placeholder `water_readiness` value. Discovery still infers
generic mop support from the presence of mode controls, `_mop_ready` is a
boolean robot setting check, and adapters receive no typed same-device sensor
evidence or watched observation sources. Profile application also occurs at the
shared runtime boundary rather than through an adapter profile contract.

This plan evolves that foundation; it does not add Roborock checks to
`coordinator.py`.

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
one unambiguous same-device match for every key is vacuum-only for scheduler
decisions.

## Adapter schema evolution

- Extend the adapter match/discovery context with the vacuum device registry ID
  and a typed, transient tuple of same-device entity evidence: registry entity
  ID, domain, platform, entity-description/translation key, device class, and
  current state. Local unique IDs may be inspected only when Home Assistant
  exposes no safer stable key; they must never leave this transient context.
- Return an adapter snapshot containing normalized capabilities plus transient
  watched entity IDs. Watched IDs are used only to refresh discovery/evaluation
  and are never stored, logged, checkpointed, exposed in Repairs, or projected
  to dashboard entities.
- Replace the free-form water string with a typed readiness result containing
  `supported`, `ready`, a stable reason code, and normalized booleans/unknowns.
  Scheduler code consumes only this vendor-neutral result.
- Add an adapter operation-readiness method used during evaluation and again
  immediately before a dispatch checkpoint. Adapter `async_preflight` repeats
  the check for mop work and distinguishes a normal `blocked` result from an
  adapter/configuration `error`.
- Advance the generic and Roborock adapter schema versions when the contract is
  implemented. Existing v1 active-job checkpoints remain observational only;
  they are never replayed as new commands after upgrade.

## Proposed behavior

- The Roborock adapter recognizes the complete sensor trio on the vacuum's
  current Home Assistant device and adds `mop`/`vac_and_mop` only when the
  required controls and authoritative telemetry are present.
- Attached mop, attached water box, and no shortage means ready. A missing
  member, duplicate match, `unknown`, or `unavailable` is never ready.
- A robot that has never exposed an authoritative set is normally unsupported,
  not a system failure. A malformed/ambiguous adapter probe or loss of a
  previously configured mop capability produces a safe vacuum-card diagnostic
  and a translated, deduplicated Repair only when user action is required.
- Water shortage, removal, or an unavailable observation before checkpoint is
  a normal fail-closed scheduling wait. It does not create a system-wide
  dispatch halt because no clean was attempted.
- If readiness becomes blocked during the final adapter preflight but before a
  cleaning command, clear the provisional checkpoint, leave mop cadence due,
  and return to a normal wait. Exceptions, profile-application failures,
  dispatch failures, and start-confirmation failures retain the v1.3
  system-wide halt and Repair behavior.
- If mop work is blocked while vacuum work is due, dispatch vacuum-only and do
  not advance mop cadence.
- Water loss after cleaning starts is observed and logged. The scheduler does
  not stop a running robot solely because a sensor changed; observed robot
  state remains authoritative.

## Dashboard and entity experience

- The selected vacuum card shows scheduler-mopping support, current water
  readiness, safe reason text, and the mopping-enable/profile controls only
  when the adapter contract supports them.
- The selected room card shows mop due/readiness and why mop work is waiting.
  Carpet remains an independent stronger exclusion.
- Capability changes use the existing dynamic discovery signal. If a control
  already exists and support disappears temporarily, it becomes unavailable
  with a diagnostic rather than remaining actionable or being silently
  repurposed.
- The two dashboard JavaScript copies remain byte-identical; no local water
  entity ID or native adapter target is exposed.

## Implementation plan

1. Add the typed adapter discovery evidence, snapshot, watched-source, and
   operation-readiness contracts. Keep the generic adapter as the fallback and
   vacuum-only for scheduler operation support.
2. Implement Roborock discovery of the three stable same-device sensor keys.
   Reject missing, duplicate, cross-device, or ambiguous matches safely.
3. Extend normalized `AdapterCapabilities`/readiness and update the adapter
   registry's probe-failure fallback without leaking vendor evidence.
4. Add watched adapter observation entities to discovery change signatures and
   the coordinator watch set so state changes refresh previews and trigger an
   ordinary safe evaluation.
5. Add pure mop eligibility decisions in `models.py` combining requested
   operation, normalized adapter support/readiness, carpet exclusion, and room
   cadence. Cover all true/false/unknown/unavailable combinations.
6. Replace `_mop_ready` callers with structured results in candidate building,
   assignment, schedule preview, duration selection, and projections.
7. Reorder the final runtime path so operation readiness is rechecked before
   the durable dispatch checkpoint. Preserve a typed `blocked` versus `error`
   distinction through adapter preflight and the existing fault latch.
8. Preserve mop history while blocked. A manual or scheduler vacuum-only
   completion never advances mop history.
9. Add vacuum-card and room-card status/control entities with stable ownership
   roles. Use the corrected `issues.<key>.fix_flow` translation contract for
   any actionable capability Repair.
10. Document the fail-closed adapter boundary and the requirements for a future
    vendor implementation.

## Validation

- Test discovery of one complete same-device Roborock trio and rejection of
  missing, duplicated, cross-device, ambiguous, unknown, and unavailable
  evidence.
- Test that the generic adapter and a Roborock with no water telemetry remain
  vacuum-only even when mode selects offer mop values.
- Test shortage, removed mop, removed water box, and state recovery updates.
- Test that a simultaneously due vacuum dispatches vacuum-only while mop work
  remains due.
- Test that a final readiness race is a non-dispatching wait, while an adapter,
  profile, command, or start-confirmation failure still engages the existing
  global halt and Repair.
- Test carpet exclusion, Store/restart behavior, dynamic entity membership,
  Repair translation placeholders, and dashboard-copy equality.
- Run the complete repository tests and compile every integration Python
  module.

## Acceptance criteria

- A new mop job starts only with adapter-confirmed support, attached mop and
  water box, and no water-shortage problem at the final pre-dispatch check.
- A robot without authoritative telemetry is vacuum-only and has no actionable
  scheduler mop controls.
- Water state changes update eligibility without altering cadence or creating
  a false system failure.
- Vacuum-only work remains schedulable when mopping is unsupported or blocked.
- Future vendors can implement the same normalized observation/readiness
  contract without changing scheduler orchestration.
