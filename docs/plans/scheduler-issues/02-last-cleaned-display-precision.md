# Plan: Make recent last-cleaned text precise

## Observed behaviour

Office had a last-clean timestamp about 39 hours old while Home Assistant's
standard timestamp rendering displayed “2 days ago”. The underlying timestamp
is correct; the presentation loses the distinction that matters to a
48-hour cadence.

## Goal

Show elapsed time in hours while a clean is less than 48 hours old, then use
whole-day wording at 48 hours and beyond. Preserve the existing timestamp
entity, its device class, and its stable entity ID for all existing consumers.

## Design

1. Leave each room's last-cleaned sensor state as the authoritative ISO
   timestamp or unknown. Do not replace it with a formatted string.
2. Add a presentation-only, safe attribute such as last_cleaned_display.
   It should say “39 hours ago” for an age below 48 hours, “2 days ago” at
   48 hours, and “unknown” only for an absent timestamp.
3. Render that attribute as the Last cleaned row in the custom room card.
   The raw timestamp remains available in Home Assistant's normal entity
   details and to automations.
4. Refresh the rendered text at the next hour boundary even when the last
   clean itself did not change. This can be a lightweight coordinator refresh
   or client-side clock refresh; it must not write durable scheduler state.

## Implementation steps

1. Add a pure elapsed-time formatter in models.py with an injected current
   time. Define explicit boundaries for 0, 1, 47, 48, and multi-day hours.
2. Project the formatted value from the room state into the timestamp
   sensor's attributes without changing native_value.
3. Change the custom dashboard room row from the default timestamp renderer
   to the presentation attribute. Retain a conventional timestamp entity for
   external dashboards.
4. Ensure the custom card recomputes or receives an update at each hour
   boundary, including after a browser reconnect.
5. Apply the same JavaScript change to the served and standalone copies.

## Tests and acceptance criteria

- Unit-test the formatter at 47 hours 59 minutes, exactly 48 hours, and a
  missing timestamp.
- Test that the last-cleaned entity still has timestamp device class and
  unchanged native state.
- Extend dashboard tests to assert that the room card uses the display
  attribute and that the two JavaScript copies are identical.
- Confirm a 39-hour Office-style fixture displays “39 hours ago”, never
  “2 days ago”.

## Rollout verification

Verify both the integration dashboard card and the standalone card with recent
and older timestamp fixtures. No Home Assistant restart is needed for a
repository-only prototype, but a released served-frontend change follows the
normal HACS and restart procedure.
