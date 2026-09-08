# Architecture

Adaptive RoboVacs 2.0 is split into domain, application, infrastructure, and
presentation layers. The scheduler uses Home Assistant registries as its
configuration source and never persists transient entity IDs or native room
segments as durable robot identities.

## Data flow

```text
HA events, services, and entity actions
                |
                v
       typed SchedulerCommand values
                |
                v
       SchedulerApplication queue
                |
                v
 observe -> reconcile -> plan -> checkpoint -> revalidate -> dispatch
                |
                v
       immutable IntegrationSnapshot
                |
                v
 push-only DataUpdateCoordinator -> CoordinatorEntity platforms
```

Each config entry owns one FIFO command worker. State transitions and user
commands are never coalesced. Only explicitly marked refresh/evaluation work
with the same key may share a pending result. Recursive callbacks enqueue a new
command instead of re-entering a transaction.

## Layers

Room eligibility timestamps use the pure `next_clean_schedule` rule in `models.py`.
The projection combines cadence, initial baseline, recognised room deferrals, and
persisted occurrences into an immutable `RoomView.next_clean_at`. `room_status.py`
derives activity and per-robot preview reasons from immutable views. Occupancy and
readiness restrictions never predict a time at which they will clear.

`schedule_clock.py` owns one cancellable presentation timer for the next daily
window closing. It advances only timestamp fields in the last settled snapshot;
it does not evaluate candidates, dispatch, or write Store data. Normal application
updates replace and rearm that clock. Relative countdowns live entirely in the
existing dashboard module and do not create Home Assistant updates.

### Domain

`models.py`, `planner.py`, `jobs.py`, and `state.py` contain identifiers,
enums, typed values, pure scheduling rules, whole-plan allocation, lifecycle
reducers, and the schema-18 Store model. They do not import Home Assistant.
The reducers return transitions and effects; they do not call services or
mutate a coordinator.

### Application

The `application/` package contains the scheduler's state owner and focused
workflow components. Its small `__init__.py` exports `SchedulerApplication`,
preserving `from .application import SchedulerApplication` for integration
setup, services, and typed runtime data. Internal components import each other
within the package and use parent-relative imports for domain and infrastructure
dependencies; `application/jobs.py` orchestrates the root `jobs.py` reducers,
and `application/dispatch.py` orchestrates the root `dispatch.py` pipeline.

`application/core.py` is the composition root, transaction router, and sole owner of
mutable `SchedulerState`. Its focused components keep that ownership boundary
without rebuilding a second god file:

- `application/events.py` translates Home Assistant callbacks into commands.
- `application/evaluation.py` owns the observe-to-plan transaction.
- `application/dispatch.py` owns occurrence preparation and the persisted
  checkpoint-before-start transaction.
- `application/recovery.py` restores checkpoints and owns recovery timers,
  while `application/jobs.py` applies pure lifecycle reducers to observed
  robot state.
- `application/room_recovery.py` shares live/startup error recovery and serialized
  Repair acknowledgement. It persists room blocks, interrupted occurrences, and
  robot/job detachment together before exposing a released robot to scheduling.
- `application/policy.py` supplies observations and pure scheduling inputs.
- Adjacency policy consumes saved direct same-floor links and resolved occupancy
  through `resolve_adjacency`. It returns transient typed blockers independently
  of the target's occupancy, cadence and durable fault/recovery state. The dispatch
  pipeline rechecks it after profile and checkpoint awaits before starting work.
- `application/settings.py` owns typed configuration and floor-plan changes.
- `application/faults.py` and `application/water.py` own their scoped workflows.
- `application/actions.py` handles explicit user cleaning and return requests.

Clock and timer helpers remain in `application/core.py`. Component wrappers
resolve them through deferred imports to preserve one patchable clock without
an eager circular import. The architecture tests resolve relative import depth
and enforce dependency boundaries across every application submodule.

`commands.py` defines the accepted command union, while `command_queue.py`
provides serialization and shutdown draining. A command observes current
external state, reconciles durable state, plans work, saves a dispatch
checkpoint, revalidates safety, and only then asks a gateway to act.
Responses are frozen as `CommandResult` values while crossing the queue and
are converted to Home Assistant dictionaries only by service or Repair-flow
adapters.

### Infrastructure

- `lifecycle.py` owns one cancellable adjacency night-boundary timer. It queues
  normal evaluations at real local-time transitions, including DST, and rearms
  after settings changes. The independent presentation clock never dispatches.
- `discovery.py` reads current area, floor, device, entity, and label
  registries and produces a typed `DiscoverySnapshot`.
- `observations.py` converts current HA states to typed observations.
- `gateway.py` and `adapters/` own vacuum service calls and vendor-specific
  profile or dispatch behavior.
- `storage.py`, `notifications.py`, `repair_service.py`, and `floor_plans.py`
  expose narrow typed boundaries without a coordinator reference.
- `retired_features.py` removes only entry-owned obsolete controls and the
  retired map archive through Home Assistant APIs. No capture or vendor map
  transport remains.
- `application/legacy_map.py` handles pre-1.14 map-selection holds through a
  compatibility Repair. It validates existing HA room mappings and persists a
  scoped hold release without sending physical commands.

### Presentation

`snapshots.py` defines the frozen, equality-comparable
`IntegrationSnapshot`. `projections.py` copies settled state into sorted typed
room, robot, map, and floor-plan tuples. `coordinator.py` is a push-only
`DataUpdateCoordinator` with no polling interval; it only forwards snapshots
or a top-level update error. Platform entities extend `CoordinatorEntity` and
perform Home Assistant serialization, labels, options, date formatting, units,
and attributes at their properties.

The config entry runtime is typed as
`ConfigEntry[AdaptiveRoboVacsRuntimeData]`. Its application, coordinator, and
lifecycle are stored only in `entry.runtime_data`.

## Identity and persistence

Stable entity-registry IDs are the only durable robot keys. The current entity
ID is resolved through discovery immediately before observation or an outbound
action. A retained alias preserves existing Adaptive RoboVacs unique IDs after
a vacuum entity rename.

The scheduler Store keeps its existing key and envelope version. Internal
schemas 1 through 17 migrate to schema 18 only after the entire payload parses
and validates. An unresolved legacy identity is retained as a typed unresolved
reference, cannot dispatch, and creates a Repair. Malformed retained data or a
newer schema is never overwritten; the entry starts in storage-safe,
observe-only mode and publishes its diagnostic state.

Room recovery records are separate from mapping/profile faults and carry stable
room, robot, occurrence, stage, and episode identities. Ten continuous seconds
of fresh, docked, terminal-ready, error-free observations permit detachment;
restart settling and unsafe or missing evidence reset the interval. Repair
confirmation clears only the matching detached episode and manual bypasses.
It does not dispatch. Error-interrupted work earns no completion or duration
credit; only the retained unfinished stage becomes eligible after confirmation.

## Dispatch invariants

- Room-local occupancy blocks new scheduled work.
- Party Mode, observe-only mode, storage-safe mode, startup settling, and
  shutdown are non-dispatching.
- Every physical stage uses a fresh observation and complete revalidation.
- An active-job checkpoint is saved and published before an outbound start.
- Robot observations override timing estimates during normal operation and
  recovery.
- An uncertain start outcome is held for explicit recovery and is never
  retried automatically.
- Public errors use stable safe codes; logs retain contextual diagnostics.
- A failed room remains eligible for a later attempt after its scoped fault is
  resolved.

Manual room actions bypass scheduler policy gates—cadence, windows, occupancy,
configured enablement, battery thresholds, scheduler holds, and the
legacy global halt—but still require a physically docked compatible same-floor
robot, valid mapping/profile/preflight, any required water approval, start
confirmation, and every non-bypassable global mode.

## Boundary enforcement

`tests/test_architecture.py` parses imports and the application composition with
the Python AST. It prevents HA dependencies in domain modules,
infrastructure-to-coordinator back-references, platform access to mutable state
or storage, policy or I/O imports in the coordinator, and regression of the
focused application responsibilities back into the composition root.
Behavioral tests cover public contracts instead of incidental source text.
