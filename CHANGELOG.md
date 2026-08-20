# Changelog

All notable changes to Adaptive RoboVacs are documented here. The project uses
[Semantic Versioning](https://semver.org/). This file is the concise release
index; detailed upgrade, deployment, and validation notes for releases from
v1.3.0 onward remain in [`docs/releases`](docs/releases/).

## [Unreleased]

### Added

- Added this central changelog as the release-history index.

## [1.6.11] - 2026-08-20

### Fixed

- Mop stages now confirm an exact Mop-only operating mode before starting. If
  the robot cannot settle in that mode within 30 seconds, only mopping is
  skipped; combined vacuum-and-mop cleaning is never started in its place.
- A shared Roborock operation/mop selector can no longer overwrite Mop-only
  with a saved combined-mode value. This applies to standalone mop runs and
  the mop stage of Vacuum then mop programs.

## [1.6.10] - 2026-08-19

### Fixed

- Corrected the Roborock Q10 transport mapping for **Fast** and **Daily**
  cleaning depth. The dashboard order remains Fast, Daily, Fine, while Fast
  now sends line 1 and the observed fastest Daily clean sends line 0.

Detailed notes: [`docs/releases/v1.6.10.md`](docs/releases/v1.6.10.md).

## [1.6.9] - 2026-08-17

### Fixed

- Separated non-dispatching scheduler-halt recovery from readiness checks for a
  future clean. A docked or already-cleaning vacuum can now clear a stale halt,
  while battery, occupancy, mapping, adapter, and profile checks still apply
  before the next dispatch.
- Added actionable reasons when a halt cannot be cleared.

Detailed notes: [`docs/releases/v1.6.9.md`](docs/releases/v1.6.9.md).

## [1.6.8] - 2026-08-17

### Fixed

- Allowed scheduler-halt recovery while the affected vacuum is already
  cleaning, without interrupting or incorrectly crediting an unconfirmed room
  clean.
- Exposed untracked native-app cleaning as `native_app_assumed`.

Detailed notes: [`docs/releases/v1.6.8.md`](docs/releases/v1.6.8.md).

## [1.6.7] - 2026-08-17

### Fixed

- Corrected Home Assistant Repair flows so opening a Repair does not treat the
  initial form as confirmation and clear the issue.

Detailed notes: [`docs/releases/v1.6.7.md`](docs/releases/v1.6.7.md).

## [1.6.6] - 2026-08-14

### Added

- Added a per-robot **Stop and return to dock** control that preserves
  cancellation semantics until the robot is observed docked.

### Changed

- Removed the legacy room carpet/no-mopping toggle in favour of explicit
  programs and room mop controls. Store schema v8 removes the retired setting.

Detailed notes: [`docs/releases/v1.6.6.md`](docs/releases/v1.6.6.md).

## [1.6.5] - 2026-08-14

### Changed

- Made dashboard manual room cleans explicit user overrides. They require a
  compatible, docked robot and retain Party Mode and observe-only safeguards,
  but bypass normal scheduler timing and eligibility gates.

Detailed notes: [`docs/releases/v1.6.5.md`](docs/releases/v1.6.5.md).

## [1.6.4] - 2026-08-14

### Added

- Added Roborock Q10 custom-clean support for the native **Max+** fan-speed
  value, with a safe persisted downgrade to Max after a rejected or
  unconfirmed Max+ start.

Detailed notes: [`docs/releases/v1.6.4.md`](docs/releases/v1.6.4.md).

## [1.6.3] - 2026-08-14

### Fixed

- Corrected the initial Q10 Cleaning Depth default to **Daily** while
  preserving an intentional later **Not configured** choice.

Detailed notes: [`docs/releases/v1.6.3.md`](docs/releases/v1.6.3.md).

## [1.6.2] - 2026-08-14

### Added

- Added Q10 **Cleaning Depth** defaults and room overrides: Fast, Daily, and
  Fine. The selected depth is encoded into the transient custom-clean profile.

Detailed notes: [`docs/releases/v1.6.2.md`](docs/releases/v1.6.2.md).

## [1.6.1] - 2026-08-14

### Fixed

- Added safe recovery for an unstarted scheduled Q10 occurrence whose saved
  profile is no longer supported while the assigned robot is docked.

Detailed notes: [`docs/releases/v1.6.1.md`](docs/releases/v1.6.1.md).

## [1.6.0] - 2026-08-14

### Added

- Added one-command native two-pass room cleaning for compatible Roborock
  Q10/B01 vacuums using their custom-clean protocol.

Detailed notes: [`docs/releases/v1.6.0.md`](docs/releases/v1.6.0.md).

## [1.5.2] - 2026-08-14

### Changed

- Removed the redundant room-card manual-request status row while retaining
  the backend audit and normal cadence updates.

Detailed notes: [`docs/releases/v1.5.2.md`](docs/releases/v1.5.2.md).

## [1.5.1] - 2026-08-14

### Fixed

- Scoped profile validation to the current physical operation, so stale
  mop-only values cannot block a vacuum stage.

Detailed notes: [`docs/releases/v1.5.1.md`](docs/releases/v1.5.1.md).

## [1.5.0] - 2026-08-14

### Added

- Added inherited robot defaults and exact room-level cleaning profiles.
- Added safety-gated manual clean, vacuum-only, and mop-only room actions,
  completing GitHub issue #4. Store schema v7 made profiles and manual
  occurrences restart-safe.

Detailed notes: [`docs/releases/v1.5.0.md`](docs/releases/v1.5.0.md).

## [1.4.4] - 2026-08-13

### Fixed

- Hardened restart recovery, registry-stable robot identity, duration
  forecasting, Store validation, and config-entry shutdown behaviour.

Detailed notes: [`docs/releases/v1.4.4.md`](docs/releases/v1.4.4.md).

## [1.4.3] - 2026-08-13

### Fixed

- Completed mop-capability discovery for late-loading vendor mode selectors.

Detailed notes: [`docs/releases/v1.4.3.md`](docs/releases/v1.4.3.md).

## [1.4.2] - 2026-08-13

### Fixed

- Added a bounded, non-dispatching post-start capability refresh for vendor
  entities that become available after watcher registration.

Detailed notes: [`docs/releases/v1.4.2.md`](docs/releases/v1.4.2.md).

## [1.4.1] - 2026-08-13

### Fixed

- Made cleaning-program and profile controls refresh from live adapter
  capabilities after entity setup.

Detailed notes: [`docs/releases/v1.4.1.md`](docs/releases/v1.4.1.md).

## [1.4.0] - 2026-08-13

### Added

- Added water-aware ordered cleaning programs, independent vacuum/mop pass
  settings, and restart-safe multi-stage occurrences.
- Added authoritative water checks and fail-closed one-hour confirmation for
  mopping without water telemetry.

Detailed notes: [`docs/releases/v1.4.0.md`](docs/releases/v1.4.0.md).

## [1.3.2] - 2026-08-13

### Fixed

- Restored translated content and actionable context in Home Assistant Repair
  dialogs.

Detailed notes: [`docs/releases/v1.3.2.md`](docs/releases/v1.3.2.md).

## [1.3.1] - 2026-08-13

### Fixed

- Corrected late-loading vendor capability discovery so controls such as fan
  speed appear without another restart.

Detailed notes: [`docs/releases/v1.3.1.md`](docs/releases/v1.3.1.md).

## [1.3.0] - 2026-08-13

### Added

- Added typed generic and Roborock adapter contracts, native two-pass
  capability, room pass controls, scheduler-wide dispatch-failure halting,
  Home Assistant Repairs, and robot fan-speed selection.

Detailed notes: [`docs/releases/v1.3.0.md`](docs/releases/v1.3.0.md).

## Earlier tagged releases

The repository retains these releases as Git tags. They predate the detailed
release-note files above.

- [1.2.1](https://github.com/Googlproxer/adaptive-robovacs/tree/v1.2.1) (2026-08-11): fixed docked scheduler completion.
- [1.2.0](https://github.com/Googlproxer/adaptive-robovacs/tree/v1.2.0) (2026-08-10): added per-room daily cleaning windows.
- [1.1.1](https://github.com/Googlproxer/adaptive-robovacs/tree/v1.1.1) (2026-08-10): shortened target-card entity labels.
- [1.1.0](https://github.com/Googlproxer/adaptive-robovacs/tree/v1.1.0) (2026-08-10): split the dashboard into global, robot, and room cards.
- [1.0.11](https://github.com/Googlproxer/adaptive-robovacs/tree/v1.0.11) (2026-08-08): added desired cleaning windows.
- [1.0.10](https://github.com/Googlproxer/adaptive-robovacs/tree/v1.0.10) (2026-08-08): used observed physical recovery for held cleans.
- [1.0.9](https://github.com/Googlproxer/adaptive-robovacs/tree/v1.0.9) (2026-08-08): held scheduling after robot pauses and errors.
- [1.0.8](https://github.com/Googlproxer/adaptive-robovacs/tree/v1.0.8) (2026-08-08): simplified unresolved-room countdowns.
- [1.0.7](https://github.com/Googlproxer/adaptive-robovacs/tree/v1.0.7) (2026-08-08): added room retry behaviour after dispatch failures.
- [1.0.6](https://github.com/Googlproxer/adaptive-robovacs/tree/v1.0.6) (2026-08-08): simplified next-clean countdowns.
- [1.0.5](https://github.com/Googlproxer/adaptive-robovacs/tree/v1.0.5) (2026-08-08): improved safe room-status reporting.
- [1.0.4](https://github.com/Googlproxer/adaptive-robovacs/tree/1.0.4) (2026-08-08): added tracking for Home Assistant-initiated manual room cleans.
