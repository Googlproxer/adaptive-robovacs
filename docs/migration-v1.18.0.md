# v1.18.0 migration

Requires Home Assistant 2026.10.0 or newer, whose schema and Repairs APIs are
used by the config flow, services and Repair flows. Existing entity and unique
IDs stay intact. New per-room number/switch entities are discovered normally.

Store schema 19 migrates schema 18 and supported older schemas while preserving
jobs, ordered occurrences, exact profiles, completion timestamps, cadence,
deferrals, duration samples, holds, faults and recovery. New defaults are:

| Setting | Default | Meaning |
| --- | --- | --- |
| Occupancy sensitivity | 0 | Future horizon is the safe robot/stage duration plus 10 minutes. Values 1–180 set that exact horizon. |
| Stable vacancy fallback | On | Continuously observed vacancy can satisfy the vacancy gate despite poor history. |
| Vacancy fallback wait | 0 | Wait the horizon; a value 1–1440 can lengthen, but never shorten, it. |

This changes automatic eligibility: a room with poor history can eventually
clear the forecast gate. Its occupied state, protected neighbours, permitted
hours, ready robot, water rules, scoped recovery, and global modes still apply.
No setting changes the safety rules for a clean already running. Strong enough
conditional history can still permit a start before the fallback deadline.

Initialization, source replacement, occupancy returning, or loss of resolved
vacancy resets the continuous-clear clock. Offline time is not counted. Existing
completed historical samples remain available unless the occupancy sources
change. Migration starts missed-window evidence empty and does not infer past
missed opportunities from dates alone.

Missed-window notifications use existing discovered mobile targets. They are
informational and contain no start action. A permitted interval must first be
observed; the default threshold is three cadence deadlines or two mop follow-up
expiries. A cause is sent once per due episode, with changes throttled for 24
hours except a new repeated-mop warning. Completing the episode or disabling
scheduling clears its tag. Preview never sends or clears notifications.

The 12-hour vacuum-to-mop limit is unchanged. Expiry counts now survive restart,
and the dashboard shows the repeated-vacuum reason along with independent
vacuum, mop and full-program timestamps. Partial progress cannot move the full
program's completion timestamp.

Before deployment, take a Home Assistant backup including this
integration's Store. An older integration will reject schema 19 in storage-safe
mode; rollback requires the matching pre-upgrade Store backup, not hand-editing
the schema number. Follow AGENTS.md's complete release, CI,
exact-tag HACS installation and idle-robot restart procedure. Verify loaded
version, settings, dashboards and observed robot state without issuing a test
clean.
