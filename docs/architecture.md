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

### Domain

`models.py`, `planner.py`, `jobs.py`, and `state.py` contain identifiers,
enums, typed values, pure scheduling rules, whole-plan allocation, lifecycle
reducers, and the schema-17 Store model. They do not import Home Assistant.
The reducers return transitions and effects; they do not call services or
mutate a coordinator.

### Application

`application.py` is the composition root, transaction router, and sole owner of
mutable `SchedulerState`. Its focused components keep that ownership boundary
without rebuilding a second god file:

- `application_events.py` translates Home Assistant callbacks into commands.
- `application_evaluation.py` owns the observe-to-plan transaction.
- `application_dispatch.py` owns occurrence preparation and the persisted
  checkpoint-before-start transaction.
- `application_recovery.py` restores checkpoints and owns recovery timers,
  while `application_jobs.py` applies pure lifecycle reducers to observed
  robot state.
- `application_room_recovery.py` shares live/startup error recovery and serialized
  Repair acknowledgement. It persists room blocks, interrupted occurrences, and
  robot/job detachment together before exposing a released robot to scheduling.
- `application_policy.py` supplies observations and pure scheduling inputs.
- `application_settings.py` owns typed configuration and floor-plan changes.
- `application_faults.py` and `application_water.py` own their scoped workflows.
- `application_actions.py` handles explicit user cleaning and return requests.

`commands.py` defines the accepted command union, while `command_queue.py`
provides serialization and shutdown draining. A command observes current
external state, reconciles durable state, plans work, saves a dispatch
checkpoint, revalidates safety, and only then asks a gateway to act.
Responses are frozen as `CommandResult` values while crossing the queue and
are converted to Home Assistant dictionaries only by service or Repair-flow
adapters.

### Infrastructure

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
- `application_legacy_map.py` handles pre-1.14 map-selection holds through a
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
schemas 1 through 16 migrate to schema 17 only after the entire payload parses
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
