# Plan: Robot defaults and per-room cleaning profiles

## Goal

Let every robot define its default cleaning behavior and let each room inherit
or override that behavior. Resolve one complete, capability-compatible profile
for the chosen robot and reapply it before every stage so settings from one
room or stage cannot leak into the next.

## Confirmed product model

**Power level** means the vacuum's advertised fan speed. Vacuum cards own robot
defaults. Room cards own optional overrides.

The profile fields are:

- cleaning program: **Vacuum only**, **Mop only**, **Vacuum then mop**, or
  **Mop then vacuum**;
- fan speed;
- cleaning mode;
- mop mode;
- mop intensity;
- vacuum pass count; and
- mop pass count.

The cleaning program replaces the separate vacuum/mop cadence decision. Each
room has one schedule and one cadence; its effective program defines the ordered
stages attempted at every occurrence. A room offers **Robot default** for every
overridable field. Maintenance or administrative controls such as child lock,
do-not-disturb, or dust-bin actions are not room profile fields.

## v1.4.4 baseline and remaining gap

The v1.4 release line implements the shared foundations originally coordinated with
plan 3: one cadence, ordered cleaning programs, robot program defaults, room
program overrides, independent vacuum/mop pass defaults and overrides,
operation-specific adapter capabilities, durable stages, water readiness, and
program compatibility Repairs.

The remaining work is the broader cleaning profile: nullable per-room fan-speed,
cleaning-mode, mop-mode, and mop-intensity overrides; typed whole-profile
resolution; adapter-owned validation/application; and guaranteed reset of every
applicable value before each stage. Runtime profile application still reads
those fields from robot settings, so two rooms cannot yet request distinct raw
vendor profile values safely.

The review release changes the implementation boundary for this remaining
work. Robot settings and duration samples are now keyed by stable vacuum
entity-registry IDs, while current vacuum entity IDs are transient service-call
aliases. Final vacancy eligibility is calculated after assignment from that
robot's operation/pass-specific history. Schema-v6 Store decoding is typed and
strict, all integration-owned asynchronous work is gated during unload, and
dashboard state is derived in `projections.py`. The profile implementation must
extend those boundaries without reintroducing entity-ID-keyed settings,
pre-assignment pooled forecasts, untracked service-call tasks, or direct entity
reads of mutable Store dictionaries.

Both inspected robots advertise fan speed but with different option sets.
Their other options also differ. Every vendor-provided option remains an exact
raw Home Assistant value associated with the robots that advertise it; there
is no hard-coded common list or semantic translation.

v1.4.4 also exposes **Not configured** truthfully for supported but unset robot
profile fields and prefers same-device registry translation metadata when
classifying cleaning/mop selectors. Plan 5 must preserve both behaviors: an
unset value is not an implicit vendor default, and option-text heuristics remain
a fallback rather than the primary capability contract.

## Ownership and resolution

- Every robot stores explicit defaults for its supported profile fields,
  including one cleaning program. An unset supported field is visibly **Not
  configured**; the UI must not display the first advertised option as though
  it were saved.
- Persist those defaults under the robot's stable registry ID. Resolve the
  current entity ID only at profile-application/observation time, and retain
  the v1.4.4 entity alias solely to preserve existing Adaptive RoboVacs unique
  IDs and public entities across vacuum renames.
- Every room stores nullable overrides. Null means **Robot default** and is
  resolved only after candidate assignment identifies a robot.
- Program options are normalized scheduler semantics rather than raw vendor
  strings. Expand them to `[vacuum]`, `[mop]`, `[vacuum, mop]`, or
  `[mop, vacuum]`. An ordered program always remains separate stages; a native
  simultaneous operation is not semantically equivalent.
- Resolve vacuum and mop pass counts independently. Each stage carries only its
  operation's resolved count, allowing one/two, two/one, or two/two without
  changing the room's single cadence or ordered program.
- Before a room may save or use an exact-value override, at least one eligible
  robot must advertise that value. Before inheritance may be used, the chosen
  robot must have an explicit default for that supported field so a later job
  can restore it instead of inheriting the previous room's state.
- Resolve every field for every dispatched stage and reapply the relevant
  values in stable order. Do not apply only values that differ from the
  scheduler's last estimate.
- Assignment filters to robots that support the full program and resolved
  profile before battery/readiness ranking. Never partially translate or
  silently substitute an unsupported value. Transient no-water readiness or a
  required per-stage user attestation is not profile incompatibility; plan 3
  resolves it only when mop is current.
- Once a compatible robot is chosen, calculate forecast eligibility with its
  registry-keyed duration samples for the exact operation and pass count.
  Repeat current window, occupancy/adjacency/transit, readiness, capability,
  and profile checks before preparation and physical dispatch; the room-level
  estimate must never be the final approval to start.
- The robot chosen for an occurrence supplies all inherited defaults and remains
  the sequence's robot. It is not reserved between stages, but a later stage is
  not reassigned to a robot with different defaults. Removal or capability
  regression becomes a visible compatibility wait/Repair.

## Adapter profile contract

This plan builds on the adapter schema and ordered-stage evolution delivered by
the water-aware cleaning-program release. It must extend that contract rather
than adding vendor branches to orchestration.

- Keep the existing normalized option sets and operation-specific pass counts
  in `AdapterCapabilities`. Add typed
  `RequestedCleaningProfile`, `ResolvedCleaningProfile`, `CleaningProgram`,
  and `CleaningStage` values.
- Add adapter methods to validate a resolved stage profile without side effects
  and to apply it without starting a clean. The generic adapter implements
  standard Home Assistant fan-speed and discovered select actions. A vendor
  adapter may override or extend application while preserving the same
  normalized result.
- Keep application and dispatch separate. Applying a profile performs no area
  clean or native segment command; dispatch receives the current resolved stage
  and the occurrence fingerprint.
- Adapters return stable result codes and never expose native targets or raw
  exceptions. Roborock may use native repeat-2 dispatch for a stage requesting
  two passes, but shared runtime must not also toggle a pass control. It may not
  collapse ordered vacuum/mop stages into `vac_and_mop`.
- Record adapter ID/schema, requested/resolved profile, program, both requested
  and resolved pass defaults/overrides, stage index, and the current stage's
  pass count in the occurrence/active-stage checkpoint. Use
  `robot_registry_id` as durable identity and keep the current entity ID only
  as a runtime alias. After restart, observed robot state remains authoritative
  and no profile or command is replayed automatically.

## Defaults, migration, and leakage prevention

- Preserve existing registry-keyed v1.4.4 robot fan/mode/mop defaults, explicit
  **Not configured** values, retained entity aliases, and stable entity/unique
  IDs. New room profile overrides migrate to null (**Robot default**).
- Reinterpret the existing robot `double_pass` default and room `pass_count`
  override as the vacuum-stage pass settings, preserving their entity/unique
  IDs and effective behavior with clearer vacuum-specific labels. Add a new
  robot mop-pass default initialized to one and a nullable room mop-pass
  override initialized to **Robot default**. Upgrading therefore never
  unexpectedly doubles mopping.
- Treat the robot cleaning-program, single cadence, and independent pass
  settings delivered by plan 3 as authoritative. The new migration adds only
  the remaining nullable room profile fields and must not re-run or reinterpret
  the completed v1.4 migration.
- The legacy robot `mopping_enabled` setting is replaced rather than retained
  as a second source of truth. **Vacuum only** is the safe default; an explicit
  legacy mop intent is reconciled to **Vacuum then mop** where the adapter
  verifies scheduler mopping. Authoritative water telemetry enables automatic
  readiness; otherwise plan 3 requires explicit per-stage confirmation.
- Reapply a complete applicable profile before each stage. Vacuum stages do not
  apply mop-only fields. Mop stages apply the resolved mop settings. Shared
  fields such as fan speed and cleaning mode follow adapter-declared relevance;
  skipped water stages perform no profile service calls.
- Vacuum and mop pass choices are each **Robot default**, **1 pass**, or
  **2-pass cross-hatch** on room cards. Vacuum cards expose independent robot
  defaults. Hide a stage's control when the effective program cannot contain
  that operation; retain its saved value so changing programs does not destroy
  user configuration.
- Adapter support is operation-specific. A robot advertising vacuum two-pass
  but not mop two-pass can run double vacuum/single mop but is incompatible
  with a double-mop request. Never infer mop repeat support solely from vacuum
  repeat support.
- If eligible robots differ, room option lists show the live union for editing,
  while assignment still enforces the complete exact profile. Presentation may
  show supporting robot names but persistence stores raw values and registry
  identities only.
- Extend the typed Store codec from the schema current at implementation time.
  Historical migrations may preserve stale exact vendor values for diagnosis;
  malformed current-schema structures/types must enter storage safe mode rather
  than being coerced to a working profile.

## Mopping, carpet, and sequence interaction

- Cleaning-program and mop-mode/intensity controls appear only when a floor has
  an adapter that verifies scheduler mopping. Telemetry-backed robots use the
  authoritative water contract; verified mop-capable robots without telemetry
  show **Water confirmation required** and use plan 3's explicit per-stage
  confirmation. **Mop only** and both ordered programs remain incompatible with
  a truly vacuum-only robot.
- Carpet is a stronger room exclusion for mop stages. A carpeted room may use
  **Vacuum only**; a saved program containing mop is an actionable
  configuration incompatibility, not a transient water skip.
- Water readiness is checked when the mop stage is actually next and again in
  its final adapter preflight. For **Vacuum then mop**, do not pre-skip mop from
  the initial water state: water becoming ready during the vacuum stage allows
  mop to run. Water still unavailable at the telemetry-backed mop preflight
  skips that stage. A no-sensor robot requires an unexpired explicit user
  confirmation instead. Both paths leave vacuum stages eligible regardless of
  ordering.
- Completed stages remain completed. Occupancy, adjacency, transit, battery, or
  window changes between stages persist the remaining sequence and return it
  through normal candidate evaluation.
- Profile/program/capability changes during a pending occurrence invalidate its
  unresolved fingerprint safely. Never apply a newly edited profile to only
  the remaining half of an old occurrence; close or re-plan it through an
  explicit deterministic state transition and require any bedroom approval
  again.

## Failure behavior

Profile service calls are not atomic. Apply the complete current-stage profile
before adapter dispatch and checkpoint the phase. If any call raises, times
out, or returns an error:

- do not send that stage's clean command or advance the occurrence cadence;
- do not start any later stage;
- log safe robot, room, adapter, stage, profile-field, and requested-value
  context;
- retain no native target or raw exception in Store/entities/Repairs;
- engage the existing durable system-wide scheduler halt with
  `profile_apply_failed`; and
- use the scheduler-failure Repair/global recheck flow to validate current
  entities/options without issuing a test clean before explicit resume.

The next attempt after resume reapplies the complete pending-stage profile. It
never trusts a partially changed robot and never replays a completed stage.
Water absence is handled before profile application as a normal skip and never
enters this failure path.

Every application task must be config-entry-owned and check the coordinator's
closing state immediately before each Home Assistant profile service call and
before clean dispatch. Unload cancels or drains the work; it must never leave a
partially scheduled callback capable of changing a robot after the entry is
gone. New compatibility Repair families must carry stable entry/room/robot
registry context so config-entry removal can enumerate and delete them without
live discovery.

## Dashboard experience

- Every vacuum card shows the robot cleaning-program default plus configured,
  current-observation, and compatibility status for its other defaults.
- Every room card shows one cleaning cadence and gains **Robot default** profile
  selects. It presents vacuum passes and mop passes as distinct controls.
  Program choices are capability-filtered normalized labels; vendor fields use
  the exact live union for eligible robots on that floor.
- Room status shows the resolved robot, effective program, ordered/current
  stage, inherited fields, pending water-confirmation deadline, water-skipped or
  unconfirmed stage, and compatibility reasons. It does not retain separate
  vacuum/mop due rows, repeat the room name on every row, or display another
  room's controls.
- Dynamic capability/friendly-name changes update option membership and labels
  through the existing discovery signal. Both JavaScript copies remain
  byte-identical.
- Entity/card state comes from safe `projections.py` output and coordinator
  accessors. The retained robot entity alias is used only for stable unique IDs,
  never as the durable profile-settings key.

## Implementation plan

1. Add typed requested/resolved profiles and pure default-resolution,
   stage-relevance, fingerprint, and whole-program compatibility helpers in
   `models.py`, reusing the delivered cleaning-program/stage and
   operation-specific pass models.
2. Preserve the v1.4 robot program/pass defaults and room program/pass
   overrides. Extend typed `RoomSettings` only with nullable fan-speed,
   cleaning-mode, mop-mode, and mop-intensity overrides, using an explicit,
   idempotent Store migration without altering v1.4 occurrence semantics or
   registry-keyed `RobotSettings` identity.
3. Extend the versioned adapter contract with stage-profile validation and
   application. Implement standard Home Assistant actions in the generic
   adapter and make Roborock delegate unless a vendor-specific override is
   required.
4. Refactor candidate/assignment construction to resolve one whole program and
   complete profile per robot, discard incompatible robots before ranking, and
   calculate final vacancy eligibility from that robot's registry-keyed exact
   operation/pass duration before ranking. Carry the chosen typed fingerprint
   and registry ID through preview, occurrence creation, and every stage.
5. Refactor runtime profile application into the adapter boundary. Apply only
   the current stage's relevant cleaning mode, fan speed, mop mode/intensity,
   and resolved operation-specific non-native pass controls in deterministic
   order; repeat capability and just-in-time water preflight before
   checkpoint/application. Route work through config-entry task tracking and
   repeat the closing check before every service call and dispatch.
6. Reuse the system fault latch for actual application errors. Add separate
   auto-clearing compatibility Repairs for missing defaults, saved unsupported
   programs/values, carpet/mop conflicts, or capability regression. Make their
   issue IDs/data enumerable during config-entry removal without `runtime_data`.
7. Add stable robot-default and room-override controls/status roles to the
   selected cards without replacing unaffected public entity IDs or unique IDs.
   Remove/migrate only the superseded dual-cadence and mopping-enable surfaces.
8. Store program, requested/resolved profile, both operation pass resolutions,
   adapter version, stable robot registry ID, stage index, and terminal stage
   outcomes in occurrence/active-stage checkpoints. Project only safe display
   data; keep current entity IDs transient and robot observations authoritative
   during recovery.
9. Put room/vacuum compatibility and effective-profile status in
   `projections.py`, then document normalized program semantics, exact-value
   compatibility, required defaults, one cadence, water skipping, pass
   handling, and Repair/resume behavior.

## Validation

- Test all robot-default and room-override program combinations, including
  exact ordered expansion and rejection when a robot cannot support the whole
  program.
- Test standard generic fan-speed/select calls and vendor delegation without a
  clean command during profile application.
- Test consecutive rooms with different speeds followed by a **Robot default**
  room; every stage receives an explicit resolved applicable value.
- Test missing robot defaults, heterogeneous robots, exact raw values,
  whole-profile filtering, carpet conflicts, capability regression, and
  capability-aware controls.
- Test an entity rename with an unchanged vacuum registry entry: robot defaults,
  room resolution, occurrence fingerprints, adaptive entity IDs, and history
  remain attached once, while profile calls use the new current entity ID.
- Test fast and slow compatible robots on the same floor. Each candidate uses
  its own exact operation/pass duration before assignment, and all final safety
  gates are repeated with the selected robot's duration.
- Test every independent pass combination across one- and two-stage programs,
  especially double vacuum/single mop and single vacuum/double mop. Test
  operation-specific capability rejection, Roborock native repeat-2 behavior,
  separate duration keys, and prevention of ordered-stage collapse into a
  combined native operation.
- Test no water in both stage orders: mop is skipped only when it becomes the
  current stage, vacuum remains eligible, skipped mop profile calls are not
  made, and the occurrence advances only under plan 3's terminal-outcome rules.
  When water becomes ready during a preceding vacuum, the configured mop pass
  count must run.
- Test no-sensor robots independently from unsupported robots: verified mopping
  remains program-compatible, but no mop profile or command may be applied
  without plan 3's current unexpired explicit confirmation.
- Test profile edits, occupancy waits, restart, and robot capability changes
  between stages without leakage or replay.
- Test unload while profile application is queued, while it owns the
  coordinator lock, and between two profile service calls. No later profile or
  clean action may run after closing begins.
- Test compatibility Repairs auto-clear, while an actual stage-profile
  application failure aborts the occurrence and durably halts all scheduler
  work until explicit successful recheck/resume.
- Test Store/entity migration, dynamic room/vacuum card membership, stable
  unique IDs, strict current-schema rejection/storage safe mode, translation
  placeholders, Repair cleanup without runtime data, and dashboard-copy
  equality.
- Run the complete repository tests and compile every integration Python
  module.

## Acceptance criteria

- A robot owns one explicit cleaning-program/profile default and every room can
  inherit or override it without creating a second cadence.
- Vacuum and mop pass counts resolve independently from robot defaults and room
  overrides, and each is validated against operation-specific adapter support.
- Two rooms can reliably request different profiles on the same robot, and a
  following inherited room restores the complete robot default.
- Vacuum entity renames preserve profile ownership and public Adaptive RoboVacs
  entities because durable settings/fingerprints use registry identity.
- Ordered programs remain ordered physical stages, and every remaining stage
  re-enters normal safety evaluation.
- An incompatible profile blocks before dispatch and is actionable without
  creating a false global failure; transient no-water skips only mopping.
- Any failure while applying an attempted stage profile prevents that and all
  later cleans and uses the existing durable system-wide halt/Repair workflow.
- Future vendors can validate and apply the same normalized profiles/stages
  through the adapter contract without scheduler changes.
- Unload and malformed Store data fail closed without a late profile call,
  dispatch, silent default substitution, or overwrite of saved user state.
