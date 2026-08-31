# Plan: Always run scheduled vacuum stages when water is unavailable

## Observed behaviour

Rob reports a persistent water shortage. Several vacuum-then-mop rooms show a
terminal skipped_no_water outcome. The intended policy is correct: a skipped
mop is acceptable, but it must not suppress or defer the regular vacuum stage.

## Goal

For a scheduled program containing vacuum and mop, water readiness is checked
only when the mop stage reaches the front of the occurrence. Vacuum starts and
completes normally despite water shortage. A no-water mop is terminal only for
that mop stage, and the next cadence is based on the completed occurrence.

## Design

1. Preserve stage ordering. For vacuum-then-mop, dispatch vacuum without
   consulting water readiness; re-run normal safety and water preflight only
   after the observed vacuum completion.
2. On a no-water mop result, record skipped_no_water, notify as already
   designed, and close the occurrence if no later stage remains. Do not create
   a room fault, scheduler halt, or retry loop.
3. Ensure a vacuum-only fallback is used when an adapter cannot apply a mop
   profile, rather than letting mop controls invalidate the preceding vacuum
   profile.
4. Record separate last_vacuum and last_mop values for diagnostics, while
   retaining the public single last-cleaned semantic for a terminal scheduled
   occurrence.

## Implementation steps

1. Trace the complete live path from candidate creation through occurrence
   preparation, profile application, dispatch, completion, and stage skip.
   Identify whether the observed rooms skipped only mop or failed before
   vacuum dispatch.
2. Make the operation argument explicit at every water gate and assert that
   none runs for a vacuum stage.
3. Verify the dispatch adapter receives a vacuum-only profile for vacuum
   stages, without mop route, intensity, or water options.
4. After a mop skip, schedule a fresh evaluation for any remaining stage and
   close the occurrence only when all stages are terminal.
5. Improve room diagnostics to show “vacuum completed; mop skipped for water”
   rather than a generic skipped outcome.

## Tests and acceptance criteria

- A vacuum-then-mop fixture with blocked water dispatches vacuum once.
- After observed vacuum completion, blocked water marks only mop skipped and
  advances cadence without creating a global or room fault.
- A mop-only program with blocked water sends no clean command.
- A water-ready fixture still runs both stages in order.
- A profile-control failure affecting only mopping leaves vacuum eligible.
- Run the existing water-mopping contract tests plus focused lifecycle tests.

## Rollout verification

With a water-shortage state present, inspect the scheduler preview and
occurrence diagnostics only. Verify that a due vacuum-then-mop room is offered
a vacuum stage; do not force a clean while the user has not authorised it.
