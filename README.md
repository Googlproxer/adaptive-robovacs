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
  and capability-driven mopping controls.
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

See [setup and migration](docs/setup.md), [dashboard setup](docs/dashboard.md),
and [legacy decommissioning](docs/decommissioning.md).

## Safety

The integration does not stop a clean already in progress if a room becomes
occupied. It never dispatches work in observe-only mode, Party Mode, or where
occupancy is unresolved. Rooms that fail native area dispatch are marked
unmapped and skipped until their map binding is repaired.

## License

MIT. See [LICENSE](LICENSE).
