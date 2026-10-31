# Performance benchmarks

The benchmark runs the real integration code on the development computer using
an isolated Home Assistant and generated registries. It never connects to a
running Home Assistant, installs an integration, dispatches a vacuum, or changes
household settings. All HA service calls are guarded and fail the benchmark.
No instrumentation is installed on the server.

## Run locally

Use the Python environment pinned in `requirements_test.txt`. That file explains
the two-phase pytest installation needed by the HA test plugin. For example, in
PowerShell from the repository root:

```powershell
$testPython = '.venv\Scripts\python.exe'
& $testPython -m benchmarks --samples 25 --warmup 5 `
  --output test-results/performance/backend.json `
  --profile test-results/performance/profiles
```

The optional browser benchmark adds Playwright, matching the repository's
existing JavaScript Playwright version:

```powershell
& $testPython -m pip install -r requirements_benchmark.txt
& $testPython -m playwright install chromium
& $testPython -m benchmarks.dashboard --samples 50 --warmup 5 `
  --output test-results/performance/dashboard.json
```

Use `--scenario small` for a quick run, or `--help` for all options. Run timing
measurements sequentially on an otherwise quiet computer. Warmups, fixture
construction, allocation tracing, and CPU profiling are excluded from timing
samples. Garbage collection remains enabled. For more reliable tail estimates,
increase `--samples` to 100 or more and repeat the run.

The initial toolchain check on 2026-10-31 found Home Assistant stable 2026.10.4
and current compatible repository pins. The local environment was refreshed to
those pins. CI uses Python 3.14.9; reports record the actual Python, HA, browser,
platform, architecture, commit, and working-tree status used for each run.
On Windows, run pytest with `-p no:homeassistant`: the plugin's automatic patch
module imports Linux-only `fcntl`. The suite's explicit isolated Home Assistant
context tests still run. Linux CI runs the ordinary plugin-enabled suite.

## Workloads

| Case | Rooms | Robots | Registry entities | Vacancy samples per room |
| --- | ---: | ---: | ---: | ---: |
| small | 12 | 2 | 2,000 | 28 |
| medium | 48 | 4 | 8,000 | 112 |
| large | 96 | 8 | 20,000 | 336 |

Generated cases use two floors, same-floor adjacency links, twelve duration
samples per room, and one occupied room in four. Rooms are due, robots are
ready, and desired windows are bypassed so the benchmark exercises actual
candidate planning. Histories and clocks are deterministic. Invariant checks
reject empty candidate/assignment workloads, unwanted dispatch, failed event
filtering, unnecessary unchanged-state writes, and broken command coalescing.

Backend workloads measure:

- A queued preview evaluation, including observation, planning, opportunity
  accounting, durable-fingerprint checks, snapshot construction, and publication.
- Registry discovery, snapshot construction, and a one-room snapshot delta.
- State validation/serialization plus JSON encoding, restart decoding, and an
  unchanged save attempt that must skip the Store write.
- A burst of 1,000 unwatched or irrelevant attribute-only state events.
- 100 simultaneous preview requests that must coalesce into one evaluation.

Each row reports median, nearest-rank p95, maximum, mean, median process CPU
time, and a separate per-invocation Python allocation peak. A continuous
event-loop heartbeat measures the maximum stall over three additional calls,
including work inside the command worker. Allocation peaks are temporary
allocations, not retained-memory or leak measurements. CPU profiles and their
top 30 cumulative functions come from an additional evaluation per case.

The browser workload mounts the shipped floorplan cards in headless Chromium.
It measures unrelated updates, occupancy changes, and 100 updates coalesced
within a frame. Input creation is outside the timer. Reported CPU cost includes
the `hass` setters, queued refresh/model/index/render work, and forced layout;
frame latency is recorded separately. Unrelated changes must cause zero renders,
changed updates must render once per frame, and room DOM identity must survive.
The fixture includes background integration controls to exercise the shared
entity index. HA's outer card wrapper is a stub, so this does not measure full
Lovelace, child cards, Recorder, WebSocket transport, disk latency, network
latency, or physical vacuum actions. Background fixture preparation and garbage
collection can influence burst tails; confirm a browser hotspot with repeated
runs before optimizing it.

## A private case based on a running setup

Both runners accept `--setup <private-json-path>` instead of `--scenario`.
Keep that input and all household-derived reports **outside the repository**, even
when identifiers have been replaced. The benchmark itself remains offline.

The input is an anonymized setup description produced from read-only HA entity
and registry snapshots. It contains numbered floors/rooms/robots, settings,
sensor states, floorplan geometry/edges, native profile option enums, and
retained sample counts. It contains no entity/registry/device IDs, room names,
native map or segment details, addresses, tokens, or credentials. A local
capture can be refreshed through the Home Assistant connector's read tools;
this runner deliberately has no live-server or credential option.

The public entity API exposes history counts rather than individual history
samples. This case reconstructs sample timestamps and durations locally while
retaining the captured per-room counts. It is a representation of the setup,
not an exact replay of the durable Store. Active jobs, recovery checkpoints,
faults, battery-learning histories, and real historical vacancy distributions
are not reconstructed. All captures force observe-only mode and use a memory
Store. The live system is queried only for existing configuration/state/counters;
profiling and synthetic evaluations take place on the development computer.

```powershell
# Point this at a private capture outside the checkout.
$benchmarkCase = 'C:\private-benchmarks\local-setup.json'
& $testPython -m benchmarks --setup $benchmarkCase --samples 25 --warmup 5 `
  --output C:/private-benchmarks/backend.json `
  --profile C:/private-benchmarks/profiles
& $testPython -m benchmarks.dashboard --setup $benchmarkCase --samples 50 `
  --output C:/private-benchmarks/dashboard.json
```

Backend output includes `local_setup`, retaining current cadence and windows,
and `local_due`, making enabled rooms overdue, bypassing desired windows, and
giving currently unoccupied rooms at least two hours of clear time to exercise
planning. Occupancy, adjacency, robot policy, and history counts remain in
place. A blocked current setup is valid and need not produce assignments.
The dashboard uses only `local_setup`, since the geometry and index workload are
identical for the due variant. Inspect `benchmarks/setup_case.py` and the
`synthetic_application` loader for the accepted schema; do not commit private
captures as fixtures.

## Budgets and regression checks

| Workload | Advisory p95 budget |
| --- | ---: |
| Preview evaluation | 50 ms |
| Registry discovery / restart decoding | 50 ms |
| Snapshot build / encode JSON / unchanged save | 25 ms |
| One-room snapshot delta | 5 ms |
| 1,000 noisy events | 10 ms |
| 100 coalesced evaluation requests | 75 ms |
| Dashboard update or burst CPU | 16.7 ms |

These are investigation triggers, not guarantees for Home Assistant hardware.
CI publishes warnings but does not fail on timing noise from shared runners.
Behavioural invariant failures always fail the workflow. On a controlled runner,
append `--check` to fail when a budget is exceeded.

To compare changes, save a baseline, then use the same machine/environment,
case, and sample settings:

```powershell
& $testPython -m benchmarks --scenario small --samples 100 `
  --output test-results/performance/before.json
# Make the change, then repeat:
& $testPython -m benchmarks --scenario small --samples 100 `
  --baseline test-results/performance/before.json `
  --output test-results/performance/after.json --check
```

Comparisons reject incompatible suites, environments, and dimensions. They
flag p95 increases greater than both 25% and 1 ms. Avoid comparing timings from
different computers even when their metadata matches. If the fixture or
measurement definitions change, create a new baseline and increment the report
schema when comparisons would otherwise appear compatible.

## Investigating a warning

Start with the JSON rows and CPU profile text. A slow unchanged-save row points
to work performed before the write is skipped. A slow evaluation with quick
snapshot/discovery rows points toward planning/forecasting or persistence
checks. Profile cumulative times include nested calls and profiling overhead;
do not add them together or substitute them for uninstrumented timings.

Existing integration diagnostics expose bounded evaluation/discovery timings,
queue depth/coalescing, Store writes/skips, snapshot publications/skips, and
entity writes/skips. A single read of these counters can check whether an
offline hotspot is relevant to the running system without profiling the server.
They are historical operational counters, not a controlled live benchmark.

The `Performance benchmark` GitHub workflow runs on relevant pushes/PRs and
supports manual dispatch. It retains JSON, profile text/binary files, and a
synthetic screenshot for 30 days with read-only repository permissions. Ordinary
validation still runs independently. This tooling changes no installed code
and needs no integration version bump, HACS deployment, or HA restart.
