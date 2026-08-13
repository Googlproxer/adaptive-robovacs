# Adaptive RoboVacs

Adaptive RoboVacs is a registry-driven Home Assistant custom integration for
occupancy-aware room cleaning. It discovers vacuum cleaners, floors, areas, and
occupancy sensors at runtime, so replacing hardware or adding a room does not
require editing scheduler code.

The integration includes a local Home Assistant brand icon in
`custom_components/adaptive_robovacs/brand/`; Home Assistant loads it
automatically on supported versions.

## What it does

- Schedules common rooms and opt-in bedrooms using adjustable per-area cadence
  and independently inherited daily cleaning windows.
- Resolves every vacuum through an integration-owned adapter. Unknown vendors
  retain the portable `vacuum.clean_area` path; compatible Roborock vacuums can
  use native two-pass cross-hatched room cleaning through Home Assistant's
  existing area mapping.
- Prefers occupancy sensors labelled `robovac-radar`, with occupancy/motion
  binary sensors in the same area as a fallback.
- Supports party mode, manual-clean deferrals, learned vacancy forecasts,
  restart recovery, multi-robot ready-first allocation, room pass overrides,
  carpet-aware vacuum-only rooms, and capability-driven mopping controls.
- Provides target-scoped Lovelace cards for global settings, each vacuum, and
  each room, with generated status and control rows.

## Installation

1. Copy `custom_components/adaptive_robovacs` to your Home Assistant
   `custom_components` directory and restart Home Assistant.
2. Add **Adaptive RoboVacs** from Settings > Devices & services. It starts in
   observe-only mode and will not start a vacuum.
3. Assign each vacuum's dock/device to an area with the correct floor.
4. Map the vacuum's native segments to Home Assistant areas using the vacuum
   entity's **Map vacuum segments to areas** action.
5. Add area labels as needed:
   `robovac-bedroom`, `robovac-bedroom-transit`, and `robovac-exclude`.
   Label radar occupancy entities `robovac-radar`. Home Assistant normalizes
   their underlying IDs to underscores (for example, `robovac_bedroom`), which
   the integration handles automatically.
6. Add the supplied dashboard card resource and compose the global, per-vacuum,
   and per-room cards using the example sections dashboard. Review schedule
   previews before turning off observe-only mode.

See [setup](docs/setup.md) and [dashboard setup](docs/dashboard.md). Home
Assistant user-initiated room cleans are tracked automatically; native
vacuum-app starts are intentionally left untracked.

## Safety

The integration does not stop a clean already in progress if a room becomes
occupied. It never dispatches work in observe-only mode or Party Mode. By
default, due rooms wait for their effective desired cleaning window. Each
room's start and end can independently use the global value or select a
15-minute daily override, including an interval that crosses midnight. Enable
a room's **Ignore desired cleaning window** switch to permit its otherwise-safe
clean outside those hours. A room with unresolved occupancy is retried only in
that room's effective window, and bedroom-transit rooms remain excluded from
that exception. A failed scheduler start is treated as a system failure. The
first adapter preflight, profile, service, or start-confirmation failure
persistently halts new dispatch across every robot and creates a Home Assistant
**Repair** with a safe explanation. A late robot state cannot clear that halt
automatically. Resolve the underlying availability or mapping problem, then
use the Repair flow or the dashboard's confirmed **Recheck and resume** action.
The recheck never sends a test clean, and the failed room remains due after
scheduling is explicitly resumed.

Native map and segment identifiers are read transiently from the selected
vacuum entity's current Home Assistant area mapping. They are never copied to
Adaptive RoboVacs storage, status entities, Repairs, or logs. See the
[v1.3 troubleshooting guide](docs/setup.md#v130-troubleshooting) for recovery
steps.

An observed robot pause or error creates a durable per-robot scheduler hold.
The hold survives automatic idle transitions and Home Assistant restarts, so an
expected duration can never resume work after a fault. Resume with the robot's
physical controls and the scheduler continues only after it observes
`cleaning`. Cancel with the physical dock control and the scheduler observes
`returning` followed by `docked`; it records no clean credit or
duration sample, then rebases enabled vacuum and eligible mop schedules on that
floor 24 hours into the future while retaining their relative spacing. A clean
already observed as complete before a later fault remains held until the robot
is observed at the dock, including when it is placed there manually. On
restart, a held docked job is treated as a completion only if Home Assistant
was offline for at least its expected clean
duration; otherwise it is treated as that physical cancellation.

## Releases and upgrades

Production updates are published as full GitHub Releases with semantic tags
(for example, `v1.0.5`). HACS uses the latest release tag as the deployable
version, not a moving commit from the default branch. Each release must bump
the integration version in `custom_components/adaptive_robovacs/manifest.json`,
pass the test suite, and have matching tag and release versions.

## License

MIT. See [LICENSE](LICENSE).
