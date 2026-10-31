# Migration to v1.20.0

## Water tracking

The integration discovers the separate Roborock dock through vendor device
identifiers and its owning integration entry. Names and areas do not pair docks.
Clean-water and dirty-water flags are problem reports: empty/full **or missing**.
They are not water percentages, and can remain cached until a wash or mop starts.

Water snapshots read the Home Assistant state cache five minutes after settled
docking, then hourly while docked. Washing or leaving the dock cancels that
sequence. Restarting schedules one new check without replaying missed hours.
These checks do not refresh vendor data, start washes, or evaluate the schedule.
Tank entities are not added to state-change subscriptions.

An unresolved episode produces one tagged mobile notification per robot, using
the existing discovered Companion targets. It identifies the tanks requiring
refill/refitting or emptying/refitting, and includes onboard shortage when
reported. Changed problems update the same tag. A reported problem younger than
60 seconds gets one confirmation snapshot five minutes later. Unknown readings
preserve existing problems; healthy snapshots clear them. No periodic reminder
is sent for an unchanged problem.

## Scheduled mop verification

A reported dock tank problem prevents a new mop dispatch. At each otherwise
eligible scheduled mop occurrence, one native dock wash can check whether the
report is stale. Multi-room runs share that attempt. Routine water snapshots
never initiate washing, and explicit dashboard mop requests do not bypass a
reported tank problem. Vacuum-only work remains eligible.

Normal occupancy, windows, robot readiness, faults, Party Mode, observe-only,
and shutdown checks apply before a verification command. The wash must start
within two minutes and complete within ten minutes of the request. Five minutes
after terminal docking, a snapshot must report healthy tanks and no dock error.
Command acceptance alone is not proof of water availability. After success the
normal dispatch gates run again. Failed or unverifiable checks skip the affected
mop stage; the next scheduled occurrence may retry. A timed-out wash is not
automatically stopped, and observed hardware activity continues to block work.

Verification creates no cleaning job, cleaning credit, duration sample, permanent
room exclusion, or dispatch fault. Its checkpoint survives restart and is never
blindly resent. Fresh robot observations decide how recovery proceeds.

## Store and dashboard

Store payload schema advances from 20 to 21; its Home Assistant Store envelope
remains version 1. Existing jobs, runs, settings, histories, and battery samples
are retained. The new robot water records use stable robot registry identities.
No public entity IDs or unique IDs change. Both shipped dashboard copies add
last-reported water conditions, snapshot time, and verification progress.

After deploying and verifying mobile delivery, disable any existing standalone
low-water automation for a monitored robot to prevent duplicate alerts. Preserve
the automation configuration for rollback. Do not disable it before the
replacement's installed behaviour and recipients have been verified.

## Deployment

Publish an annotated `v1.20.0` tag and full GitHub Release after full validation
and release authorization. Install that exact tag through HACS, validate HA
configuration, and restart only when both vacuums are not cleaning. Verify the
integration loads, the dashboard resource reloads, and water snapshots and
notification delivery work before retiring legacy water alerts. This migration
does not authorize a Home Assistant Core upgrade or a test wash on live hardware.
