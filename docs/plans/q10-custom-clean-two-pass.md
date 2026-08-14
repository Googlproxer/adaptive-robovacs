# Plan: Q10 custom-clean native two-pass support

**Status:** Implemented and deployed in v1.6.0, with docked-only stale-profile
recovery in v1.6.1, Q10 **Cleaning Depth** controls in v1.6.2, and the
requested Daily-depth default migration in v1.6.3.

## Goal

Enable a compatible Roborock Q10/B01 vacuum to run a mapped room in one native
custom-clean operation with a requested `cleanCount` of two. This is a
protocol-specific extension to the existing Roborock adapter; it must not
reuse the legacy `app_segment_clean` command or emulate two passes with two
separate cleaning starts.

The external Q10 reference documents a per-room custom-clean profile with
`cleanCount` in the range 1--3 and a working room-clean start sequence. See
[the API reference](https://codeberg.org/zcodeberg/roborock-q10-utils-gpl/src/branch/main/Q10_API_REFERENCE.md)
and its [payload implementation](https://codeberg.org/zcodeberg/roborock-q10-utils-gpl/src/branch/main/q10_custom_clean.py).

## Confirmed technical direction

The Home Assistant Q10 vacuum implementation accepts B01 DP commands through
`vacuum.send_command`. The dedicated Q10 adapter will use that public Home
Assistant service boundary rather than creating a direct Roborock client or
storing credentials.

### Initial product decisions

- The Q10 adapter will select the exposed `customized` cleaning mode before
  every custom-clean start. This follows observed native-app behavior. A
  controlled validation will still confirm the precise required command order.
- `max_plus` is unsupported for Q10 custom cleaning in the initial release. A
  request that resolves to it fails safely as profile-incompatible; it is not
  downgraded to another fan level. Support can be added only after a dedicated
  protocol test.
- A custom profile is transient dispatch preparation, not durable robot state.
  Build and send it immediately before the corresponding start and do not
  reuse it for later work. Observed app behavior indicates the robot resets it
  after completion, so no post-completion restoration command is planned.

For one mapped room, the native sequence under investigation is:

1. Send `dpCommon` with a `CUSTOMER_CLEAN` (DP 62) payload immediately before
   starting. The payload is base64-encoded bytes consisting of the room count
   followed by six bytes per room: room ID, fan level, water level, clean type,
   clean count, and line pattern. For two passes, clean count is `2`.
2. Select the vacuum's exposed `customized` cleaning mode.
3. Send `dpStartClean` with `{"cmd": 2, "clean_paramters": [room_id]}`.

Only step 3 starts physical work. Steps 1 and 2 must never be retried after a
start attempt, and a failure or uncertain outcome must use the existing
durable scheduler-wide dispatch halt.

The reference prose says the customized mode is required, while its executable
helper writes the custom profile and starts the room clean without selecting
that mode. The integration will select the mode regardless, based on observed
native-app behavior. Hardware validation remains a release gate for the exact
command order and the resulting clean behavior.

## Scope and safeguards

- Select the Q10 path from protocol/capability evidence, not a friendly name,
  entity ID, fixed model string, room name, or native segment ID.
- Continue to resolve Home Assistant areas through the selected vacuum's
  current `area_mapping` immediately before dispatch. Native room IDs are
  transient: do not persist, log, project, or hard-code them.
- Preserve all existing eligibility gates. Occupancy, bedroom-transit,
  Party Mode, observe-only mode, shutdown, profile compatibility, and robot
  readiness block every stage before it reaches Home Assistant.
- Advertise two passes only after the Q10 probe proves that the mapping and
  custom-clean command path are usable. Unsupported or uncertain hardware
  remains on the portable one-pass path.
- A requested two-pass clean must fail safely when unsupported; it must not
  silently fall back to one pass or issue a second physical clean.
- Keep the public scheduler pass choices at one or two. Although the Q10
  protocol documents `cleanCount` up to three, a third pass is out of scope
  until its duration, controls, and safety behavior are designed separately.

## Profile constraints

The Q10 custom profile requires values that the portable area-clean service
does not carry. The adapter must validate every one rather than infer or
silently change a user-selected profile.

- Map only known compatible fan-speed values to Q10 `funLevel` values 1--4.
  `max_plus` is explicitly unsupported for the initial custom path unless a
  verified protocol mapping is added in later work.
- Start with an explicitly supported operation and known `cleanType`. Do not
  enable custom mopping until `waterLevel`, mop readiness, and cleaning-line
  behavior have verified mappings.
- Define the line-pattern value as an intentional, documented Q10 adapter
  setting or a tested fixed profile; do not assume that it preserves an
  unrelated vacuum-app setting.
- Validate mapped room IDs as current, unambiguous integral values representable
  by the native byte payload. Reject malformed, stale, cross-map, or ambiguous
  mappings before any command is sent.

## Implemented follow-up: Cleaning Depth

Expose the Q10 custom-clean `cleanLine` setting as **Cleaning Depth** at both
the robot-default and room-override levels. The verified protocol values are:

| UI option | `cleanLine` | Intended behaviour |
| --- | ---: | --- |
| Fast | `0` | Faster, lower-coverage route |
| Daily | `1` | Current default route |
| Fine | `2` | Higher-coverage route |

### Compatibility and persistence rules

- Eligible Q10 robot defaults are **one pass** and **Daily** depth. Rooms
  inherit both defaults unless they explicitly override either one. The
  migration records initialization separately, so a later explicit
  **Not configured** selection is not mistaken for legacy state.
- The resolved depth becomes part of the exact scheduled profile and follows
  the same robot-default then room-override precedence as fan speed and mode.
- Only a Q10 adapter that proves its native custom-clean capability may offer
  the controls. A room override remains incompatible with an assigned robot
  that cannot support it; the scheduler must not silently discard it.
- Pass count and Cleaning Depth are independent. A Q10 room may use Fast,
  Daily, or Fine with either one or two vacuum passes. A selected depth uses
  the Q10 custom-clean profile even for one pass; an explicitly unset depth
  retains the portable one-pass route.
- Observations indicate a custom profile can persist while the robot remains
  in `customized` mode. Treat that as a behavior to characterize in hardware
  tests, not a dispatch dependency: each supported start should initially
  write its exact requested profile immediately before starting. Any later
  optimization that reuses a profile must prove equivalence across mode
  changes, docking, and completion.

### Implemented changes and remaining hardware validation

1. The optional `cleaning_depth` field is persisted with exact stage profiles,
   exposed in status projections, and remains compatible with state that
   predates the field.
2. Eligible Q10 adapters advertise `fast`, `daily`, and `fine`; the integration
   creates **Cleaning Depth** robot and same-floor room selects, and keeps both
   dashboard copies in parity.
3. Q10 payload validation maps only these three values to `0`, `1`, or `2`;
   vendor payloads and map targets remain transient.
4. Unit tests cover the Daily payload, Fine with both one and two passes,
   unsupported values, persisted profile compatibility, and dashboard parity.
5. With a docked Q10 in an unoccupied mapped room, run controlled starts for
   Daily and one non-default depth. Observe start/cleaning/completion, then
   characterize profile persistence while staying in `customized` mode, after
   leaving that mode, after docking, and after completion. Do not rely on
   persistence unless those tests prove it safe.
6. Release through the normal semantic-version, HACS, Home Assistant restart,
   and live-verification flow.

## Implementation plan

1. Add a Q10-specific capability probe and adapter branch under the existing
   Roborock adapter contract. The generic adapter remains the fallback, and
   the existing legacy native repeat path remains limited to protocols that
   actually support it.
2. Add pure payload/profile-validation helpers, preferably in `models.py` or
   an adapter-local pure module. Unit test exact encoding, one/two counts,
   invalid room IDs, unsupported fan values, operation compatibility, and
   malformed mapping evidence.
3. Add a staged Q10 dispatch operation that rechecks live scheduler safety
   before each Home Assistant service call. Log safe protocol phase and adapter
   context for failures, but never raw payloads, native IDs, or raw integration
   exceptions in dashboard-facing state. Send the custom profile only in this
   immediate pre-start sequence; never cache or restore it after an observed
   completed clean.
4. Model the pre-start configuration phases explicitly in the active-job
   checkpoint without persisting payloads or native identifiers. On restart,
   observed vacuum state remains authoritative; the integration must never
   replay a profile write or start command solely from persisted state.
5. Add adapter/runtime tests that assert service names, command order, exactly
   one physical start, no fallback after Q10 start is attempted, durable halt
   behavior, and preservation of a due room after failure.
6. Perform a controlled manual validation in one unoccupied mapped room after
   explicit approval. Verify both candidate mode sequences, the requested
   two-pass path, observed state transitions, completion, and a subsequent
   one-pass clean. Record only portable results in release documentation.
7. Advertise Q10 two-pass capability only after validation passes, then run the
   full repository test and compilation checks. A production integration
   change requires the normal semantic version bump, tagged release, HACS
   installation, and Home Assistant restart procedure.

## Acceptance criteria

- A verified compatible Q10 vacuum starts one room-clean operation using a
  custom profile with `cleanCount: 2`.
- The scheduler never sends `app_segment_clean` to a Q10 protocol device and
  never approximates two passes with two starts.
- Unsupported profile values, mappings, and devices reject safely before a
  physical start; a selected dispatch failure engages the existing global halt.
- No local map details, native IDs, custom payloads, credentials, or raw vendor
  errors enter Store data, dashboard entities, Repairs, source, or logs.
- Existing generic and legacy-Roborock behavior remains covered by regression
  tests, and all required validation checks pass before release.
