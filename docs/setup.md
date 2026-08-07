# Setup

## Labels and room discovery

The integration treats every Home Assistant area on a discovered vacuum's dock
floor as a room. Use labels instead of editing configuration files:

| Label | Apply to | Effect |
| --- | --- | --- |
| `robovac-bedroom` | Area | Defaults to disabled, weekly cleaning cadence. |
| `robovac-bedroom-transit` | Area | Daytime-only; blocks while any bedroom is occupied. |
| `robovac-exclude` | Area | Never scheduled. |
| `robovac-radar` | Occupancy binary sensor | Preferred over older occupancy/motion sensors. |

Home Assistant normalizes the label registry IDs to underscores (for example,
`robovac_bedroom`). The names above are what you create in the UI; the
integration matches the normalized IDs.

Set every vacuum's device or entity area to its dock area. The integration gets
the served floor from that area's floor assignment. A newly assigned vacuum,
room, or sensor is picked up during the next evaluation without a reload.

## Native room mapping

For each vacuum, open its entity settings and map its vendor room segments to
Home Assistant areas. This is Home Assistant's built-in mapping used by
`vacuum.clean_area`; no segment names or IDs are stored in this project.

An unsupported or failed room dispatch is shown as **Unmapped** and is skipped.
Repair the native mapping, then use **Preview schedule**. The scheduler will
retry the room automatically on its next due evaluation.

## Occupancy rules

All radars must be clear to mark a room vacant. If one is unavailable, the
integration can use a complete all-clear legacy motion/occupancy fallback. A
room without any occupancy source is eligible when due. A new entry after a
clean starts does not interrupt it.

When an occupancy source is unresolved, the room remains blocked during the
day and is retried only in the configured quiet-night window (01:00–05:00 by
default). Bedroom-transit areas are never included in that exception: they
remain daytime-only and still require every bedroom to be clear. The dashboard
provides start and end controls for the quiet-night window.

Enable a room's **Carpet (no mopping)** switch to make that room vacuum-only.
Its stored mopping cadence and history are retained so turning the switch back
off restores the prior mop schedule; carpeted rooms never receive a mop-only or
vacuum-and-mop dispatch while the switch is on.
