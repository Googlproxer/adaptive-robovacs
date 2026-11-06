# v1.21.0: robot-aware occupancy

Requires Home Assistant Core 2026.11.0 or newer. Store schema 21 migrates to 22;
the Home Assistant Store envelope remains version 1. Entity IDs and unique IDs
are unchanged.

## Attribution policy

The Roborock V1 adapter listens to existing native map-content and coordinator
updates. It reads cached numeric map/segment evidence and resolves it through
the vacuum entity registry's saved area mapping. Native room names are never
matched to Home Assistant room names. Unsupported contracts retain ordinary
occupancy handling; no additional robot queries or map captures are requested.

An occupancy onset with an established preceding clear interval is provisional
for up to 120 seconds. New automatic work remains blocked during that period.
A fresh observed robot visit within the matching interval can explain the pulse.
The earlier human-vacancy anchor then remains continuous and the robot pulse
does not become a completed vacancy-training sample.

If room updates skip an intermediate area, only an unavoidable intermediate room
in the saved same-floor floor-plan links can explain transit. The inference is
bounded by fresh observed endpoints. An ambiguous route is not guessed.

Human occupancy that was established before the matching interval remains human
until the selected occupancy sources clear. A later robot arrival cannot erase
it. Occupancy persisting 180 seconds after the last matching robot departure, or
outlasting fresh location evidence (180 seconds), becomes blocking mixed/uncertain
occupancy. It is censored from learning. A fresh clear observation starts the next
vacancy interval. Losing or switching occupancy sources also censors continuity.

The policy intentionally accepts that human presence beginning and ending with
a matching robot visit can be missed. It is not a human-identification sensor.
Stationary docked robots do not continually exempt their dock room. Actual
robot traffic independently blocks new automatic starts into that target room,
including final dispatch revalidation, without resetting human vacancy.
Classified human or unresolved occupancy also blocks the next physical stage of
a managed current-room program. An already-running physical clean is not stopped.

## History and restart

When a room first gains supported attribution, its old vacancy-training samples
are cleared once. Those samples have no robot/human provenance, so selective
repair is not reliable. Last-cleaned records, cadence, stage outcomes, duration
learning, battery learning and durable jobs remain intact. Other rooms retain
their learning. Normal vacancy fallback remains available while history rebuilds.

Pending attribution checkpoints are stored through Home Assistant Store. They
are never trusted across an observation gap: startup censors an interrupted
episode, clears its vacancy anchor and requires fresh observations. The existing
startup settling period and all normal mode, recovery and dispatch gates remain.

Take a Home Assistant backup before deploying. v1.20.3 cannot read schema 22;
rolling back requires the matching pre-upgrade scheduler Store/backup as well
as the earlier integration code.

## Diagnostics

The existing room occupancy sensor continues reporting raw occupancy. Its added
attributes report effective occupancy, classification and attributed robots.
Room Status additionally exposes the preserved vacancy anchor through its usual
vacancy diagnostics, attribution evidence (direct or inferred), episode onset
and the next attribution deadline. Room-card Occupancy evidence explains pending,
robot, human and uncertain classifications. Both dashboard copies remain identical.

## Performance

Location notifications are immutable FIFO inputs. Unchanged location heartbeats
refresh bounded presence evidence without evaluating the schedule, saving the
Store or publishing entity snapshots. Only meaningful location/occupancy changes
and deadline expiry request evaluation. Route inference is cached and recent
visits are bounded per robot. Deadline timers replace periodic attribution polling.

v1.20.3's occupancy-history validation/fingerprint cache and checkpoint durability
remain in use. See [performance measurements](performance.md).
