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
| `custom:adaptive-robovacs-global` | One | Scheduler status, Party Mode, observe-only mode, cleaning windows, forecast confidence, and schedule preview. |
| `custom:adaptive-robovacs-vacuum` | One per vacuum | Status/activity plus every robot-owned setting and control. |
| `custom:adaptive-robovacs-room` | One per room | Schedule, last-cleaned and occupancy status plus every room-owned setting and control. |

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
