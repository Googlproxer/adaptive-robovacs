# Performance benchmarks

The benchmark runs the real integration code on the development computer using
an isolated Home Assistant and generated registries. It never connects to a
running Home Assistant, installs an integration, dispatches a vacuum, or changes
household settings. All HA service calls are guarded and fail the benchmark.
No instrumentation is installed on the server.

## Run locally

Use the Python environment pinned in `requirements_test.txt`. Starting with
v1.20.3 it targets Core 2026.11.0 directly, using the small repository-owned
runtime fixture in `tests/ha_runtime.py`. Install the requirements normally;
no external HA pytest plugin or dependency overrides are needed. For example,
in PowerShell from the repository root:

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
those pins. CI then used Python 3.14.9; reports record the actual Python, HA, browser,
platform, architecture, commit, and working-tree status used for each run.
The 2026-11-06 check found Core 2026.11.0 running and in the official stable
feed. Requirements and CI were refreshed to that Core version, Python 3.14.9, and Ruff
0.17.4. The earlier fixture plugin pinned an older Core and is no longer used;
ordinary pytest works on Windows and Linux. Core registries and config entries
remain real, while registry persistence is disabled in the isolated fixture.
The integration's own Store contracts exercise real atomic disk writes.

## Active-cleaning benchmark

### Cleaning session occupancy v1.22.0

On 2026-11-09, v1.22.0 was compared with v1.21.1 (`ae9c44f`) on the
same Windows computer, Python 3.14.10, Core 2026.11.0 and pinned native libraries.
CI now also uses Python 3.14.10. Runs were sequential, with 100 samples and ten
warmups. Both versions used the same benchmark harness and retained identical
synthetic sample counts after any one-time learning migration. The workload has
17 rooms, two robots, 4,000 entities and 800 samples per room.

| Workload | v1.21.1 p95 ms | v1.22.0 p95 ms | Change |
| --- | ---: | ---: | ---: |
| Active evaluation | 76.990 | 76.663 | -0.4% |
| 16 sequential timer updates | 233.446 | 228.202 | -2.2% |
| Transit location heartbeat | 0.048 | 0.056 | +0.007 ms |
| Transit pulse checkpoint | 0.119 | 0.146 | +0.027 ms |

The existing cleaning budgets and matched regression checks pass. Active
evaluation medians were 33.831 / 34.316 ms; timer-batch medians were
210.687 / 209.094 ms. Small microsecond-scale differences should not be treated
as predictions of live server CPU use.

The new active-session run measured heartbeat median / p95 0.041 / 0.051 ms
and pulse checkpoint 0.107 / 0.145 ms. Heartbeats added zero evaluations, Store
saves or publications; robot pulses preserved the vacancy anchor and completed
no training samples. Timer-progress cache assertions continue to require zero
decodes of unchanged occupancy history. The new session benchmark runs in the
performance workflow alongside the existing transit benchmark:

```powershell
& $testPython -m benchmarks.occupancy --cleaning-sessions `
  --samples 100 --warmup 10 --check `
  --output test-results/performance/cleaning-sessions.json
```

Local matched reports are retained under `test-results/performance/` as
`v1.21.1-cleaning.json`, `v1.22.0-cleaning.json`, `v1.21.1-occupancy.json`,
`v1.22.0-occupancy.json` and `v1.22.0-cleaning-sessions.json`. They contain only
generated data. The broader historical results below retain their original
environment and scope.

### Robot occupancy attribution: v1.21.0

The v1.21.1 activation correction uses the native Home-trait listener rather than
the map-content trait, which has no listener API in python-roborock 7.12.0.
Cached subscription replay and unchanged frame objects never refresh location
freshness. The native boundary adds only an object-identity comparison and uses
the existing native updates. Tests now exercise the actual pinned native traits.

After that correction, the repeated matched 100-sample cleaning comparison
(ten warmups, same v1.20.3 baseline/environment below) measured median / p95
35.72 / 58.05 ms for active evaluation and 203.41 / 254.84 ms for 16 timer
updates. Budgets and regression checks pass. The 100-sample attribution run
(five warmups) measured heartbeat median / p95 0.039 / 0.046 ms and pulse
checkpoint 0.106 / 0.115 ms, with zero added heartbeat evaluations, saves or
publications. Full local validation passes 691 Python tests with 95.51% coverage;
dashboard/browser, typing, formatting, compilation and parity checks also pass.

A matched 100-sample comparison on 2026-11-06 (ten warmups, Windows,
Python 3.14.9, Core 2026.11.0) used frozen v1.20.3 production source and the
v1.21.0 working tree with the same unchanged cleaning benchmark and fixture:

| Workload | v1.20.3 median / p95 | v1.21.0 median / p95 |
| --- | ---: | ---: |
| Active evaluation | 38.23 / 79.11 ms | 36.67 / 68.34 ms |
| 16 sequential timer updates | 211.35 / 243.60 ms | 203.16 / 250.92 ms |

Both p95 budgets and baseline regression checks pass. The timer batch retained
zero evaluations, 16 Store writes, zero publications, and zero decodes of
unchanged occupancy samples. Its p95 increased 3.0%; this result preserves the
v1.20.3 persistence improvement without establishing a live server CPU saving.

The separate attribution benchmark uses 17 rooms, two robots, 4,000 entities
and 800 vacancy samples per room. A 100-sample run with five warmups measured:

| Normalized input | Median | p95 |
| --- | ---: | ---: |
| Same-room location heartbeat | 0.039 ms | 0.048 ms |
| Robot pulse checkpoint | 0.105 ms | 0.115 ms |

Heartbeat invariants require zero additional evaluations, Store saves or
publications. Pulse invariants require the original vacancy anchor and learned
samples to remain unchanged. These timings cover FIFO input handling and
checkpoint preparation; they exclude native map parsing, device I/O, full
schedule evaluation and disk latency. Meaningful room changes still request
normal evaluation and persistence. No added polling or native refresh is used.
Visits are bounded, transit routes are cached, and one deadline timer handles
classification and traffic expiry.

```powershell
& $testPython -m benchmarks.occupancy --samples 100 --warmup 5 `
  --output .venv/robot-occupancy-evidence/occupancy.json --check
```

The complete backend suite (25 samples, five warmups) passed all behavioural
invariants. Small and medium cases met every advisory budget. The 96-room large
case still exceeded budgets for evaluation, snapshot building, encoding,
restoration, unchanged saves and coalesced requests; their p95 values were
174.35, 25.57, 136.79, 126.71, 74.72 and 183.99 ms respectively. These are
absolute workload limits, not a matched before/after attribution comparison.
The Chromium dashboard suite (50 samples, five warmups) passed its invariants
and all CPU budgets; the largest p95 was 7.10 ms for a 100-update large-case burst.

Final local validation passed 689 pytest tests with 95.51% combined branch/line
coverage, the same 689 unittest tests, Ruff formatting/lint, strict mypy,
top-level and recursive compilation, 45 dashboard unit tests and ten browser
regressions across Chromium, Android emulation and WebKit (two desktop touch
tests intentionally skipped). Dashboard and icon copies remain byte-identical.
No live deployment or device acceptance is included in these results.

### Persistence follow-up: v1.20.3

A matched 50-sample comparison (five warmups, Python 3.14.9, Core 2026.11.0)
used unchanged v1.20.2 production source and the optimized source with the same
isolated runtime fixture and generated data. Median / nearest-rank p95 times:

| Workload | v1.20.2 | v1.20.3 | Median reduction |
| --- | ---: | ---: | ---: |
| Active evaluation | 62.6 / 106.4 ms | 33.3 / 72.9 ms | 46.7% |
| 16 sequential timer updates | 501.1 / 543.5 ms | 189.2 / 218.8 ms | 62.2% |

Both cleaning p95 budgets pass. The separate, untimed history-cache verification
retained 16 Store writes and decoded zero unchanged occupancy samples. Each
timer update must independently produce a checkpoint; a scheduled full
evaluation can add work and writes beyond those 16 required checkpoints.

The application caches immutable occupancy wire values, their validation
evidence, and history fingerprints. Every save receives detached containers.
Value-based keys detect in-place edits, replacement, timezone/DST-fold changes,
pruning and clearing. All other schema and cross-record checks still run, and
startup/JSON loads retain full validation. Actual checkpoints use atomic Store
writes and executor JSON encoding; write failures and cancelled writes must not
advance the saved fingerprint or overtake newer checkpoints.

These timings include production preparation and validation using the memory
Store. They exclude filesystem latency, executor scheduling, Recorder, devices
and clients. Real-Store contract tests independently check atomic persistence,
off-loop encoding, failed writes, retry, cancellation ordering and full restore.
The measurements do not establish a percentage reduction in live server CPU.

### Workload and command

This fixed synthetic workload adds 17 rooms, two robots, 4,000 registry entities,
and 800 generated vacancy samples per room, all within the retention window.
It contains no captured household settings, native map data, or history. One
observed manual job is already cleaning; the second robot is docked.

```powershell
$testPython = '.venv\validation\Scripts\python.exe'
& $testPython -m benchmarks.cleaning --samples 30 --warmup 5 `
  --output .venv/clean-performance-evidence/cleaning.json
```

The first row measures an active evaluation. The second awaits 16 timer events
separately, advancing the clock one second per event. This represents sustained
traffic rather than same-turn coalescing. Both rows run the actual command queue,
observation, policy, checkpointing, snapshot, and publication code. Every progress
batch must retain the active job and write a checkpoint with its latest observation.
The report also records evaluation, Store-write, and publication counts from the
last batch. Allocation tracing and heartbeat calls are separate from timed samples.

All service calls are prohibited. The memory Store includes production-equivalent
validation but excludes disk/executor latency. No Home Assistant entity platforms,
Recorder, WebSocket clients, or native devices are mounted; their costs are excluded.
Results describe local integration work, not a predicted percentage of VM CPU.

Run the same command before and after a change with the same environment, then
add `--baseline <before-json>` for comparison. Keep local evidence outside
Playwright's default `test-results` directory: `npm run test:browser` clears that
directory when it starts. The performance CI runs this benchmark independently
and retains its report with the other performance artifacts.

On 2026-11-05, sequential runs on the same Windows computer with Python 3.14.9
and Core 2026.10.4 compared unchanged v1.20.1 source with the v1.20.2 implementation
(30 samples, five warmups):

| Workload | Before median / p95 | After median / p95 | Median reduction |
| --- | ---: | ---: | ---: |
| Active evaluation | 127.5 / 201.5 ms | 63.9 / 116.2 ms | 49.9% |
| 16 sequential timer updates | 2,289.1 / 2,410.7 ms | 567.1 / 622.2 ms | 75.2% |

The last measured timer batch changed from 16 evaluations, 17 Store writes,
and 16 snapshot publications to zero evaluations, 16 Store writes, and zero
publications. A batch crossing the 30-second evaluation boundary can still
evaluate and publish. The write count deliberately retains restart-safe progress
checkpoints. The timer batch's p95 remains above its advisory 500 ms budget;
this is a measured improvement, not a claim that every performance target is met.

The existing backend suite (25 samples, five warmups) reduced median preview
evaluation time from 6.46 to 4.77 ms for small, 70.04 to 32.42 ms for medium, and
606.04 to 138.40 ms for large. Large unchanged-save comparison fell from 75.52
to 42.94 ms. Two initial medium snapshot/restore p95 comparison warnings did not
reproduce in a paired 100-sample run; their medians remained close to baseline.
The large scenario still exceeds several advisory budgets, including evaluation,
encoding/restoration, unchanged-save comparison, and coalesced requests.

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
