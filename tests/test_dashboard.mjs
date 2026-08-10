import assert from "node:assert/strict";
import test from "node:test";


class MockElement {
  replaceChildren(...children) {
    this.children = children;
  }
}

const elements = new Map();
globalThis.HTMLElement = MockElement;
globalThis.customElements = {
  define(name, constructor) {
    elements.set(name, constructor);
  },
  get(name) {
    return elements.get(name);
  },
};
globalThis.window = {
  customCards: [],
  loadCardHelpers: async () => ({
    createCardElement: async (configuration) => ({
      configuration,
      getCardSize: () => 4,
    }),
  }),
};

await import("../custom_components/adaptive_robovacs/frontend/adaptive-robovacs-dashboard.js");

const GlobalCard = elements.get("adaptive-robovacs-global");
const VacuumCard = elements.get("adaptive-robovacs-vacuum");
const RoomCard = elements.get("adaptive-robovacs-room");

const entry = "entry-one";
const adaptiveState = (role, attributes = {}) => ({
  state: "ready",
  attributes: {
    adaptive_robovacs_entry_id: entry,
    adaptive_robovacs_role: role,
    ...attributes,
  },
});

const baseStates = () => ({
  "sensor.scheduler": adaptiveState("scheduler_status"),
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
  "sensor.robot_two_status": adaptiveState("robot_status", {
    robot_entity_id: "vacuum.robot_two",
    friendly_name: "Robot Two status",
  }),
  "switch.robot_two_enabled": adaptiveState("robot_control", {
    robot_entity_id: "vacuum.robot_two",
    friendly_name: "Robot Two enabled",
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
  "select.kitchen_desired_start": adaptiveState("room_window_start_control", {
    area_id: "kitchen",
    friendly_name: "Kitchen desired cleaning start",
  }),
  "select.kitchen_desired_end": adaptiveState("room_window_end_control", {
    area_id: "kitchen",
    friendly_name: "Kitchen desired cleaning end",
  }),
  "number.kitchen_cadence": adaptiveState("room_control", {
    area_id: "kitchen",
    friendly_name: "Kitchen cadence",
  }),
  "switch.kitchen_enabled": adaptiveState("room_control", {
    area_id: "kitchen",
    friendly_name: "Kitchen enabled",
  }),
  "sensor.bedroom_next_clean": adaptiveState("room_schedule", {
    area_id: "bedroom",
    room: "Bedroom",
    floor_id: "upper",
    bedroom: true,
  }),
  "switch.bedroom_enabled": adaptiveState("room_control", {
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

test("registers the three target-scoped cards only", () => {
  assert.deepEqual([...elements.keys()], [
    "adaptive-robovacs-global",
    "adaptive-robovacs-vacuum",
    "adaptive-robovacs-room",
  ]);
  assert.deepEqual(
    window.customCards.map((card) => card.type),
    [
      "adaptive-robovacs-global",
      "adaptive-robovacs-vacuum",
      "adaptive-robovacs-room",
    ]
  );
});

test("global card includes scheduler status and controls in role order", () => {
  const { configuration } = configure(GlobalCard, {});
  assert.equal(configuration.type, "entities");
  assert.equal(configuration.title, "Scheduler");
  assert.deepEqual(configuration.entities, [
    "sensor.scheduler",
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
  ]);
  assert.ok(configuration.entities.every((row) => !row.entity.includes("robot_two")));
});

test("room card contains one selected room with status before controls", () => {
  const { configuration } = configure(RoomCard, { area_id: "kitchen" });
  assert.equal(configuration.title, "Kitchen");
  assert.deepEqual(configuration.entities, [
    { entity: "sensor.kitchen_next_clean", name: "Next clean" },
    { entity: "sensor.kitchen_last_cleaned", name: "Last cleaned" },
    { entity: "sensor.kitchen_occupancy", name: "Occupancy" },
    { entity: "select.kitchen_desired_start", name: "Desired cleaning start" },
    { entity: "select.kitchen_desired_end", name: "Desired cleaning end" },
    { entity: "number.kitchen_cadence", name: "Cadence" },
    { entity: "switch.kitchen_enabled", name: "Enabled" },
  ]);
  const entityIds = configuration.entities.map((row) => row.entity);
  assert.ok(entityIds.every((entityId) => !entityId.includes("bedroom")));
  assert.equal(new Set(entityIds).size, entityIds.length);
});

test("new target-owned controls appear without changing card configuration", () => {
  const states = baseStates();
  const initial = configure(RoomCard, { area_id: "kitchen" }, states).configuration;
  states["select.kitchen_profile"] = adaptiveState("room_control", {
    area_id: "kitchen",
    friendly_name: "Kitchen profile",
  });
  const updated = configure(RoomCard, { area_id: "kitchen" }, states).configuration;
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

test("cards request full section width and validate option types", () => {
  const { card } = configure(RoomCard, { area_id: "kitchen" });
  assert.deepEqual(card.getGridOptions(), { columns: "full" });
  assert.throws(() => configure(RoomCard, { area_id: ["kitchen"] }), /must be a string/);
  assert.throws(() => configure(GlobalCard, { title: 42 }), /must be a string/);
});
