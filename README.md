# Adaptive RoboVacs

Adaptive RoboVacs is a registry-driven Home Assistant custom integration for
occupancy-aware room cleaning. It discovers vacuum cleaners, floors, areas, and
occupancy sensors at runtime, so replacing hardware or adding a room does not
require editing scheduler code.

The integration includes a local Home Assistant brand icon in
`custom_components/adaptive_robovacs/brand/`; Home Assistant loads it
automatically on supported versions.

## What it does

- Schedules common rooms and opt-in bedrooms using adjustable per-area cadence.
- Uses Home Assistant's native vacuum segment-to-area mapping and calls
  `vacuum.clean_area` with Home Assistant area IDs.
- Prefers occupancy sensors labelled `robovac-radar`, with occupancy/motion
  binary sensors in the same area as a fallback.
- Supports party mode, manual-clean deferrals, learned vacancy forecasts,
  restart recovery, multi-robot ready-first allocation, double-pass settings,
  carpet-aware vacuum-only rooms, and capability-driven mopping controls.
- Provides a self-updating Lovelace card and generated room/robot controls.

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
6. Add the supplied dashboard card resource and use the example dashboard
   configuration. Review schedule previews before turning off observe-only mode.

See [setup](docs/setup.md) and [dashboard setup](docs/dashboard.md). Home
Assistant user-initiated room cleans are tracked automatically; native
vacuum-app starts are intentionally left untracked.

## Safety

The integration does not stop a clean already in progress if a room becomes
occupied. It never dispatches work in observe-only mode or Party Mode. A room
with unresolved occupancy is retried only in the configured quiet-night window;
bedroom-transit rooms remain excluded from that exception. A failed native-area
dispatch is recorded as an **unknown error** in the room's diagnostic metadata,
with the complete cause in the Adaptive RoboVacs integration log rather than a
misleading map-repair instruction. The room remains eligible for future
scheduler attempts, and a successful dispatch clears the error.

An observed robot pause or error also creates a durable per-robot scheduler
hold. The hold survives an automatic idle transition and Home Assistant
restarts, so an expected duration can never resume work after a fault. Resuming
the clean on the robot releases the hold only after Home Assistant observes it
cleaning again; a completed clean is then tracked normally. A user-initiated
Home Assistant **Stop** or **Return to base** cancels the held job without
crediting the room as cleaned. For a native-app cancellation, first leave the
robot docked or idle, then press that robot's **Confirm held clean cancelled**
button. The button only releases scheduler state; it never sends a command to
the vacuum.

## Releases and upgrades

Production updates are published as full GitHub Releases with semantic tags
(for example, `v1.0.5`). HACS uses the latest release tag as the deployable
version, not a moving commit from the default branch. Each release must bump
the integration version in `custom_components/adaptive_robovacs/manifest.json`,
pass the test suite, and have matching tag and release versions.

## License

MIT. See [LICENSE](LICENSE).
