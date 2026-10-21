# Migrating to v1.19.0

Version 1.19.0 adds managed cleaning runs and battery-use learning.

Store schema 19 migrates to 20 automatically through Home Assistant's `Store`.
Existing room history, active jobs, configured programs, floor plans, faults,
recoveries, and stable entity identities are preserved. New battery learning and
run reservations start empty; no manual storage edits are required. Invalid or
newer payloads retain the existing storage-safe, observe-only behavior.

Initially the scheduler continues single-room cleaning while learning battery
use. Each exact robot/operation/profile needs three valid observed stage samples.
Changing a profile can require additional learning. Native-app cleans and
inferred or interrupted completions do not train the new model.

When learning is available, the configured minimum battery gates the beginning
of a managed run. Later rooms may start below that threshold when observed charge
covers remaining work plus the 20% reserve. The scheduler budgets every admitted
room's complete configured program, with a 25% consumption margin. It can shorten
a run after higher-than-expected use; unfinished rooms remain due for recharge.

Programs execute one room at a time, including vacuum/mop stages in their saved
order. Once the current room is observed cleaning, its own occupancy changes
allow it to finish that program. Occupied or unresolved pending rooms are removed
from the run. Water, adjacency, windows, readiness, faults, Party Mode, and
observe-only gates still apply. An expired mop prerequisite ends the old run
permission before repeating its vacuum stage.

Public entity IDs and unique IDs are unchanged. Both copies of the dashboard
module include queue and battery-learning rows. After approved activation, reload
the dashboard resource using `?v=1.19.0` to refresh the frontend.

For an approved deployment, follow the repository release procedure: validated
commit, annotated `v1.19.0` tag, full GitHub Release, successful CI, exact HACS
installation, configuration check, and restart with both robots safe. No test
clean or hold clearance is part of this migration.
