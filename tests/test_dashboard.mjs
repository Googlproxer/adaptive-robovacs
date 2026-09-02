import assert from "node:assert/strict";
import test from "node:test";


class MockElement {
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
          text: "Recheck the failed request and resume scheduler dispatch? No test clean will be sent.",
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

  assert.deepEqual(configuration.entities.slice(0, 3), [
    { entity: "sensor.kitchen_next_clean", name: "Next clean" },
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
    FloorPlanCard.prototype._render.toString(),
    /height:calc\(100dvh - 112px\)/,
  );
  assert.deepEqual(card._point({ clientX: 360, clientY: 0 }, svg, 60), { x: 0, y: 0 });
  assert.deepEqual(card._point({ clientX: 600, clientY: 300 }, svg, 60), { x: 24, y: 30 });
  assert.deepEqual(card._point({ clientX: 840, clientY: 600 }, svg, 60), { x: 48, y: 60 });
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
