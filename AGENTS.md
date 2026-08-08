# Adaptive RoboVacs agent guide

## Scope and architecture

- This repository contains one Home Assistant custom integration:
  `custom_components/adaptive_robovacs`.
- The scheduler is registry-driven. Do not hard-code live entity IDs, room
  names, floor IDs, map segment IDs, device IDs, or credentials.
- Preserve the safety model: room occupancy blocks new work; Party Mode and
  observe-only mode never dispatch; bedroom-transit areas have stricter rules.
- Durable scheduler state is stored through Home Assistant's `Store`. Changes
  to jobs, cadence, duration learning, or recovery must retain restart-safe
  behaviour and keep the robot's observed state authoritative over estimates.

## Implementation rules

- Keep scheduling decisions pure where practical in `models.py`, then add or
  update a unit test in `tests/test_models.py`.
- Dispatch failures must be logged with diagnostic context but must not expose
  raw integration errors to dashboard users or permanently exclude a room from
  future scheduling attempts.
- Keep the integration-served dashboard card and the standalone copy identical:
  `custom_components/adaptive_robovacs/frontend/adaptive-robovacs-dashboard.js`
  and `dashboard/adaptive-robovacs-dashboard.js`.
- Preserve the public entity IDs and stable unique IDs unless all Home
  Assistant consumers have been audited and migrated.
- Do not add real Home Assistant data, native map details, addresses, tokens,
  or other local runtime data to the repository.

## Validation

Run both checks before committing integration changes:

```powershell
python -m unittest discover -s tests -v
Get-ChildItem custom_components\adaptive_robovacs\*.py | ForEach-Object { python -m py_compile $_.FullName }
```

The GitHub Actions workflow runs the same checks. Keep its official actions on
their current compatible major versions and grant it only the permissions it
needs.

## Releases and deployment

- Integration changes require a semantic `manifest.json` version bump, a
  matching annotated `vX.Y.Z` tag, and a full GitHub Release. HACS consumes the
  release tag, not a raw commit.
- Run the full validation suite before tagging a release.
- HACS deployment and a Home Assistant restart are required only when the
  installed custom integration or its served frontend changes. Confirm both
  vacuums are not cleaning before a restart.
- Repository-only changes such as GitHub Actions, documentation, or
  `AGENTS.md` are committed and pushed normally, with no HACS download,
  Home Assistant restart, manifest version bump, tag, or GitHub Release.
- The HACS listing uses the repository-root `icon.png`; it must remain byte
  identical to `custom_components/adaptive_robovacs/brand/icon.png`.

## Git hygiene

- Inspect `git status` and the targeted diff before staging; preserve unrelated
  user changes.
- Use focused commits with descriptive messages. The established project flow
  publishes approved changes directly to `main`.
