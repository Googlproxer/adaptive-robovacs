# Plan: Per-room fan speed and cleaning profiles

## Goal

Allow each room to request a fan/suction speed and expose the other supported
robot-specific cleaning behaviors in one capability-driven dashboard profile.
Apply the complete resolved profile before dispatch so settings from one room
cannot leak into the next job.

## Product decision

**Power level** means Home Assistant's native vacuum fan speed. The room
dashboard should also expose the robot-specific behaviors that directly affect
that room's clean: cleaning mode, mop mode, mop intensity, and native
two-pass cross-hatching when supported. Maintenance or administrative controls
such as child lock, do-not-disturb, or dust-bin actions are not part of the room
profile.

## Live discovery finding

Both inspected robots advertise Home Assistant's standard fan-speed feature,
but their option sets differ. Their cleaning-mode options also differ, and only
capable hardware exposes usable mop-mode and mop-intensity controls. The UI and
assignment logic therefore must use each robot's advertised raw options rather
than a hard-coded common list.

Home Assistant's portable interface is `fan_speed_list` plus
`vacuum.set_fan_speed`; see the
[vacuum entity contract](https://developers.home-assistant.io/docs/core/entity/vacuum/).

## Proposed behavior

- Add a room cleaning profile with **Robot default** or an exact advertised
  value for each supported behavior.
- Use `vacuum.set_fan_speed` for fan speed. Do not add a vendor-specific select
  fallback in the first release while the standard feature is available.
- Keep existing robot mode/mop entities and stable unique IDs as robot defaults.
  Add a robot fan-speed default. A room override resolves against the selected
  robot before assignment.
- Build dashboard choices from live capabilities. If multiple eligible robots
  advertise different values, show their union with compatibility detail and
  restrict assignment to a robot supporting every explicit selection.
- Never translate raw option names, silently substitute a value, or expose a
  control on unsupported hardware.
- Mopping controls remain subject to the complete water-capability and current
  readiness rules. Two-pass is visible only after native support is advertised.

## Migration and default safety

Existing rooms and the new robot fan-speed default migrate to unset, preserving
current behavior. The dashboard must require a robot fan-speed default before
allowing any room on that robot to save an explicit fan-speed override. Once
enabled, every dispatch resolves and reapplies a speed, so a later **Robot
default** room cannot inherit the previous room's override accidentally.

## Implementation plan

1. Extend `RobotProfile` with the native fan-speed target, advertised raw
   options, current observation, and the already discovered cleaning/mop
   controls. Normalize only transport shape, not user-facing values.
2. Add a typed `RoomCleaningProfile` containing nullable `fan_speed`,
   `cleaning_mode`, `mop_mode`, `mop_intensity`, and `pass_count`. Add a
   nullable robot `fan_speed_default`. Version and test the Store migration.
3. Add pure helpers to resolve defaults and test whole-profile compatibility
   against a robot. Filter incompatible robots before battery ranking and
   publish a specific generic block reason if none can apply the profile.
4. Add a robot-owned fan-speed-default select. Add room controls whose choices
   are **Robot default** plus the live union for eligible floor robots. Display
   which robot supports a heterogeneous option without persisting that display
   metadata.
5. Create one runtime profile transaction that applies cleaning mode, fan
   speed, mop mode/intensity, and pass count in a deterministic order before
   `vacuum.clean_area`. Recheck capability and water readiness immediately
   before application.
6. If any profile call fails, do not clean. Log robot, room, control kind, and
   requested option with safe exception context; clear provisional active
   state, expose a generic profile error, and leave the room eligible later.
7. Store requested and resolved profile values in preview and the durable
   active-job checkpoint. Robot observations after restart remain authoritative
   and the profile is reapplied only as part of a fresh safe dispatch.
8. Refactor the dashboard room panel into a capability-driven **Cleaning
   profile** section. Hide unsupported rows, explain incompatible saved values,
   and keep both JavaScript copies byte identical.
9. Document raw-value compatibility, defaults, water gating, and the distinction
   between cleaning mode and fan speed.

## Validation

- Test standard fan-speed discovery and exact service calls before dispatch.
- Test consecutive rooms with different speeds and a following **Robot
  default** room; each receives the resolved value.
- Test heterogeneous robots and whole-profile compatibility filtering.
- Test that mop fields cannot make a robot without complete water telemetry
  mop-capable and that two-pass remains absent without native support.
- Test profile failure aborts area cleaning without changing cadence or map
  status and remains retryable.
- Test Store migration, dynamic dashboard options, stable unique IDs, and
  dashboard-copy equality.
- Run the repository unit tests and compile every integration Python module.

## Acceptance criteria

- Two rooms can reliably request different fan speeds on the same robot.
- The dashboard shows only the cleaning behaviors supported by eligible robots
  and derives every raw option from Home Assistant at runtime.
- **Robot default** restores an explicit saved default and never leaks a prior
  room's profile.
- An incompatible profile blocks safely rather than being translated or
  partially applied.
- Profile failures prevent dispatch and do not permanently exclude the room.

