# Issue #2 feature plans

These documents break [GitHub issue #2, **Features**](https://github.com/Googlproxer/adaptive-robovacs/issues/2)
into one implementation plan per checklist item. They are planning artifacts;
none of the issue items is complete until its plan has been implemented,
validated, released, installed through HACS, and verified in Home Assistant.

## Baseline

The checked-out `main` branch is integration version 1.2.1. Pull request
[#3](https://github.com/Googlproxer/adaptive-robovacs/pull/3) has been merged;
it introduced a typed Store codec and separated runtime service calls, job
lifecycle mutations, and dashboard projections. The remaining plans use that
refactored shape:

- durable configuration and migrations in `state.py`;
- pure safety decisions in `models.py`;
- Home Assistant observations and service calls in `runtime.py`;
- entity-facing data in `projections.py`; and
- orchestration in `coordinator.py`.

Per-room daily cleaning windows shipped in v1.2.0. Version 1.2.1 contains the
subsequent docked-completion recovery fix and is the baseline for the remaining
feature work.

## Plans

| Issue item | Confirmed direction | Plan |
| --- | --- | --- |
| Per-room cleaning windows | One repeating daily interval; weekday/weekend schedules are deferred | [Implemented in v1.2.0](01-per-room-cleaning-windows.md) |
| Multipass support | Add a generic/vendor adapter contract, then use a Roborock adapter for mapped native two-pass cross-hatching | [Vacuum adapters and Roborock native multipass](02-room-multipass.md) |
| Mopping when water is available | A robot without the required live water/mop signals does not support scheduler mopping | [Water-aware mopping](03-water-aware-mopping.md) |
| Cross occupancy detection via room list | Symmetric adjacency: occupancy in either room blocks the other | [Adjacent-room occupancy blockers](04-cross-room-occupancy.md) |
| Power level settings per room | Native fan speed, presented with the other supported per-room robot behaviors | [Per-room cleaning profiles](05-room-power-levels.md) |
| Confirm with message before bedrooms | Assign one user and phone to each bedroom and send an actionable notification for each run | [Bedroom confirmation](06-bedroom-confirmation.md) |

## Live capability findings

The Home Assistant instance was inspected on 2026-08-10 to resolve the issue's
capability questions. These documents intentionally record only portable
integration metadata and behavior, never local entity IDs, device IDs, room
names, map details, or notification targets.

- One Roborock device exposes a complete same-device set for mop attachment,
  water-box attachment, and water shortage. The other robot exposes no water
  telemetry and therefore must be treated as vacuum-only by this scheduler.
- Both robots expose Home Assistant's standard fan-speed feature, but their
  advertised values differ. Cleaning mode values also differ, while mop mode
  and mop intensity are available only where supported.
- The current Home Assistant `vacuum.clean_area` action accepts an area but no
  pass count. Home Assistant retains the user-maintained area-to-segment
  mapping, and the Roborock implementation omits the native repeat value when
  cleaning those segments. The revised multipass plan adds an integration-owned
  adapter layer: the Roborock adapter may resolve Home Assistant's mapping at
  dispatch time and issue the native repeat command without persisting a
  second mapping.

## Shared constraints

Every implementation must retain these repository contracts:

- Discover live robots, areas, floors, capabilities, occupancy sources, and
  notification targets from Home Assistant. Never add deployment-specific
  entity IDs, area IDs, device IDs, native segment IDs, service names, or
  credentials to source.
- Home Assistant areas remain the scheduler's public target. Vendor adapters
  may use native commands only after resolving the requested area through the
  selected vacuum's current Home Assistant area mapping. Native target IDs are
  transient and must never be hard-coded, persisted, logged, or projected.
- Keep Party Mode and observe-only mode non-dispatching. Occupancy and
  bedroom-transit rules remain mandatory and are rechecked immediately before
  dispatch.
- Persist scheduler-owned settings, pending work, and migrations through Home
  Assistant `Store`. Robot observations remain authoritative over saved
  estimates after a restart.
- Put reusable scheduling decisions in `models.py` and cover them in
  `tests/test_models.py`. Add focused state, runtime, service, entity, and
  dashboard contract tests where the implementation boundary warrants them.
- Build dashboard choices from live registries and advertised capabilities.
  Keep both dashboard JavaScript copies byte identical and preserve existing
  public entity and unique IDs.
- Log complete dispatch, profile, and notification failures with safe context
  while exposing only generic dashboard errors. Failures must not permanently
  exclude a room.
- Treat each shipped item as an integration release: bump `manifest.json`, run
  the complete validation suite, create the matching annotated semantic tag
  and GitHub Release, update through HACS, and restart Home Assistant only after
  confirming both vacuums are not cleaning.

## Recommended implementation order

1. Merge or otherwise settle PR #3 so all later state changes have one
   migration path.
2. Implement per-room daily windows and symmetric room adjacency, which are
   independent scheduling gates.
3. Introduce the generic/vendor vacuum adapter contract and the Roborock
   adapter while adding native two-pass room cleaning. Unadapted vendors retain
   the current Home Assistant behavior.
4. Introduce the shared room cleaning-profile model and dashboard controls.
   Fan speed and water-gated mop controls consume the normalized adapter
   capabilities instead of adding vendor checks to the scheduler.
5. Add durable bedroom assignments and confirmation after ordinary candidate
   gates are stable. Approval authorizes a re-evaluation; it never bypasses a
   safety gate.

The features may be released separately. Each release includes only the
migrations and public controls needed by that release.

## Resolved product decisions

1. The first cleaning-window release supports one daily start/end interval per
   room. Weekday/weekend schedules may be added later.
2. Multipass means Roborock's native two-pass cross-hatching. Roborock is the
   first vendor adapter. Adapters may run native commands, but native targets
   must be resolved from the user-maintained Home Assistant area mapping for
   every dispatch and must never be persisted by Adaptive RoboVacs. Vacuums
   without an adapter retain the portable Home Assistant behavior.
3. Scheduler mopping requires authoritative water/mop telemetry. A robot with
   no supported water signal is not mop-capable in this integration.
4. Cross-room occupancy models undirected adjacency. If either adjacent room
   is occupied, a new clean cannot start in the other.
5. Power means fan speed. The dashboard should expose all supported
   per-room cleaning-profile behaviors without assuming identical option names
   across robots.
6. Each bedroom is assigned a Home Assistant user and Companion-app phone. The
   assigned phone receives a per-run actionable confirmation request.
