# Plan: Roborock water-aware mopping

## Goal

Dispatch mop-only or vacuum-and-mop work only when the selected robot exposes
the required authoritative mop/water signals and they currently indicate that
mopping can start. Keep vacuum work independent so a blocked mop does not
prevent an otherwise due vacuum clean.

## Product decision

A robot without a supported water sensor set does not support mopping through
this scheduler. The existing `mopping_enabled` switch cannot override missing,
unknown, unavailable, or unsafe water telemetry. Roborock is the first vendor
adapter; additional vendors require their own documented capability adapter.

## Live discovery finding

The inspected Home Assistant instance contains one Roborock device with three
same-device binary signals and another robot without them. The portable
Roborock entity-description keys are defined by Home Assistant's
[Roborock binary-sensor platform](https://github.com/home-assistant/core/blob/dev/homeassistant/components/roborock/binary_sensor.py):

| Entity-description key | Required safe observation |
| --- | --- |
| `water_box_carriage_status` | Mop attached is on |
| `water_box_status` | Water box attached is on |
| `water_shortage` | Water shortage/problem is off |

These keys are integration metadata, not deployment entity IDs. A robot with
no complete trio is vacuum-only for scheduler decisions.

## Proposed behavior

- Discover the three signals from the vacuum's same Home Assistant device by
  integration platform and stable entity-description/unique-ID metadata. Never
  match a local friendly name or hard-code an entity ID.
- Define `supports_scheduler_mopping` only when the robot also has applicable
  cleaning/mop controls and the complete Roborock water signal set.
- Represent readiness as a structured result containing support, ready state,
  reason code, and normalized observations. Do not expose raw vendor errors.
- Treat a missing member, ambiguity, `unknown`, or `unavailable` as not ready.
  Recheck all signals immediately before profile application and dispatch.
- If mop work is blocked, keep it overdue. If vacuum work is also due, allow a
  vacuum-only job and do not advance mop cadence.
- Water loss after cleaning starts is observed and logged; the scheduler does
  not stop a running vacuum solely from an estimate. The robot remains
  authoritative.

## Implementation plan

1. Add a Roborock capability adapter that groups entity-registry entries by the
   vacuum's device and recognizes the three stable description keys. Reject
   missing or duplicate matches with a safe diagnostic.
2. Extend `RobotProfile` with normalized mop-attachment, water-box, and
   shortage observations. Add those entities to the watched state set so a
   change refreshes previews and schedules a normal evaluation.
3. Add a pure `mop_readiness` decision in `models.py` combining adapter support,
   complete live observations, robot enablement, and carpet exclusion. Cover
   every on/off/unknown/unavailable/missing/ambiguous combination.
4. Replace boolean readiness callers with the structured result. Candidate
   creation and robot assignment must distinguish unsupported hardware from a
   temporarily unsafe water state.
5. Recheck readiness in `runtime.py` immediately before changing cleaning or
   mop controls. A failed check clears provisional dispatch state, leaves mop
   cadence due, and never marks the area unmapped.
6. Project `supports_scheduler_mopping`, `mop_ready`, and safe reason codes to
   robot and room status. Do not expose local water entity IDs unless an
   administrator-only diagnostic is deliberately added later.
7. Preserve stored mop history when blocked. Manual or scheduler vacuum-only
   completion must not advance mop history.
8. Integrate mop mode and intensity into the shared per-room cleaning-profile
   dashboard. Hide or disable those controls for robots that fail the complete
   capability test.
9. Document the fail-closed policy and the adapter boundary. A future vendor
   must supply equivalent authoritative signals and tests rather than relying
   on a generic user label as a manual assertion.

## Validation

- Test discovery of one complete same-device Roborock trio and rejection of
  missing, duplicated, cross-device, or unavailable signals.
- Test that a robot with no water telemetry is never selected for mop-only or
  vacuum-and-mop work even when its cleaning-mode select offers mop values.
- Test shortage, removed mop, removed water box, and stale-state transitions.
- Test that a simultaneously due vacuum dispatches vacuum-only while mop work
  remains due.
- Test carpet as an independent stronger exclusion and the pre-dispatch recheck.
- Test restart behavior against current Home Assistant states, not persisted
  readiness estimates.
- Run the repository unit tests and compile every integration Python module.

## Acceptance criteria

- A new mop job starts only with a complete supported sensor set, attached mop
  and water box, and no water-shortage problem.
- A robot without the required telemetry is treated as vacuum-only and has no
  dashboard mop profile controls.
- Water state changes update eligibility without altering cadence.
- Vacuum-only work remains schedulable when mopping is unsupported or blocked.

