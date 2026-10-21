import assert from "node:assert/strict";
import test from "node:test";


class MockElement {
  constructor() {
    this.style = {};
    this.listeners = new Map();
    this.events = [];
  }
  attachShadow() { return new MockElement(); }
  append(...children) { this.children = children; }
  setAttribute(name, value) { this[name] = value; }
  addEventListener(name, listener) { this.listeners.set(name, listener); }
  dispatchEvent(event) { this.events.push(event); }
  replaceChildren(...children) {
    this.children = children;
  }
}

const elements = new Map();
const visibilityListeners = new Map();
const animationFrames = new Map();
let nextAnimationFrame = 1;
let visibilityState = "visible";
let helperLoads = 0;
const createdCards = [];

globalThis.HTMLElement = MockElement;
globalThis.customElements = {
  define(name, constructor) {
    elements.set(name, constructor);
  },
  get(name) {
    return elements.get(name);
  },
};
globalThis.document = {
  createElement() { return new MockElement(); },
  addEventListener(type, listener) {
    visibilityListeners.set(type, listener);
  },
  get visibilityState() {
    return visibilityState;
  },
};
globalThis.window = {
  customCards: [],
  requestAnimationFrame(callback) {
    const frame = nextAnimationFrame++;
    animationFrames.set(frame, callback);
    return frame;
  },
  cancelAnimationFrame(frame) {
    animationFrames.delete(frame);
  },
  loadCardHelpers: async () => {
    helperLoads += 1;
    return {
      createCardElement: async (configuration) => {
        const card = {
          configuration,
          hassWrites: [],
          getCardSize: () => 4,
          set hass(hass) {
            this.hassWrites.push(hass);
          },
        };
        createdCards.push(card);
        return card;
      },
    };
  },
};

await import("../custom_components/adaptive_robovacs/frontend/adaptive-robovacs-dashboard.js");

const GlobalCard = elements.get("adaptive-robovacs-global");
const VacuumCard = elements.get("adaptive-robovacs-vacuum");
const RoomCard = elements.get("adaptive-robovacs-room");
const TimestampRow = elements.get("adaptive-robovacs-timestamp-row");
const StatusRow = elements.get("adaptive-robovacs-status-row");
const EvidenceRow = elements.get("adaptive-robovacs-evidence-row");
const FloorPlanCard = elements.get("adaptive-robovacs-floorplan");

const entry = "entry-one";
const adaptiveState = (role, attributes = {}, state = "ready") => ({
  state,
  attributes: {
    adaptive_robovacs_entry_id: entry,
    adaptive_robovacs_role: role,
    ...attributes,
  },
});

const baseStates = () => ({
  "sensor.scheduler": adaptiveState("scheduler_status"),
  "button.resume": adaptiveState("fault_resume_control"),
  "number.forecast": adaptiveState("global_control"),
  "select.window": adaptiveState("global_control"),
  "button.preview": adaptiveState("scheduler_control"),
  "sensor.robot_one_status": adaptiveState("robot_status", {
    robot_entity_id: "vacuum.robot_one",
    friendly_name: "Robot One status",
  }),
  "switch.robot_one_enabled": adaptiveState("robot_control", {
    robot_entity_id: "vacuum.robot_one",
    friendly_name: "Robot One enabled",
  }),
  "button.robot_one_stop_return": adaptiveState("robot_stop_return_control", {
    robot_entity_id: "vacuum.robot_one",
    friendly_name: "Robot One stop and return to dock",
  }),
  "sensor.robot_two_status": adaptiveState("robot_status", {
    robot_entity_id: "vacuum.robot_two",
    friendly_name: "Robot Two status",
  }),
  "switch.robot_two_enabled": adaptiveState("robot_control", {
    robot_entity_id: "vacuum.robot_two",
    friendly_name: "Robot Two enabled",
  }),
  "button.robot_two_stop_return": adaptiveState("robot_stop_return_control", {
    robot_entity_id: "vacuum.robot_two",
    friendly_name: "Robot Two stop and return to dock",
  }),
  "sensor.kitchen_next_clean": adaptiveState("room_schedule", {
    area_id: "kitchen",
    room: "Kitchen",
    floor_id: "ground",
    bedroom: false,
    friendly_name: "Kitchen next clean",
  }),
  "sensor.kitchen_last_cleaned": adaptiveState("room_last_cleaned", {
    area_id: "kitchen",
    friendly_name: "Kitchen last cleaned",
  }),
  "sensor.kitchen_occupancy": adaptiveState("room_occupancy", {
    area_id: "kitchen",
    friendly_name: "Kitchen occupancy",
  }),
  "sensor.kitchen_manual": adaptiveState("room_manual_status", {
    area_id: "kitchen",
    friendly_name: "Kitchen manual request",
  }),
  "select.kitchen_cleaning_period": adaptiveState("room_cleaning_period_control", {
    area_id: "kitchen",
    friendly_name: "Kitchen cleaning period",
  }, "Custom"),
  "select.kitchen_cleaning_profile": adaptiveState("room_cleaning_profile_control", {
    area_id: "kitchen",
    friendly_name: "Kitchen cleaning profile",
  }, "Robot default"),
  "select.kitchen_desired_start": adaptiveState("room_window_start_control", {
    area_id: "kitchen",
    friendly_name: "Kitchen desired cleaning start",
  }),
  "select.kitchen_desired_end": adaptiveState("room_window_end_control", {
    area_id: "kitchen",
    friendly_name: "Kitchen desired cleaning end",
  }),
  "select.kitchen_passes": adaptiveState("room_pass_count_control", {
    area_id: "kitchen",
    friendly_name: "Kitchen cleaning passes",
  }),
  "select.kitchen_mop_passes": adaptiveState("room_mop_pass_count_control", {
    area_id: "kitchen",
    friendly_name: "Kitchen mop passes",
  }),
  "select.kitchen_program": adaptiveState("room_cleaning_program_control", {
    area_id: "kitchen",
    friendly_name: "Kitchen cleaning program",
  }),
  "select.kitchen_fan": adaptiveState("room_fan_speed_control", {
    area_id: "kitchen",
    friendly_name: "Kitchen fan speed",
  }),
  "select.kitchen_mode": adaptiveState("room_mode_control", {
    area_id: "kitchen",
    friendly_name: "Kitchen mode",
  }),
  "select.kitchen_mop_mode": adaptiveState("room_mop_mode_control", {
    area_id: "kitchen",
    friendly_name: "Kitchen mop mode",
  }),
  "select.kitchen_mop_intensity": adaptiveState("room_mop_intensity_control", {
    area_id: "kitchen",
    friendly_name: "Kitchen mop intensity",
  }),
  "select.kitchen_depth": adaptiveState("room_cleaning_depth_control", {
    area_id: "kitchen",
    friendly_name: "Kitchen cleaning depth",
  }),
  "number.kitchen_cadence": adaptiveState("room_control", {
    area_id: "kitchen",
    friendly_name: "Kitchen cadence",
  }),
  "switch.kitchen_enabled": adaptiveState("room_enabled_control", {
    area_id: "kitchen",
    friendly_name: "Kitchen enabled",
  }),
  "switch.kitchen_ignore_desired_window": adaptiveState("room_ignore_desired_window_control", {
    area_id: "kitchen",
    friendly_name: "Kitchen ignore desired cleaning window",
  }),
  "button.kitchen_clean": adaptiveState("room_manual_clean_control", {
    area_id: "kitchen",
    friendly_name: "Kitchen manual clean",
  }),
  "button.kitchen_vacuum": adaptiveState("room_manual_vacuum_control", {
    area_id: "kitchen",
    friendly_name: "Kitchen manual vacuum only",
  }),
  "button.kitchen_mop": adaptiveState("room_manual_mop_control", {
    area_id: "kitchen",
    friendly_name: "Kitchen manual mop only",
  }),
  "sensor.bedroom_next_clean": adaptiveState("room_schedule", {
    area_id: "bedroom",
    room: "Bedroom",
    floor_id: "upper",
    bedroom: true,
  }),
  "switch.bedroom_enabled": adaptiveState("room_enabled_control", {
    area_id: "bedroom",
    friendly_name: "Bedroom enabled",
  }),
  "vacuum.robot_one": {
    state: "docked",
    attributes: { friendly_name: "Robot One" },
  },
  "vacuum.robot_two": {
    state: "docked",
    attributes: { friendly_name: "Robot Two" },
  },
});

function configure(Card, config, states = baseStates()) {
  const card = new Card();
  card.setConfig(config);
  card._hass = { states };
  return { card, configuration: card._configuration() };
}

function flushAnimationFrames() {
  const callbacks = [...animationFrames.values()];
  animationFrames.clear();
  callbacks.forEach((callback) => callback());
}

async function flushDashboard() {
  flushAnimationFrames();
  await Promise.resolve();
  await Promise.resolve();
}

function setVisibility(nextVisibility) {
  visibilityState = nextVisibility;
  visibilityListeners.get("visibilitychange")?.();
}

function mount(Card, config, states = baseStates()) {
  const card = new Card();
  card.connectedCallback();
  card.setConfig(config);
  card.hass = { states };
  return card;
}

test("registers target-scoped cards and keeps the editor private", () => {
  assert.deepEqual([...elements.keys()], [
    "adaptive-robovacs-timestamp-row",
    "adaptive-robovacs-status-row",
    "adaptive-robovacs-evidence-row",
    "adaptive-robovacs-global",
    "adaptive-robovacs-vacuum",
    "adaptive-robovacs-room",
    "adaptive-robovacs-floorplan-editor",
    "adaptive-robovacs-floorplan",
  ]);
  assert.deepEqual(
    window.customCards.map((card) => card.type),
    [
      "adaptive-robovacs-global",
      "adaptive-robovacs-vacuum",
      "adaptive-robovacs-room",
      "adaptive-robovacs-floorplan",
    ]
  );
});

test("global card includes scheduler status and controls in role order", () => {
  const { configuration } = configure(GlobalCard, {});
  assert.equal(configuration.type, "entities");
  assert.equal(configuration.title, "Scheduler");
  assert.deepEqual(configuration.entities, [
    "sensor.scheduler",
    {
      entity: "button.resume",
      name: "Recheck and resume",
      tap_action: {
        action: "perform-action",
        perform_action: "button.press",
        target: { entity_id: "button.resume" },
        confirmation: {
          text: "Recheck all scheduler holds and recoveries, release those now safe, and refresh the schedule? No test clean will be sent; normal safety checks still apply.",
        },
      },
    },
    "number.forecast",
    "select.window",
    "button.preview",
  ]);
});

test("vacuum card contains one selected vacuum and uses its friendly name", () => {
  const { configuration } = configure(VacuumCard, {
    vacuum_entity_id: "vacuum.robot_one",
  });
  assert.equal(configuration.title, "Robot One");
  assert.deepEqual(configuration.entities, [
    { entity: "sensor.robot_one_status", name: "Status" },
    { entity: "switch.robot_one_enabled", name: "Enabled" },
    { entity: "button.robot_one_stop_return", name: "Stop and return to dock" },
  ]);
  assert.ok(configuration.entities.every((row) => !row.entity.includes("robot_two")));
});

test("vacuum card presents the managed queue and battery learning", () => {
  const states = baseStates();
  Object.assign(states["sensor.robot_one_status"].attributes, {
    cleaning_run_summary: "Study → Hall; 30 min remaining; 20% use + 20% reserve",
    battery_learning_summary: "3 measurements; three per stage profile required",
  });
  const { configuration } = configure(VacuumCard, {
    vacuum_entity_id: "vacuum.robot_one",
  }, states);
  assert.ok(configuration.entities.some((row) => row.attribute === "cleaning_run_summary"));
  assert.ok(configuration.entities.some((row) => row.attribute === "battery_learning_summary"));
  assert.ok(configuration.entities.every((row) => !row.entity.includes("robot_two")));
});

test("vacuum card ignores retired map controls and never offers map actions", () => {
  const states = baseStates();
  for (const role of [
    "robot_map_capture_status", "robot_map_capture",
    "robot_map_snapshot_preview_select", "robot_map_snapshot_preview",
  ]) {
    states[`sensor.${role}`] = adaptiveState(role, {
      robot_entity_id: "vacuum.robot_one",
      map_selection_pending: true,
      available_maps: [{ map_id: "old-map", name: "Old map" }],
    });
  }
  const { configuration } = configure(VacuumCard, {
    vacuum_entity_id: "vacuum.robot_one",
  }, states);
  assert.deepEqual(configuration.entities, [
    { entity: "sensor.robot_one_status", name: "Status" },
    { entity: "switch.robot_one_enabled", name: "Enabled" },
    { entity: "button.robot_one_stop_return", name: "Stop and return to dock" },
  ]);
});

test("adjacency selector stays below occupancy independently of profile and period", () => {
  for (const period of ["Off", "Default", "Night", "Custom"]) {
    for (const profile of ["Robot default", "Custom"]) {
      const states = baseStates();
      states["select.renamed_adjacency"] = adaptiveState("room_adjacency_mode_control", {
        area_id: "kitchen", friendly_name: "Kitchen adjacency protection",
        options: ["Off", "Night only", "Always"],
      }, "Night only");
      states["select.kitchen_cleaning_period"].state = period;
      states["select.kitchen_cleaning_profile"].state = profile;
      const { configuration } = configure(RoomCard, { area_id: "kitchen" }, states);
      const rows = configuration.entities;
      const occupancy = rows.findIndex((row) => row.entity === "sensor.kitchen_occupancy");
      assert.deepEqual(rows[occupancy + 1], {
        entity: "select.renamed_adjacency", name: "Adjacency protection",
      });
      assert.equal(rows.filter((row) => row.entity === "select.renamed_adjacency").length, 1);
    }
  }
});

test("global card discovers both adjacency night controls", () => {
  const states = baseStates();
  for (const bound of ["start", "end"]) {
    states[`select.night_${bound}`] = adaptiveState("global_control", {
      friendly_name: `Adjacency night ${bound}`,
    }, bound === "start" ? "23:00" : "09:00");
  }
  const { configuration } = configure(GlobalCard, {}, states);
  const ids = configuration.entities.map((row) => typeof row === "string" ? row : row.entity);
  assert.ok(ids.includes("select.night_start"));
  assert.ok(ids.includes("select.night_end"));
});

test("room card contains one selected room with simple controls before advanced settings", () => {
  const { configuration } = configure(RoomCard, { area_id: "kitchen" });
  assert.equal(configuration.title, "Kitchen");
  assert.deepEqual(configuration.entities, [
    { entity: "sensor.kitchen_next_clean", name: "Next clean" },
    {
      type: "attribute",
      entity: "sensor.kitchen_last_cleaned",
      attribute: "last_cleaned_display",
      name: "Last cleaned",
    },
    { entity: "sensor.kitchen_occupancy", name: "Occupancy" },
    { entity: "select.kitchen_cleaning_period", name: "Cleaning period" },
    { entity: "select.kitchen_cleaning_profile", name: "Cleaning profile" },
    { entity: "select.kitchen_desired_start", name: "Desired cleaning start" },
    { entity: "select.kitchen_desired_end", name: "Desired cleaning end" },
    { entity: "number.kitchen_cadence", name: "Cadence" },
    { entity: "button.kitchen_clean", name: "Manual clean" },
    { entity: "button.kitchen_vacuum", name: "Manual vacuum only" },
    { entity: "button.kitchen_mop", name: "Manual mop only" },
  ]);
  const entityIds = configuration.entities.map((row) => row.entity);
  assert.ok(entityIds.every((entityId) => !entityId.includes("bedroom")));
  assert.equal(new Set(entityIds).size, entityIds.length);
  assert.ok(!entityIds.includes("sensor.kitchen_manual"));
  assert.ok(!entityIds.includes("select.kitchen_passes"));
  assert.ok(!entityIds.includes("select.kitchen_mop_passes"));
  assert.ok(!entityIds.includes("select.kitchen_program"));
  assert.ok(!entityIds.includes("select.kitchen_fan"));
  assert.ok(!entityIds.includes("select.kitchen_mode"));
  assert.ok(!entityIds.includes("select.kitchen_mop_mode"));
  assert.ok(!entityIds.includes("select.kitchen_mop_intensity"));
  assert.ok(!entityIds.includes("select.kitchen_depth"));
  assert.ok(!entityIds.includes("switch.kitchen_enabled"));
  assert.ok(!entityIds.includes("switch.kitchen_ignore_desired_window"));
});

test("room card exposes predicted total and required vacancy from the verified model", () => {
  const states = baseStates();
  states["sensor.kitchen_next_clean"].attributes.duration_model_version = 2;
  states["sensor.kitchen_next_clean"].attributes.predicted_total_minutes = 24;
  states["sensor.kitchen_next_clean"].attributes.required_vacancy_minutes = 28;
  const { configuration } = configure(RoomCard, { area_id: "kitchen" }, states);

  assert.deepEqual(configuration.entities.slice(0, 4), [
    { entity: "sensor.kitchen_next_clean", name: "Next clean" },
    { type: "attribute", entity: "sensor.kitchen_last_cleaned", attribute: "last_cleaned_display", name: "Last cleaned" },
    {
      type: "attribute",
      entity: "sensor.kitchen_next_clean",
      attribute: "predicted_total_minutes",
      name: "Predicted total (min)",
    },
    {
      type: "attribute",
      entity: "sensor.kitchen_next_clean",
      attribute: "required_vacancy_minutes",
      name: "Required vacancy (min)",
    },
  ]);
});

test("room card reveals profile override controls only in Custom mode", () => {
  const states = baseStates();
  states["select.kitchen_cleaning_profile"] = adaptiveState(
    "room_cleaning_profile_control",
    {
      area_id: "kitchen",
      friendly_name: "Kitchen cleaning profile",
    },
    "Custom"
  );
  const { configuration } = configure(RoomCard, { area_id: "kitchen" }, states);
  const entityIds = configuration.entities.map((row) => row.entity);
  assert.ok(entityIds.includes("select.kitchen_program"));
  assert.ok(entityIds.includes("select.kitchen_passes"));
  assert.ok(entityIds.includes("select.kitchen_mop_passes"));
  assert.ok(entityIds.includes("select.kitchen_fan"));
  assert.ok(entityIds.includes("select.kitchen_mode"));
  assert.ok(entityIds.includes("select.kitchen_mop_mode"));
  assert.ok(entityIds.includes("select.kitchen_mop_intensity"));
  assert.ok(entityIds.includes("select.kitchen_depth"));
  assert.equal(entityIds.indexOf("select.kitchen_cleaning_profile"), 4);
  assert.ok(entityIds.indexOf("select.kitchen_passes") > entityIds.indexOf("select.kitchen_cleaning_profile"));
});

test("room card hides desired-window overrides until the period is Custom", () => {
  const states = baseStates();
  states["select.kitchen_cleaning_period"] = adaptiveState(
    "room_cleaning_period_control",
    {
      area_id: "kitchen",
      friendly_name: "Kitchen cleaning period",
    },
    "Default"
  );
  const { configuration } = configure(RoomCard, { area_id: "kitchen" }, states);
  const entityIds = configuration.entities.map((row) => row.entity);
  assert.equal(entityIds.indexOf("select.kitchen_cleaning_period"), 3);
  assert.equal(entityIds.indexOf("select.kitchen_cleaning_profile"), 4);
  assert.ok(!entityIds.includes("select.kitchen_desired_start"));
  assert.ok(!entityIds.includes("select.kitchen_desired_end"));
});

test("room card retains desired-window controls when period mode is unavailable", () => {
  const states = baseStates();
  delete states["select.kitchen_cleaning_period"];
  const { configuration } = configure(RoomCard, { area_id: "kitchen" }, states);
  const entityIds = configuration.entities.map((row) => row.entity);
  assert.ok(entityIds.includes("select.kitchen_desired_start"));
  assert.ok(entityIds.includes("select.kitchen_desired_end"));
});

test("room card retains detailed profile controls when the profile mode is unavailable", () => {
  const states = baseStates();
  delete states["select.kitchen_cleaning_profile"];
  const { configuration } = configure(RoomCard, { area_id: "kitchen" }, states);
  const entityIds = configuration.entities.map((row) => row.entity);
  assert.ok(entityIds.includes("select.kitchen_passes"));
  assert.ok(entityIds.includes("select.kitchen_fan"));
  assert.ok(entityIds.includes("select.kitchen_mode"));
});

test("new target-owned controls appear without changing card configuration", () => {
  const states = baseStates();
  const initial = configure(RoomCard, { area_id: "kitchen" }, states).configuration;
  const updated = configure(RoomCard, { area_id: "kitchen" }, {
    ...states,
    "select.kitchen_profile": adaptiveState("room_control", {
      area_id: "kitchen",
      friendly_name: "Kitchen profile",
    }),
  }).configuration;
  assert.equal(updated.entities.length, initial.entities.length + 1);
  assert.deepEqual(
    updated.entities.find((row) => row.entity === "select.kitchen_profile"),
    { entity: "select.kitchen_profile", name: "Profile" }
  );
});

test("target prefixes are hidden even when the card title is overridden", () => {
  const vacuum = configure(VacuumCard, {
    vacuum_entity_id: "vacuum.robot_one",
    title: "Upstairs vacuum",
  }).configuration;
  const room = configure(RoomCard, {
    area_id: "kitchen",
    title: "Food prep",
  }).configuration;
  assert.equal(vacuum.title, "Upstairs vacuum");
  assert.deepEqual(vacuum.entities[0], {
    entity: "sensor.robot_one_status",
    name: "Status",
  });
  assert.equal(room.title, "Food prep");
  assert.deepEqual(room.entities[0], {
    entity: "sensor.kitchen_next_clean",
    name: "Next clean",
  });
});

test("target and integration mismatches render visible diagnostics", () => {
  const missingTarget = configure(VacuumCard, {}).configuration;
  assert.equal(missingTarget.type, "markdown");
  assert.match(missingTarget.content, /Select a vacuum/);

  const wrongTarget = configure(RoomCard, { area_id: "garage" }).configuration;
  assert.equal(wrongTarget.type, "markdown");
  assert.match(wrongTarget.content, /not currently discovered/);

  const states = baseStates();
  states["sensor.other_scheduler"] = {
    state: "ready",
    attributes: {
      adaptive_robovacs_entry_id: "entry-two",
      adaptive_robovacs_role: "scheduler_status",
    },
  };
  const ambiguous = configure(GlobalCard, {}, states).configuration;
  assert.equal(ambiguous.type, "markdown");
  assert.match(ambiguous.content, /Select an Adaptive RoboVacs integration entry/);
});

test("visual forms use native target selectors", () => {
  const globalForm = GlobalCard.getConfigForm();
  const vacuumForm = VacuumCard.getConfigForm();
  const roomForm = RoomCard.getConfigForm();
  assert.deepEqual(globalForm.schema[0].selector, {
    config_entry: { integration: "adaptive_robovacs" },
  });
  const vacuumField = vacuumForm.schema.find((field) => field.name === "vacuum_entity_id");
  assert.deepEqual(vacuumField.selector, { entity: { domain: "vacuum" } });
  assert.equal(vacuumField.required, true);
  const roomField = roomForm.schema.find((field) => field.name === "area_id");
  assert.deepEqual(roomField.selector, { area: {} });
  assert.equal(roomField.required, true);
});

test("friendly-name and ownership changes invalidate the render signature", () => {
  const states = baseStates();
  const { card } = configure(VacuumCard, {
    vacuum_entity_id: "vacuum.robot_one",
  }, states);
  const original = card._entitySignature();
  states["vacuum.robot_one"] = {
    ...states["vacuum.robot_one"],
    attributes: { friendly_name: "Renamed Robot" },
  };
  assert.notEqual(card._entitySignature(), original);
});

test("failure diagnostics update native rows without changing the card structure", () => {
  const states = baseStates();
  const { card } = configure(RoomCard, { area_id: "kitchen" }, states);
  const original = card._entitySignature();
  states["sensor.kitchen_next_clean"].attributes.failure_code =
    "area_mapping_missing";
  states["sensor.kitchen_next_clean"].attributes.repair_active = true;
  assert.equal(card._entitySignature(), original);
});

test("cards request full section width and validate option types", () => {
  const { card } = configure(RoomCard, { area_id: "kitchen" });
  assert.deepEqual(card.getGridOptions(), { columns: "full" });
  assert.throws(() => configure(RoomCard, { area_id: ["kitchen"] }), /must be a string/);
  assert.throws(() => configure(GlobalCard, { title: 42 }), /must be a string/);
});

test("floor plan sensors use their Home Assistant friendly name", () => {
  const card = new FloorPlanCard();
  card._hass = {
    states: {
      "binary_sensor.entry_camera_motion": {
        attributes: { friendly_name: "Entry Camera Motion" },
      },
    },
  };
  assert.equal(
    card._sensorName({ entity_id: "binary_sensor.entry_camera_motion" }),
    "Entry Camera Motion",
  );
  assert.equal(
    card._sensorName({ entity_id: "binary_sensor.removed_sensor" }),
    "binary_sensor.removed_sensor",
  );
});

test("floor plan fits the viewport height without offsetting wide-screen edits", () => {
  const card = new FloorPlanCard();
  const svg = {
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 1200, height: 600 }),
  };
  assert.match(
    FloorPlanCard.prototype._ensureDom.toString(),
    /height:calc\(100dvh - 112px\)/,
  );
  assert.deepEqual(card._point({ clientX: 360, clientY: 0 }, svg, 60), { x: 0, y: 0 });
  assert.deepEqual(card._point({ clientX: 600, clientY: 300 }, svg, 60), { x: 24, y: 30 });
  assert.deepEqual(card._point({ clientX: 840, clientY: 600 }, svg, 60), { x: 48, y: 60 });
});

test("floor plan only renders editing handles and cursors in edit mode", () => {
  const source = FloorPlanCard.prototype._render.toString();

  assert.match(source, /const editingAffordances = this\._editing \? `<circle class="link-source"/);
  assert.match(source, /style="cursor:\$\{this\._editing \? "move" : "default"\}"/);
});

test("floor plan routes connections between nearest edges on the grid", () => {
  const card = new FloorPlanCard();

  assert.deepEqual(
    card._edgePoints(
      { x: 2, y: 2, width: 4, height: 4 },
      { x: 12, y: 3, width: 4, height: 4 },
    ),
    [{ x: 6, y: 5 }, { x: 12, y: 5 }],
  );
  assert.deepEqual(
    card._edgePoints(
      { x: 0, y: 0, width: 4, height: 4 },
      { x: 11, y: 8, width: 4, height: 4 },
    ),
    [{ x: 4, y: 4 }, { x: 8, y: 8 }, { x: 11, y: 8 }],
  );
  assert.deepEqual(
    card._edgePoints(
      { x: 2, y: 2, width: 4, height: 4 },
      { x: 3, y: 12, width: 5, height: 4 },
    ),
    [{ x: 5, y: 6 }, { x: 5, y: 12 }],
  );
  assert.match(FloorPlanCard.prototype._render.toString(), /<polyline class="edge/);
});

const state = (value, attributes) => ({ state: value, attributes });
const flushFrames = flushAnimationFrames;

function floorPlanFixture() {
  const plan = { revision: 3, edges: [], floors: [{ floor_id: "ground", rooms: [{
    area_id: "room", name: "Room", rectangle: { floor_id: "ground", x: 2, y: 2, width: 12, height: 10 },
    sensors: [{ registry_id: "radar", entity_id: "binary_sensor.radar", kind: "radar", state: "inactive", marker: { area_id: "room", x: 500, y: 500 } }],
  }] }, { floor_id: "other", rooms: [] }] };
  const card = new FloorPlanCard();
  const renders = [];
  card._render = (model) => renders.push(model);
  card.setConfig({ floor_id: "ground" });
  const hass = { user: { is_admin: true }, states: {
    "sensor.scheduler": state("ready", { adaptive_robovacs_entry_id: "entry", adaptive_robovacs_role: "scheduler_status", floor_plan: plan }),
    "binary_sensor.radar": state("off", { friendly_name: "Radar" }),
  } };
  card.hass = hass;
  card.connectedCallback();
  return { card, hass, plan, renders };
}

test("floor plan coalesces updates and ignores unrelated state, revision and other-floor changes", () => {
  const { card, hass, plan, renders } = floorPlanFixture();
  flushFrames();
  assert.equal(renders.length, 1);
  const nextPlan = structuredClone(plan);
  nextPlan.revision += 1;
  nextPlan.floors[1].rooms.push({ area_id: "elsewhere", name: "Other room", sensors: [] });
  for (let i = 0; i < 20; i++) card.hass = { ...hass, states: { ...hass.states,
    "sensor.unrelated": state(String(i), {}),
    "sensor.scheduler": state("changed", { ...hass.states["sensor.scheduler"].attributes, floor_plan: nextPlan }),
  } };
  flushFrames();
  assert.equal(renders.length, 1);
  card.disconnectedCallback();
});

test("floor plan refreshes occupancy at the same revision and source friendly-name changes", () => {
  const { card, hass, plan, renders } = floorPlanFixture();
  flushFrames();
  const nextPlan = structuredClone(plan);
  nextPlan.floors[0].rooms[0].sensors[0].state = "active";
  card.hass = { ...hass, states: { ...hass.states, "sensor.scheduler": state("ready", {
    ...hass.states["sensor.scheduler"].attributes, floor_plan: nextPlan,
  }) } };
  flushFrames();
  assert.equal(renders.length, 2);
  card.hass = { ...card._hass, states: { ...card._hass.states, "binary_sensor.radar": state("off", { friendly_name: "Renamed radar" }) } };
  flushFrames();
  assert.equal(renders.length, 3);
  card.disconnectedCallback();
});

test("floor plan defers hidden updates and drops queued work on disconnect", () => {
  const { card, hass, renders } = floorPlanFixture();
  setVisibility("hidden");
  flushFrames();
  assert.equal(renders.length, 0);
  card.hass = { ...hass, user: { is_admin: false } };
  setVisibility("visible");
  flushFrames();
  assert.equal(renders.length, 1);
  card.hass = { ...hass };
  card.disconnectedCallback();
  flushFrames();
  assert.equal(renders.length, 1);
});

test("floor plan freezes the draft revision, drops it on floor change and revokes editing with admin access", () => {
  const { card, hass, plan } = floorPlanFixture();
  flushFrames();
  card._beginEditing(card._model());
  plan.revision = 9;
  card.hass = { ...hass };
  assert.equal(card._draft.revision, 3);
  card.setConfig({ floor_id: "other" });
  assert.equal(card._draft, undefined);
  flushFrames();
  card._beginEditing(card._model());
  card.hass = { ...hass, user: { is_admin: false } };
  assert.equal(card._editing, false);
  assert.equal(card._draft, undefined);
  card.disconnectedCallback();
});

test("floor plan captures every drag, ignores a second pointer and rolls back canceled geometry", () => {
  for (const kind of ["room", "resize", "sensor", "link"]) {
    const { card } = floorPlanFixture();
    flushFrames();
    const model = card._model();
    card._beginEditing(model);
    let captured;
    const svg = { setPointerCapture: (id) => { captured = id; }, hasPointerCapture: (id) => captured === id,
      releasePointerCapture: () => { captured = undefined; }, querySelector: () => undefined };
    card._setLinkPreview = () => {};
    card._point = (event) => ({ x: event.clientX, y: event.clientY });
    const dataset = kind === "sensor" ? { sensor: "radar" } : kind === "link" ? { linkSource: "room" } : { room: "room", resize: kind === "resize" ? "true" : undefined };
    const down = { button: 0, pointerId: 1, clientX: 4, clientY: 4, currentTarget: svg, target: { closest: () => ({ dataset }) } };
    const original = structuredClone(card._draft);
    card._onPointerDown(down, model);
    assert.equal(captured, 1, kind);
    card._onPointerDown({ ...down, pointerId: 2 }, model);
    card._onPointerMove({ ...down, pointerId: 2, clientX: 9 }, model);
    assert.deepEqual(card._draft, original, kind);
    card._onPointerMove({ ...down, clientX: 9, clientY: 9 }, model);
    card._onPointerUp({ ...down, pointerId: 2 }, model);
    assert.equal(card._drag.pointerId, 1, kind);
    card._cancelDrag({ pointerId: 1 });
    assert.deepEqual(card._draft, original, kind);
    assert.equal(card._drag, undefined, kind);
    assert.equal(captured, undefined, kind);
    card.disconnectedCallback();
  }
});

test("floor plan retains a failed save draft and sends its original revision exactly once", async () => {
  const { card } = floorPlanFixture();
  flushFrames();
  const model = card._model();
  card._beginEditing(model);
  const calls = [];
  let reject;
  card._hass.callService = (...args) => { calls.push(args); return new Promise((_, fail) => { reject = fail; }); };
  const saving = card._save(model);
  await card._save(model);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][2].revision, 3);
  reject(new Error("Revision changed; reload."));
  await saving;
  assert.equal(card._editing, true);
  assert.equal(card._draft.revision, 3);
  assert.equal(card._saving, false);
  card.disconnectedCallback();
});

test("hidden-tab updates defer all card work and render only the newest state on return", async () => {
  const initialCreated = createdCards.length;
  setVisibility("hidden");
  const states = baseStates();
  const card = mount(VacuumCard, { vacuum_entity_id: "vacuum.robot_one" }, states);
  card.hass = {
    states: {
      ...states,
      "vacuum.robot_one": {
        ...states["vacuum.robot_one"],
        attributes: { friendly_name: "Latest Robot Name" },
      },
    },
  };

  flushAnimationFrames();
  await Promise.resolve();
  assert.equal(createdCards.length, initialCreated);

  setVisibility("visible");
  await flushDashboard();
  assert.equal(createdCards.length, initialCreated + 1);
  assert.equal(createdCards.at(-1).configuration.title, "Latest Robot Name");
  assert.equal(card.children.length, 1);
  card.disconnectedCallback();
});

test("unrelated updates do not recreate or update a room's nested card", async () => {
  const states = baseStates();
  const card = mount(RoomCard, { area_id: "kitchen" }, states);
  await flushDashboard();
  const nativeCard = createdCards.at(-1);
  const createdBefore = createdCards.length;
  const writesBefore = nativeCard.hassWrites.length;
  card.hass = {
    states: {
      ...states,
      "sensor.unrelated": { state: "updated", attributes: {} },
    },
  };
  await flushDashboard();

  assert.equal(createdCards.length, createdBefore);
  assert.equal(nativeCard.hassWrites.length, writesBefore);
  card.disconnectedCallback();
});

test("relevant updates refresh in place and structural discovery recreates once", async () => {
  const states = baseStates();
  const card = mount(RoomCard, { area_id: "kitchen" }, states);
  await flushDashboard();
  const nativeCard = createdCards.at(-1);
  const createdBefore = createdCards.length;

  card.hass = {
    states: {
      ...states,
      "number.kitchen_cadence": {
        ...states["number.kitchen_cadence"],
        state: "4",
      },
    },
  };
  await flushDashboard();
  assert.equal(createdCards.length, createdBefore);
  assert.equal(nativeCard.hassWrites.length, 2);

  card.hass = {
    states: {
      ...states,
      "select.kitchen_extra": adaptiveState("room_control", {
        area_id: "kitchen",
        friendly_name: "Kitchen extra",
      }),
    },
  };
  await flushDashboard();
  assert.equal(createdCards.length, createdBefore + 1);
  assert.equal(
    createdCards.at(-1).configuration.entities.find((row) => row.entity === "select.kitchen_extra").name,
    "Extra"
  );
  card.disconnectedCallback();
});

test("one shared helper load services rapid renders and disconnected cards", async () => {
  const helperLoadsBefore = helperLoads;
  const createdBefore = createdCards.length;
  const states = baseStates();
  const card = mount(GlobalCard, {}, states);
  card.hass = {
    states: {
      ...states,
      "sensor.scheduler": {
        ...states["sensor.scheduler"],
        state: "newest",
      },
    },
  };
  await flushDashboard();
  assert.equal(createdCards.length, createdBefore + 1);
  assert.ok(helperLoads === helperLoadsBefore || helperLoads === helperLoadsBefore + 1);

  const detached = mount(GlobalCard, {}, states);
  flushAnimationFrames();
  detached.disconnectedCallback();
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(detached.children, undefined);
  card.disconnectedCallback();
});


test("timestamp rows retain minute rounding and unit boundaries", () => {
  const now = Date.parse("2026-09-08T00:00:00Z");
  for (const [minutes, text, delay] of [
    [0, "Due now", undefined], [-1, "Due now", undefined],
    [0.1, "in 1 minute", 6000], [1, "in 1 minute", 60000],
    [1.01, "in 2 minutes", 600], [59, "in 59 minutes", 60000],
    [59.01, "in 1 hour", 600], [60, "in 1 hour", 60000],
    [120, "in 2 hours", 60000], [1439, "in 23 hours", 3600000],
    [1440, "in 1 day", 60000], [2880, "in 2 days", 60000],
  ]) {
    const value = TimestampRow.display({ state: new Date(now + minutes * 60000).toISOString() }, now);
    assert.equal(value.text, text, `${minutes} minutes`);
    assert.equal(value.delay, delay, `${minutes} boundary`);
  }
  assert.equal(TimestampRow.display({ state: "unavailable" }, now).text, "Unavailable");
  for (const state of [undefined, { state: "unknown" }, { state: "invalid" }]) {
    assert.deepEqual(TimestampRow.display(state, now), { text: "\u2014" });
  }
});

test("timestamp timers pause while hidden, clean up, and preserve exact-time details", (t) => {
  const now = Date.parse("2026-09-08T00:00:00Z");
  t.mock.timers.enable({ apis: ["Date", "setTimeout"], now });
  const row = new TimestampRow();
  row.setConfig({ entity: "sensor.next", name: "Next clean" });
  row.hass = { states: { "sensor.next": { state: new Date(now + 61000).toISOString() } } };
  row.connectedCallback();
  // HA's generic entity row accepts reactive properties, not setConfig().
  assert.equal(row._row.setConfig, undefined);
  assert.deepEqual(row._row.config, { entity: "sensor.next", name: "Next clean" });
  assert.equal(row._row.hass, row._hass);
  assert.equal(row._value.textContent, "in 2 minutes");
  t.mock.timers.tick(1000);
  assert.equal(row._value.textContent, "in 1 minute");
  visibilityState = "hidden";
  visibilityListeners.get("visibilitychange")();
  assert.equal(row._timer, undefined);
  t.mock.timers.tick(60000);
  assert.equal(row._value.textContent, "in 1 minute");
  visibilityState = "visible";
  visibilityListeners.get("visibilitychange")();
  assert.equal(row._value.textContent, "Due now");
  assert.equal(row._timer, undefined);
  row.listeners.get("click")();
  assert.equal(row.events[0].type, "hass-more-info");
  assert.equal(row.events[0].detail.entityId, "sensor.next");
  row.hass = { states: { "sensor.next": { state: new Date(Date.now() + 60000).toISOString() } } };
  assert.notEqual(row._timer, undefined);
  row.disconnectedCallback();
  assert.equal(row._timer, undefined);
  t.mock.timers.tick(60000);
  row.connectedCallback();
  assert.equal(row._value.textContent, "Due now");
  row.disconnectedCallback();
});

test("new room layout leads with Status, Next clean, Last clean and reads Status diagnostics", () => {
  const states = baseStates();
  states["sensor.kitchen_next_clean"].attributes.device_class = "timestamp";
  states["sensor.kitchen_status"] = adaptiveState("room_status", {
    area_id: "kitchen", room: "Kitchen", friendly_name: "Kitchen status", duration_model_version: 2,
    predicted_total_minutes: 24, required_vacancy_minutes: 28,
  }, "Scheduled");
  const { configuration } = configure(RoomCard, { area_id: "kitchen" }, states);
  assert.deepEqual(configuration.entities.slice(0, 5), [
    { type: "custom:adaptive-robovacs-status-row", entity: "sensor.kitchen_status", name: "Status" },
    { type: "custom:adaptive-robovacs-timestamp-row", entity: "sensor.kitchen_next_clean", name: "Next clean" },
    { type: "attribute", entity: "sensor.kitchen_last_cleaned", attribute: "last_cleaned_display", name: "Last cleaned" },
    { type: "attribute", entity: "sensor.kitchen_status", attribute: "predicted_total_minutes", name: "Predicted total (min)" },
    { type: "attribute", entity: "sensor.kitchen_status", attribute: "required_vacancy_minutes", name: "Required vacancy (min)" },
  ]);
});

test("status row derives live clear time from stable timestamps and pauses while hidden", (t) => {
  const now = new Date("2026-09-17T00:00:21Z");
  t.mock.timers.enable({ apis: ["Date", "setTimeout"], now });
  const row = new StatusRow();
  row.setConfig({ entity: "sensor.study_status", name: "Status" });
  row.hass = {
    states: {
      "sensor.study_status": {
        state: "Adjacent room protection: Bedroom (waiting for 30 clear minutes)",
        attributes: {
          adjacency_blocked: true,
          adjacency_blockers: [{
            area_id: "bedroom",
            name: "Bedroom",
            occupancy: "unoccupied",
            vacancy_diagnostic: {
              unoccupied_since: "2026-09-17T00:00:00Z",
              reason: "waiting for 30 clear minutes",
            },
          }],
        },
      },
    },
  };
  row.connectedCallback();
  assert.equal(
    row._value.textContent,
    "Adjacent room protection: Bedroom (waiting for 30 clear minutes; clear for 0.3 minutes)"
  );
  t.mock.timers.tick(6000);
  assert.equal(
    row._value.textContent,
    "Adjacent room protection: Bedroom (waiting for 30 clear minutes; clear for 0.5 minutes)"
  );
  visibilityState = "hidden";
  visibilityListeners.get("visibilitychange")();
  t.mock.timers.tick(12000);
  assert.match(row._value.textContent, /clear for 0\.5 minutes/);
  visibilityState = "visible";
  visibilityListeners.get("visibilitychange")();
  assert.match(row._value.textContent, /clear for 0\.7 minutes/);
  row.disconnectedCallback();
});
test("room evidence distinguishes historical confidence, fallback and policy", () => {
  const attributes = {
    selected_robot_id: "vacuum.alpha",
    robot_eligibility: [{ robot_entity_id: "vacuum.alpha", robot_name: "Alpha", operation: "vacuum", passes: 2, duration_minutes: 40, eligible: true,
      vacancy_diagnostic: { required_clear_minutes: 50, confidence_available: false, decision_source: "stable_vacancy_fallback", forecast_confidence: 0 } }],
  };
  const state = { state: "ready now", attributes };
  assert.match(EvidenceRow.display(state), /Alpha \(selected\): vacuum, 2 pass/);
  assert.match(EvidenceRow.display(state), /horizon 50 min; Insufficient surviving history; stable-vacancy fallback/);
  assert.doesNotMatch(EvidenceRow.display(state), /100%/);
  Object.assign(attributes.robot_eligibility[0].vacancy_diagnostic, {
    confidence_available: true, decision_source: "historical", forecast_confidence: 0.8,
    successful_sample_count: 8, surviving_sample_count: 10,
  });
  assert.match(EvidenceRow.display(state), /8\/10 surviving samples \(80%\)/);
  Object.assign(attributes.robot_eligibility[0].vacancy_diagnostic, {
    decision_source: "policy", reason: "no-sensor policy",
  });
  assert.match(EvidenceRow.display(state), /no-sensor policy/);
  assert.equal(EvidenceRow.display(null), "Unavailable");
});

test("room evidence keeps program completion, sensor noise and missed windows distinct", () => {
  const state = { state: "Overdue", attributes: {
    enabled: true, overdue: true, last_vacuum: "2026-10-01T10:00:00Z", last_mop: null, last_full_clean: null,
    occurrence: { current_stage: 1, stages: [{ operation: "vacuum", passes: 2 }, { operation: "mop", passes: 1 }], follow_up_warning: "Mop follow-up expired repeatedly" },
    opportunity: { missed_cadences: 3, windows: [{}], minutes_by_cause: { forecast: 60, outside_hours: 10 }, advice: "Review vacancy settings." },
    occupancy_quality: { transitions_24h: 4096, transition_limit_reached: true, subminute_vacancies_24h: 12, median_vacancy_seconds: 21, retained_forecast_samples: 6 },
  } };
  assert.match(EvidenceRow.display(state, 0, "program"), /Mop: No completion recorded; Full program: No completion recorded; Next: mop/);
  assert.match(EvidenceRow.display(state, 0, "program"), /expired repeatedly/);
  assert.match(EvidenceRow.display(state, 0, "opportunity"), /3 cadence deadline\(s\) missed; 1 window summaries/);
  assert.match(EvidenceRow.display(state, 0, "opportunity"), /forecast: 60 min/);
  assert.match(EvidenceRow.display(state, 0, "occupancy"), /4096\+ observed changes; 12 vacancies under 1 min; median 21 s/);
  state.attributes.overdue = false;
  assert.equal(EvidenceRow.display(state, 0, "opportunity"), "Within cadence");
  state.attributes.enabled = false;
  assert.equal(EvidenceRow.display(state, 0, "opportunity"), "Scheduling disabled");
});

test("room card includes evidence rows and evidence updates without markup injection", () => {
  const states = baseStates();
  states["sensor.kitchen_status"] = adaptiveState("room_status", { area_id: "kitchen", room: "Kitchen", opportunity: {}, block_reason: "<script>synthetic</script>" }, "Overdue");
  const { configuration } = configure(RoomCard, { area_id: "kitchen" }, states);
  assert.deepEqual(configuration.entities.filter(row => row.type === "custom:adaptive-robovacs-evidence-row").map(row => row.kind), ["forecast", "program", "opportunity", "occupancy"]);
  const row = new EvidenceRow();
  row.setConfig({ entity: "sensor.kitchen_status", kind: "forecast", name: "Decision" });
  row.hass = { states };
  row.connectedCallback();
  assert.equal(row._label.textContent, "Decision");
  assert.equal(row._value.textContent, "Not evaluated: <script>synthetic</script>");
  assert.equal(row._value.innerHTML, undefined);
  row.disconnectedCallback();
});
