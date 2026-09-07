# Plan: Application package refactor

## Status and baseline

Planned on 2026-09-07 against the clean working tree at `a777365`.
Implementation, patch release v1.14.1, and HACS code installation were authorized
on 2026-09-08. The user explicitly requested no Home Assistant restart; package
activation and post-restart verification are deferred until a later restart.
See the [v1.14.1 release notes](../releases/v1.14.1.md) for validation and
deployment scope. The steps below record the implementation plan.

Implemented and locally validated for v1.14.1 on 2026-09-08: 416 Python tests,
360 subtests, 96.11% coverage, and all required lint, typing, compilation,
dashboard, and file-parity checks passed. Release and code installation follow
the no-restart scope above.

At planning time, the integration had `application.py` and twelve
`application_*.py` component modules. A local `v1.14.0` tag existed; recheck
repository and release state before implementation or release work.

## Objective and scope

Move the application layer into an `application` package, drop the
`application_` filename prefix, and preserve the public `SchedulerApplication`
import. Update affected imports, test infrastructure, and documentation.

Preserve scheduling behavior, state ownership, command ordering, mixin
inheritance order, storage serialization, public entity IDs, and stable unique
IDs. This structural refactor requires no additional storage migration.

## 1. Establish the implementation baseline

Recheck Git status, branch, and recent commits before editing. Preserve any
changes that appear after this plan was written.

Run the existing validation suite before the move and record any failures.
Use that baseline to distinguish relocation problems from existing issues.

Keep the change focused on application module locations, affected imports,
test infrastructure, and documentation. Preserve registry-driven discovery,
occupancy blocking, Party Mode, observe-only mode, restart-safe state, and
observed robot authority throughout the refactor.

## 2. Create the application package

Create this structure beneath `custom_components/adaptive_robovacs`:

```text
application/
├── __init__.py
├── core.py
├── actions.py
├── dispatch.py
├── evaluation.py
├── events.py
├── faults.py
├── jobs.py
├── legacy_map.py
├── policy.py
├── recovery.py
├── room_recovery.py
├── settings.py
└── water.py
```

Move the existing `application.py` into `application/core.py`. Move each
`application_<name>.py` into the package as `<name>.py`, including the recently
added legacy-map component.

Keep existing class names, including `SchedulerApplication` and the
`Application...Mixin` classes. Preserve the exact order of bases on
`SchedulerApplication`, because that controls method resolution.

Make `application/__init__.py` a small public entry point:

```python
"""Application layer for Adaptive RoboVacs."""

__all__ = ["SchedulerApplication"]

from .core import SchedulerApplication
```

Place `__all__` after the module docstring and any `from __future__` imports,
but before ordinary imports, following
[PEP 8's module-level dunder guidance](https://peps.python.org/pep-0008/#module-level-dunder-names).
The lowercase package and module names, readable underscores in module names,
and CapWords class names follow
[PEP 8's naming guidance](https://peps.python.org/pep-0008/#package-and-module-names).

This preserves the existing import used by integration setup, services,
runtime data, and most tests:

```python
from .application import SchedulerApplication
```

Remove the original source files as part of the moves. Do not leave
compatibility wrappers for the old internal module names; those would defeat
the requested folder cleanup.

## 3. Update imports and shared helpers

Update imports according to the dependency's location:

| Dependency | Import from inside the new package |
| --- | --- |
| Another application component | `from .policy import ApplicationPolicyMixin` |
| Domain models | `from ..models import ...` |
| Existing dispatch infrastructure | `from ..dispatch import DispatchPipeline` |
| Existing pure job reducers | `from ..jobs import ...` |
| Vendor adapters | `from ..adapters.roborock import ...` |

Pay particular attention to `dispatch.py` and `jobs.py`: both names will exist
inside the application package and at the integration root, with different
responsibilities.

Keep shared clock and timer helpers in `core.py` for this refactor. Components
currently import these helpers lazily from `application.py`; retarget those
deferred imports to `core`:

```python
def _now() -> datetime:
    from . import core

    return core._now()
```

Apply the same approach to local-time conversion and timer registration.
Retaining deferred imports avoids introducing an eager cycle between
`core.py` and the mixins it imports.

Treat these deferred imports as a deliberate exception to the usual
top-of-file import rule, retained to preserve the existing dependency behavior.
Keep other imports at module scope, grouped as standard library, third party,
then local imports. Explicit relative imports are acceptable under
[PEP 8's import guidance](https://peps.python.org/pep-0008/#imports); the scoped
exception follows its
[guidance on justified departures and project consistency](https://peps.python.org/pep-0008/#a-foolish-consistency-is-the-hobgoblin-of-little-minds).

Keep `commands.py`, `command_queue.py`, domain modules, and infrastructure
modules in their current locations. Their movement is unnecessary for the
requested cleanup.

## 4. Retarget tests to the actual implementation modules

Preserve public `SchedulerApplication` imports through the package. Update
direct component imports, including `matching_occurrence` in the
room-recovery tests.

Retarget mock paths using these rules. The leading `...` below represents
`custom_components.adaptive_robovacs.`:

| Existing target | New target |
| --- | --- |
| `...application._now` | `...application.core._now` |
| `...application._track_point` | `...application.core._track_point` |
| `...application.async_discover` | `...application.core.async_discover` |
| `...application.async_dispatcher_send` | `...application.core.async_dispatcher_send` |
| `...application.build_snapshot` | `...application.core.build_snapshot` |
| `...application_policy._local` | `...application.policy._local` |
| `...application_recovery.<name>` | `...application.recovery.<name>` |
| Other `...application_<component>.<name>` targets | `...application.<component>.<name>` |

Also update patches of `async_track_point_in_utc_time`, including targets
split across adjacent string literals.

Patching the package's re-exported attributes would not necessarily affect
globals used inside `core.py`. Tests must patch the module where the
implementation looks up each dependency.

Keep existing test filenames and behavioral assertions. Review the
application, water-confirmation, room-recovery, and Home Assistant integration
tests together.

## 5. Adapt the architecture checks

Update [test_architecture.py](../../tests/test_architecture.py) so the package
move preserves meaningful boundary enforcement.

Change the composition-root inspection to read `application/core.py`.
Replace the `application_*.py` glob with recursive discovery beneath the
application directory, and assert that discovery found modules so an empty
search cannot silently pass.

Update forbidden-import checks to reject the application package and all its
submodules from coordinator and entity-platform code. Normalize relative and
absolute imports so these equivalent forms are covered:

```python
from .application.core import SchedulerApplication
from . import application
import custom_components.adaptive_robovacs.application.core
```

Likewise, ensure application modules cannot import the coordinator through
their new relative depth.

Retain checks for focused component ownership and typed runtime data. Add
focused coverage for the changed boundary-checking logic so forbidden nested
imports demonstrably fail.

## 6. Update documentation and compilation guidance

Update [architecture.md](../architecture.md) with the new package structure,
public entry point, and component paths. Include the legacy-map component's
new location.

Update the compilation instruction in [AGENTS.md](../../AGENTS.md) to cover
nested Python files while retaining the required root-module check. The
preferred documented recursive command, run from the repository root, is:

```powershell
python -m compileall -q custom_components/adaptive_robovacs
```

The [CI workflow](../../.github/workflows/test.yml) already uses recursive
compilation. Ruff, mypy, and coverage are also configured against directories,
so the package move should not require new path exclusions.

Add a concise changelog/release-note entry explaining that the application
layer was reorganized without requiring an additional storage migration.
Preserve historical release descriptions that document earlier layouts.

## 7. Validate imports, behavior, and the final diff

Start with import collection and the affected tests. Verify that the public
package import resolves to the same `SchedulerApplication` class defined in
`core.py`, and that application components import successfully in a fresh
process.

Run the complete repository validation:

| Check | Required result |
| --- | --- |
| Ruff formatting and lint | Pass |
| Strict mypy | Pass |
| Full pytest suite and coverage | Pass; at least the existing 95% threshold |
| Required unittest discovery | Pass |
| Root-module compilation and recursive compilation | Pass |
| Dashboard tests and shipped-file parity | Pass |

The current required unittest and root-module compilation commands, run from
the repository root, are:

```powershell
python -m unittest discover -s tests -v
Get-ChildItem custom_components\adaptive_robovacs\*.py | ForEach-Object { python -m py_compile $_.FullName }
```

Run recursive compilation as well to cover the new package. Use the project's
test environment and the CI-equivalent checks defined in the workflow.

Use the existing behavioral tests to verify startup and shutdown, discovery
publication, dispatch checkpoints, recovery timers, room recovery, water
confirmation, and persisted state handling.

Search production code, tests, and current documentation for stale module
paths. Remaining matches should be deliberate historical references or checks
that obsolete files are absent. This plan also intentionally records the old
names as implementation mappings.

Review the diff with rename detection. Confirm that implementation bodies
changed only where imports or module references required it, that inheritance
order is identical, and that no unrelated files were reformatted.

## 8. Prepare the release when release work is authorized

Before committing, perform the repository-required check of running Home
Assistant Core and available stable updates, and reconcile compatible
dependency and CI pins.

A local `v1.14.0` tag existed when this plan was written. Verify remote tags
and GitHub release state before selecting the next unused semantic version;
preserve existing tags. Update the manifest, project version, and matching
release documentation consistently.

Follow the repository release procedure: focused commit to `main`, push,
annotated tag, full GitHub Release, and successful CI for the released commit.

For an approved deployment, install the exact tag through HACS, validate
configuration, confirm both vacuums are not cleaning, restart Home Assistant,
and verify the integration loads. Confirm the installed package contains the
new application directory and that observed robot states and scheduler safety
holds remain authoritative.

For this execution, the user's no-restart instruction overrides the restart
and activation steps: install the exact tag, validate configuration, verify
HACS's installed version, and leave the existing runtime loaded. Report that
the new package awaits a later restart; do not reload the integration either.

Implementation completion requires a working public import, no obsolete
application source files, passing validation, and no additional changes to
entity IDs or durable storage format. Release and deployment completion also
require the repository's final Git, tag, CI, installed-version, and loaded-state
checks.
