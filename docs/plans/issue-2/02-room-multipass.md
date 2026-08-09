# Plan: Room-specific native two-pass cleaning

## Goal

Let a room request the robot's native two-pass cross-hatching feature. Start
with Roborock, retain registry-driven Home Assistant area cleaning, and never
fall back to hard-coded native map segments.

## Product decision

Multipass means one normal pass versus exactly two native cross-hatched passes.
Arbitrary pass counts are out of scope. Roborock is the first implementation
target because it is the available test hardware. Other vendors may be added
only through capability-advertising adapters after their native behavior is
understood and tested.

## Current behavior and upstream gap

The scheduler has a robot-wide `double_pass` setting and code that can discover
a pass-count select, but the live Roborock devices expose no such entity. More
importantly, Home Assistant's current `vacuum.clean_area` schema accepts only
`cleaning_area_id`, and the Roborock `async_clean_segments` implementation
sends segments without a repeat value.

Roborock supports a native segment-clean repeat parameter; existing Home
Assistant evidence shows that `repeat: 2` activates the native repeated/cross
pattern. The portable area-cleaning contract does not currently carry that
request. Relevant upstream references are the current
[vacuum platform](https://github.com/home-assistant/core/blob/dev/homeassistant/components/vacuum/__init__.py),
[Roborock vacuum implementation](https://github.com/home-assistant/core/blob/dev/homeassistant/components/roborock/vacuum.py),
and [Roborock repeat report](https://github.com/home-assistant/core/issues/115476).

## Required design

- Do not use `vacuum.send_command`, `roborock.vacuum_clean_zone`, or stored map
  segment IDs. Those paths break the repository's area-registry contract and
  make map changes unsafe.
- Add the scheduler room control only when the installed Home Assistant stack
  advertises native support for one- and two-pass area cleaning.
- Preserve the existing robot-wide double-pass entity and unique ID. Once the
  native capability exists, it remains the default for rooms that choose
  **Robot default**.
- An explicit two-pass room may be assigned only to a robot that advertises
  native two-pass area support. Never emulate it with two scheduler dispatches
  or silently downgrade to one pass.
- Store the resolved pass count in the active-job checkpoint and keep duration
  learning separated by pass count.

## Implementation plan

### Phase A: Home Assistant vacuum contract

1. Open the required Home Assistant architecture discussion and propose an
   optional pass count for area cleaning, plus a discoverable capability or
   supported-count property. The field must remain optional and backward
   compatible for integrations that only support one pass.
2. Update the core vacuum service schema and entity method boundary so an area
   clean can carry `cleaning_passes=2` without losing area-to-segment mapping.
3. Add core tests for omitted/default, one-pass, two-pass, invalid, and
   unsupported values. Final naming follows the accepted architecture design.

### Phase B: Roborock upstream support

4. Update Home Assistant's Roborock vacuum platform to advertise one and two
   passes for compatible models and translate two passes to the native segment
   `repeat: 2` request.
5. Add mocked Roborock tests proving that one pass omits or resets repeat as the
   library requires and two passes sends the native value. Unsupported models
   must not advertise the feature.
6. Verify on the available Roborock hardware that the standard area action
   produces native cross-hatching and that a later one-pass job is not left in
   two-pass mode.

### Phase C: Scheduler support

7. Add nullable `pass_count` to `RoomSettings`, with `None` meaning **Robot
   default** and explicit values limited to 1 or 2. Migrate existing rooms to
   `None`; retain existing active-job and duration data.
8. Add pure decisions for default resolution and robot compatibility. Filter
   robots before assignment and calculate vacancy duration using the resolved
   count.
9. Dispatch through the enhanced `vacuum.clean_area` action with the resolved
   count. Recheck capability immediately before dispatch; on failure, clear the
   provisional checkpoint, log safe context, and leave the room due.
10. Add a room-owned select only where an enabled floor robot advertises native
    two-pass support. Choices are **Robot default**, **1 pass**, and **2-pass
    cross-hatch**. Show requested and resolved values in previews and active
    job diagnostics.
11. Update docs and both dashboard copies. Document that other vacuum vendors
    remain one-pass until an adapter advertises equivalent native behavior.

## Validation

- Upstream tests prove the standard area action carries a supported pass count
  and Roborock translates two passes to its native repeat command.
- Scheduler tests cover inherited, explicit one-pass, explicit two-pass,
  unsupported, and heterogeneous-robot assignment.
- Test one-pass immediately after two-pass and restart recovery during an
  active two-pass job.
- Test that no compatible robot leaves the room due with a clear diagnostic and
  does not alter cadence or mark the room unmapped.
- Run the repository unit tests and compile every integration Python module.

## Acceptance criteria

- A two-pass room uses Roborock's native cross-hatched repeat behavior through
  Home Assistant's area-cleaning API.
- No deployment-specific map or segment identifiers are persisted or sent by
  the scheduler.
- Unsupported robots cannot be selected for a two-pass room and never receive
  an emulated or downgraded clean.
- Preview, active state, recovery, and duration learning agree on the resolved
  pass count.

