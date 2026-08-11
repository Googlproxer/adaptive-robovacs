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

### Agent release procedure

Use this sequence for an approved integration release. Repository-only changes
stop after the normal commit and push; they do not use the version, tag,
release, HACS, or restart steps.

1. Inspect `git status`, the targeted diff, and recent tags. Preserve unrelated
   working-tree changes and stage only the files belonging to the release.
2. Run the required unit and compilation checks above, plus any relevant
   frontend checks. Confirm the two dashboard JavaScript copies are byte
   identical when either one changes.
3. Bump `custom_components/adaptive_robovacs/manifest.json` to the intended
   semantic version and update release/migration documentation. The tag must be
   the same version prefixed with `v`.
4. Commit the focused change directly to `main`, then push `main`:

   ```powershell
   git add -- <release-files>
   git commit -m "<focused description>"
   git push origin main
   ```

5. Create and push an annotated tag from the released commit:

   ```powershell
   git tag -a vX.Y.Z -m "Adaptive RoboVacs vX.Y.Z"
   git push origin vX.Y.Z
   ```

6. Create a full GitHub Release for that existing tag. Prefer `gh` or the
   GitHub connector over browser automation, and include user-visible changes,
   migrations, validation results, and deployment notes in the release body:

   ```powershell
   gh release create vX.Y.Z --verify-tag --title "Adaptive RoboVacs vX.Y.Z" --notes-file <release-notes-file>
   ```

   Check `gh auth status` first. If the default GitHub CLI token is invalid but
   HTTPS Git operations already work, obtain the existing credential through
   `git credential fill`, assign only its `password=` value to the
   process-scoped `GH_TOKEN`, run `gh` inside `try`, and remove `GH_TOKEN` in
   `finally`. Never print, persist, commit, or include that credential in tool
   output. If no existing credential is available, stop and ask the user to
   authenticate instead of inventing another path.
7. Verify the GitHub Actions run for the released commit succeeds. Do not
   deploy a failing release.
8. Install the exact tag through HACS using the Home Assistant integration
   tools, then validate Home Assistant's configuration. Confirm both vacuums
   are not cleaning before restart unless the user explicitly authorizes an
   active-clean restart.
9. Restart Home Assistant and wait for the integration to report `loaded`.
   Confirm HACS shows the new installed version, verify the robots' observed
   states remain authoritative, and inspect the new/changed entities and
   dashboard. Use the live browser only for visual UI verification; use Home
   Assistant tools for state and service checks.
10. Finish by checking that `main` matches `origin/main`, the release tag points
    at `HEAD`, and the worktree contains no unintentional changes. Report the
    commit, tag, release URL, CI result, deployment result, and any deliberately
    preserved user changes.

## Git hygiene

- Inspect `git status` and the targeted diff before staging; preserve unrelated
  user changes.
- Use focused commits with descriptive messages. The established project flow
  publishes approved changes directly to `main`.
