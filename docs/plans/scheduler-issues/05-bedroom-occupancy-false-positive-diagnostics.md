# Plan: Diagnose Bedroom 3 vacancy rejections without weakening safety

## Observed behaviour

Bedroom 3 is substantially past its 48-hour cadence. Its current state is
unoccupied and its configured 10:00–17:00 window accommodates the learned
cleaning duration, yet it has missed earlier eligible windows. The retained
history shows frequent occupancy transitions, which may be genuine use or
false positives.

## Goal

Determine why Bedroom 3's safe vacancies are rejected and correct only a
proven false-positive or forecast defect. A person-present signal must remain
an immediate block; this work must not weaken bedroom or bedroom-transit
safety.

## Design

1. Add safe per-room eligibility diagnostics: current occupancy source,
   unoccupied-since time, required clear minutes, forecast confidence,
   comparable-sample count, and the final rejection reason.
2. Persist a bounded audit of scheduler decisions for overdue rooms. It should
   contain safe timestamps, room labels/IDs already owned by the integration,
   source category, and reason code, never raw radar payloads or personal
   presence details.
3. Separate three cases: immediate occupied result, insufficient contiguous
   vacancy, and a historical forecast rejection despite a long current clear
   period.
4. Only after evidence identifies a false source, add a source-specific
   correction such as a short clear-state debounce or stale-source timeout.
   An on/occupied event must continue to block immediately.

## Implementation steps

1. Make the vacancy forecast a typed pure result and expose its inputs and
   safe decision summary in room schedule attributes.
2. Record a capped rolling decision trail whenever a due room changes block
   reason or misses a full desired window.
3. Add dashboard diagnostics that make the cause visible without displaying
   native sensor identifiers.
4. Capture an evidence period covering multiple Bedroom 3 windows. Compare
   actual occupied intervals, clear duration, learned cleaning duration, and
   forecast threshold.
5. Choose a narrowly scoped correction only if the evidence shows a false
   positive, unstable fallback, or forecast calculation error. Otherwise
   retain the safety policy and document the legitimate occupancy pattern.

## Tests and acceptance criteria

- Unit-test immediate occupancy, a clear period shorter than required, a
  sufficiently long clear period with too little historical confidence, and a
  successfully forecast safe vacancy.
- Verify that a bedroom-transit room remains stricter than an ordinary bedroom.
- Test bounded audit persistence and restart restoration.
- Test any selected debounce so that occupied blocks immediately and only a
  confirmed clear state is delayed.

## Rollout verification

Observe diagnostics through several scheduled windows before changing a live
occupancy policy. The final verification must show either a recorded safe
assignment or an auditable reason for every rejected eligible window.
