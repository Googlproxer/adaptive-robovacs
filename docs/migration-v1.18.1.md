# v1.18.1 migration

Requires Home Assistant 2026.10.0 or newer. Store schema remains 19. This patch
does not change entity IDs, unique IDs, floor-plan payloads, jobs, cleaning
eligibility, cadence, faults or recovery. Saved geometry and links remain intact.
When upgrading from before v1.18.0, first read its
[schema 19 migration guide](migration-v1.18.0.md).

Install the exact `v1.18.1` release through HACS after its CI checks pass,
validate configuration, then restart while both vacuums are docked and
error-free. Update the existing dashboard module resource to
`/api/adaptive_robovacs/frontend/adaptive-robovacs-dashboard.js?v=1.18.1` and reload
the dashboard. Existing clients need the changed URL to fetch the new code.

View mode now permits normal touch scrolling and browser pinch zoom across the
diagram. Edit mode reserves gestures only on room bodies, resize handles, sensor
markers and connection dots; blank canvas and surrounding controls stay
scrollable. Canceled drags restore their starting geometry. Save retains its
original revision check and applies a complete draft atomically.

For a rollback from this patch to v1.18.0, install that exact release, restart
safely, and restore its frontend resource version. No Store conversion is needed
between those two versions. Rolling back to a release before v1.18.0 still
requires the matching pre-schema-19 Store backup.
