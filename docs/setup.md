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
`vacuum.clean_area` and by enhanced adapters; no segment names or IDs are
stored in this project.

Every discovered vacuum resolves to one adapter from stable Home Assistant
entity-registry platform metadata. The generic fallback retains the portable
`vacuum.clean_area`, profile select, and fan-speed actions. A compatible
Roborock adapter additionally supports native two-pass cross-hatched room
cleaning. It rereads the selected vacuum's current area mapping immediately
before dispatch, accepts numeric or unambiguous single-map compound segments,
and fails closed on missing, stale, malformed, or multi-map evidence.

Each room's **Cleaning passes** selector supports **Robot default**, **1 pass**,
and **2 passes**. Robot default preserves the vacuum card's existing
**Double pass** default. An explicit two-pass room is eligible only for a
vacuum advertising native two-pass capability. The scheduler never emulates
two passes with two commands and never silently downgrades the room.

## Occupancy rules

All radars must be clear to mark a room vacant. If one is unavailable, the
integration can use a complete all-clear motion/occupancy fallback. A
room without any occupancy source is eligible when due. A new entry after a
clean starts does not interrupt it.

The global **Desired cleaning start** and **Desired cleaning end** provide the
default daily interval. Every room card also has its own start and end select.
Choose **Use global** to inherit that bound or choose a 15-minute value to
override it for only that room. The two bounds inherit independently, so a room
can override its start while continuing to follow changes to the global end.
Intervals are half-open (the end minute is excluded), may cross midnight, and
are invalid when start and end are identical. An invalid pair blocks
window-bound scheduling until either bound changes. Weekday/weekend schedules
and multiple daily intervals are not part of this release.

Due rooms wait for their effective window's next usable start by default;
enable a room's **Ignore desired cleaning window** switch to let an
otherwise-safe clean run outside it. A room with unresolved occupancy is
eligible only inside its own effective desired window, even when that room
ignores the usual timing preference. Bedroom-transit areas are never included
in that unresolved exception: they retain their separate daytime-only and
every-bedroom-clear rules. Party Mode and observe-only mode remain
non-dispatching regardless of any room window setting.

Enable a room's **Carpet (no mopping)** switch to make that room vacuum-only.
Its stored mopping cadence and history are retained so turning the switch back
off restores the prior mop schedule; carpeted rooms never receive a mop-only or
vacuum-and-mop dispatch while the switch is on.

## v1.3.0 troubleshooting

### Scheduler halted after a start failure

The first scheduler-selected clean that fails during adapter preflight,
profile application, service dispatch, or two-minute start confirmation stops
all later scheduler dispatch across every vacuum. Existing and manually
started cleans are not stopped. The failure is durable across Home Assistant
restarts and appears in three places:

- **Scheduler halted** on the global card;
- the same status on the affected vacuum and room cards, with safe diagnostic
  attributes; and
- one persistent error in **Settings > System > Repairs**.

Check that the affected vacuum is available and docked, its vendor integration
is healthy, the required profile options still exist, and the requested room
is mapped through the vacuum entity's **Map vacuum segments to areas** action.
Then complete the Repair flow or press the dashboard's confirmed **Recheck and
resume** action. The shared recheck validates availability, battery, profile,
adapter capability, and mapping without sending a clean. If it succeeds, it
clears the halt and leaves the room due for the next normal safety-gated
evaluation.

Ignoring or dismissing the Repair, restarting Home Assistant, refreshing
discovery, or observing the vacuum start late does not resume future dispatch.
If the outcome was uncertain, the saved checkpoint remains authoritative until
the vacuum is safely docked and the explicit recheck succeeds.

### Mapping failures

- **Mapping missing**: add the Home Assistant area to the selected vacuum's
  segment mapping.
- **Mapping stale**: refresh the vendor integration/map and update the mapping
  so every target is present in the vacuum's current segment evidence.
- **Mapping ambiguous**: ensure the requested area belongs to one active map.
  The initial Roborock adapter deliberately rejects mixed or multiple map
  evidence instead of risking a clean in another room.

Adaptive RoboVacs never shows or stores native target IDs. For an advanced bug
report, include the integration version, safe failure code, affected friendly
vacuum/room names, and relevant Adaptive RoboVacs logs; redact any local map or
segment details before sharing.

### Two-pass compatibility Repair

If a room is saved as **2 passes** and no compatible vacuum serves its floor,
Home Assistant creates a separate Repair and leaves the room due but blocked.
Restore a compatible vacuum or change **Cleaning passes** to **Robot default**
or **1 pass**, then recheck. This pre-allocation configuration problem does not
engage the scheduler-wide halt because no start was attempted.
