# Scheduler issue remediation plans

These plans are derived from the 31 August 2026 live Home Assistant
investigation. They are deliberately independent so each can be reviewed,
implemented, tested, and released on its own.

| Plan | Problem addressed |
| --- | --- |
| [01 — LEGO Room allocation](01-lego-room-sheila-allocation.md) | An eligible LEGO Room clean was left unassigned although the floor's vacuum reported ready. |
| [02 — Last-cleaned precision](02-last-cleaned-display-precision.md) | The dashboard rounds a sub-48-hour age up to “2 days ago”. |
| [03 — Unknown last-clean baseline](03-unknown-last-clean-baseline.md) | Rooms without history need a restart-safe initial cadence anchor without exposing a fictitious last-clean time. |
| [04 — Vacuum despite no water](04-vacuum-when-water-is-unavailable.md) | A no-water mop outcome must never prevent the vacuum stage of a scheduled occurrence. |
| [05 — Bedroom occupancy diagnostics](05-bedroom-occupancy-false-positive-diagnostics.md) | Bedroom 3 needs evidence-driven diagnosis of missed safe vacancies without weakening occupancy safety blindly. |
| [06 — Floor-cancellation cadence drift](06-cancellation-deferral-cadence-drift.md) | A physical cancellation currently moves unrelated rooms on the same floor past their configured cadence. |

All plans preserve the integration's safety model, use registry-driven discovery,
and require the normal unit, compilation, frontend-copy, release, and deployment
checks described in AGENTS.md.
