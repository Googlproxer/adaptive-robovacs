# Dynamic dashboard cards

The integration serves three target-scoped cards after Home Assistant starts.
Add the module once in **Settings > Dashboards > Resources**:

```yaml
url: /api/adaptive_robovacs/frontend/adaptive-robovacs-dashboard.js
type: module
```

The available card types are:

| Card | Instances | Contents |
| --- | --- | --- |
| `custom:adaptive-robovacs-global` | One | Scheduler status, Repairs halt/resume, Party Mode, observe-only mode, cleaning windows, forecast confidence, and schedule preview. |
| `custom:adaptive-robovacs-vacuum` | One per vacuum | Status/activity, water readiness, cleaning program, independent vacuum/mop pass defaults, adapter capabilities, safe failure diagnostics, and robot-owned controls. |
| `custom:adaptive-robovacs-room` | One per room | Single-cadence schedule, last-cleaned and occupancy status, program and exact profile overrides, independent vacuum/mop pass overrides, occurrence/water-confirmation diagnostics, and three manual actions. |

All three cards are available in Home Assistant's card picker and have visual
editors. The integration entry is optional when only one Adaptive RoboVacs
entry is loaded. Select a vacuum entity for each vacuum card and a Home
Assistant area for each room card. An optional title overrides the live vacuum
or room name. Vacuum and room rows omit that target name because the card title
already supplies it; this is a display-only override and does not rename entities.

The cards discover their rows through integration attributes rather than fixed
entity IDs. A newly supported control therefore appears automatically on its
existing vacuum or room card. A newly discovered vacuum or room still needs a
new card to be added and positioned manually.

Each room card places two mobile-friendly selectors immediately below its
occupancy status:

- **Cleaning period** has **Default**, **Off**, **Night** (00:00–06:00), **Morning**
  (06:00–12:00), **Afternoon** (12:00–18:00), **Evening** (18:00–00:00), and
  **Custom**. **Default** enables the room and inherits both global Desired
  cleaning bounds. Choosing a named period enables the room and writes its
  matching daily bounds. **Off** disables scheduling while retaining the
  existing bounds. **Custom** enables the room with a 09:00–20:00 window,
  which can be refined through the detailed start/end controls.
- **Cleaning profile** is **Robot default** or **Custom**. Robot default clears
  every room-level cleaning-program, pass, fan, mode, mop, and depth override
  so the matching robot defaults apply. Custom preserves the saved overrides
  and reveals the individual profile controls. Those individual entities remain
  available to automations regardless of the card view.

The card shows **Desired cleaning start** and **Desired cleaning end** only for
the **Custom** period. They offer 15-minute values plus **Use global**; start
and end inherit independently, an overnight interval is supported, and equal
effective bounds are invalid. Rooms that already inherit both global bounds
display **Default** after upgrading.
The room schedule entity reports the configured bounds, effective bounds,
inheritance flags, validity, and next usable start as attributes.

Each room has **Vacuum passes** and **Mop passes** selectors:

- **Robot default** preserves the matching vacuum-card pass default.
- **1 pass** explicitly requests one portable pass.
- **2 passes** requires a compatible adapter and never silently downgrades or
  emulates the request with a second dispatch.

Existing rooms with one or more saved profile overrides begin in **Custom** so
the upgrade preserves their configuration and keeps those controls visible.

Compatible Roborock vacuums perform two passes with one native cross-hatched
segment command. Other vendors keep the portable Home Assistant path until a
vendor adapter advertises enhanced support.

The vacuum **Cleaning program** defines its default operation order. A room can
inherit it or choose Vacuum only, Mop only, Vacuum then mop, or Mop then vacuum.
The room schedule row exposes the persisted occurrence/current stage, last
terminal stage outcome, water-confirmation deadline/status, and the Roborock
water-readiness reason through its attributes.

Robot cards provide defaults for fan speed, cleaning mode, mop mode, and mop
intensity when the adapter advertises those controls. Room cards expose
**Robot default** plus the live same-floor option union for each applicable
field. Assignment still requires one robot to support the complete exact
profile; unsupported saved values remain visible and produce a Repair rather
than being changed silently.

Each room card ends with **Manual clean**, **Manual vacuum only**, and **Manual
mop only**. These are immediate integration-owned requests, not shortcuts to
the external manual-clean observer. They use the displayed effective profile
and pass counts and bypass only cadence and the desired window. A currently
blocked press is rejected rather than queued. The room schedule attributes
expose the latest dashboard-manual outcome and any active occurrence source.

## Migrating from 1.3

Version 1.4 replaces the two vacuum/mop cadences with one room cleaning cadence.
The previous vacuum cadence value becomes that cadence; the most recent vacuum
or mop completion becomes the initial cadence anchor. The old **Mop cadence**
entity is retired. Existing room **Cleaning passes** becomes **Vacuum passes**
with the same stable entity ID; Mop passes starts at the one-pass robot default.
The robot **Mopping enabled** switch is replaced by **Cleaning program**:
disabled migrates to Vacuum only, while enabled migrates to Vacuum then mop for
a robot whose adapter verifies mopping.

If a scheduler-selected clean fails to start, the global status changes to
**Scheduler halted**, and the matching vacuum and room status rows show the
same safe failure. Home Assistant also creates a Repair. After correcting the
vacuum availability, vendor integration, or area mapping, press **Recheck and
resume**. The dashboard asks for confirmation; the recheck sends no vacuum
command and resumes only if the prerequisites are valid. Dismissing the Repair
does not resume dispatch.

## Four-column layout

Use a native Home Assistant **Sections** view with four maximum columns. Put
the global card and every vacuum card in the first section, ground-floor room
cards in the second, upper-floor non-bedroom cards in the third, and bedroom
cards in the fourth. Sections automatically reflow to fewer columns on smaller
displays.

Start with `dashboard/example-dashboard.yaml`, or create the view in the visual
editor and arrange it like this:

```yaml
type: sections
max_columns: 4
sections:
  - type: grid
    cards:
      - type: custom:adaptive-robovacs-global
      - type: custom:adaptive-robovacs-vacuum
        vacuum_entity_id: vacuum.first_robot
      - type: custom:adaptive-robovacs-vacuum
        vacuum_entity_id: vacuum.second_robot

  - type: grid
    cards:
      - type: custom:adaptive-robovacs-room
        area_id: ground_room_one

  - type: grid
    cards:
      - type: custom:adaptive-robovacs-room
        area_id: upper_room_one

  - type: grid
    cards:
      - type: custom:adaptive-robovacs-room
        area_id: bedroom_one
```

The identifiers above are placeholders. Choose the real targets through each
card's visual editor; do not copy the placeholder values into a live dashboard.
If more than one Adaptive RoboVacs config entry exists, select the intended
entry on every card as well.

Omit a room card when that room should not appear on a dashboard. This changes
presentation only: turn off the room's scheduler enable switch if it must not
be scheduled.

Held robots continue to show paused, error, return-to-dock, or completion state
through their status entity. Resume or cancel them with the robot's physical
controls; these cards deliberately do not add acknowledgement or cancellation
actions.

## Migrating from 1.0

Version 1.1 removes `custom:adaptive-robovacs-dashboard`. Before or immediately
after updating:

1. Change the view to **Sections** with a maximum of four columns.
2. Add one `custom:adaptive-robovacs-global` card to the first section.
3. Add one `custom:adaptive-robovacs-vacuum` card per vacuum below it.
4. Add one `custom:adaptive-robovacs-room` card for every room and place it in
   the appropriate floor or bedroom section.
5. Remove the old uber-card configuration, including its `columns` and
   `hidden_area_ids` options.

The standalone copy in `dashboard/adaptive-robovacs-dashboard.js` remains
available for dashboards using `/local/`, while the integration-served URL is
the supported default.
