const ADAPTIVE_ROBOVACS_DOMAIN = "adaptive_robovacs";
const ENTRY_ATTRIBUTE = "adaptive_robovacs_entry_id";
const ROLE_ATTRIBUTE = "adaptive_robovacs_role";
const DOCUMENTATION_URL =
  "https://github.com/Googlproxer/adaptive-robovacs/blob/main/docs/dashboard.md";

const GLOBAL_ROLES = new Map([
  ["scheduler_status", 0],
  ["fault_resume_control", 1],
  ["global_control", 2],
  ["scheduler_control", 3],
]);
const VACUUM_ROLES = new Map([
  ["robot_status", 0],
  ["robot_control", 1],
  ["robot_stop_return_control", 2],
]);
const ROOM_ROLES = new Map([
  ["room_schedule", 0],
  ["room_last_cleaned", 1],
  ["room_occupancy", 2],
  ["room_cleaning_program_control", 4],
  ["room_pass_count_control", 5],
  ["room_mop_pass_count_control", 6],
  ["room_fan_speed_control", 7],
  ["room_mode_control", 8],
  ["room_mop_mode_control", 9],
  ["room_mop_intensity_control", 10],
  ["room_cleaning_depth_control", 11],
  ["room_window_start_control", 12],
  ["room_window_end_control", 13],
  ["room_control", 14],
  ["room_manual_clean_control", 15],
  ["room_manual_vacuum_control", 16],
  ["room_manual_mop_control", 17],
]);
const ROOM_HIDDEN_ROLES = new Set(["room_manual_status"]);

const COMMON_FORM_SCHEMA = [
  {
    name: "entry_id",
    selector: { config_entry: { integration: ADAPTIVE_ROBOVACS_DOMAIN } },
  },
  { name: "title", selector: { text: {} } },
];

function assertString(config, key, required = false) {
  const value = config?.[key];
  if (required && (!value || typeof value !== "string")) {
    throw new Error(`'${key}' is required.`);
  }
  if (value !== undefined && value !== null && typeof value !== "string") {
    throw new Error(`'${key}' must be a string.`);
  }
}

function assertCardConfig(config, targetKey) {
  assertString(config, "entry_id");
  assertString(config, "title");
  if (targetKey) assertString(config, targetKey);
}

function nameWithoutTargetPrefix(name, targetName) {
  if (typeof name !== "string" || typeof targetName !== "string" || !targetName.trim()) {
    return undefined;
  }
  const escapedTarget = targetName.trim().replace(/[\\^$.*+?()[\]{}|]/g, "\\$&");
  const match = name.match(new RegExp("^" + escapedTarget + "(?=$|[\\s:–—-])", "i"));
  if (!match) return undefined;
  const remainder = name.slice(match[0].length).replace(/^[\s:–—-]+/, "").trim();
  if (!remainder) return undefined;
  return remainder[0].toLocaleUpperCase() + remainder.slice(1);
}

function formDefinition(targetField) {
  const schema = [...COMMON_FORM_SCHEMA];
  if (targetField) schema.splice(1, 0, targetField);
  return {
    schema,
    computeLabel: (field) => ({
      entry_id: "Integration entry",
      vacuum_entity_id: "Vacuum",
      area_id: "Room",
      title: "Title",
    })[field.name],
    computeHelper: (field) => ({
      entry_id: "Optional when only one Adaptive RoboVacs entry is loaded.",
      vacuum_entity_id: "Choose one vacuum discovered by Adaptive RoboVacs.",
      area_id: "Choose one room discovered by Adaptive RoboVacs.",
      title: "Leave empty to use the discovered name.",
    })[field.name],
  };
}

class AdaptiveRoboVacsCardBase extends HTMLElement {
  setConfig(config) {
    this._assertConfig(config || {});
    this._config = { ...(config || {}) };
    this._signature = undefined;
    if (this._hass) this._refresh();
  }

  set hass(hass) {
    this._hass = hass;
    this._refresh();
  }

  getCardSize() {
    return this._card?.getCardSize?.() ?? 3;
  }

  getGridOptions() {
    return { columns: "full" };
  }

  _assertConfig(config) {
    assertCardConfig(config);
  }

  _defaultTitle() {
    return "Adaptive RoboVacs";
  }

  _title() {
    return this._config?.title || this._defaultTitle();
  }

  _allAdaptiveEntities() {
    if (!this._hass) return [];
    return Object.entries(this._hass.states)
      .filter(([, state]) => Boolean(state.attributes?.[ENTRY_ATTRIBUTE]))
      .map(([entityId, state]) => ({
        entityId,
        state,
        attrs: state.attributes || {},
      }));
  }

  _entryContext() {
    const allEntities = this._allAdaptiveEntities();
    const configuredEntry = this._config?.entry_id;
    const entryIds = [...new Set(
      allEntities.map((item) => item.attrs[ENTRY_ATTRIBUTE]).filter(Boolean)
    )].sort();

    if (configuredEntry) {
      const entities = allEntities.filter(
        (item) => item.attrs[ENTRY_ATTRIBUTE] === configuredEntry
      );
      return entities.length
        ? { entryId: configuredEntry, entities }
        : { error: "No Adaptive RoboVacs entities were found for the selected integration entry." };
    }
    if (entryIds.length === 0) {
      return { error: "No Adaptive RoboVacs entities are currently available." };
    }
    if (entryIds.length > 1) {
      return { error: "Select an Adaptive RoboVacs integration entry for this card." };
    }
    return { entryId: entryIds[0], entities: allEntities };
  }

  _orderedEntities(items, roleOrder) {
    const unique = new Map(items.map((item) => [item.entityId, item]));
    return [...unique.values()]
      .sort((left, right) => {
        const leftOrder = roleOrder.get(left.attrs[ROLE_ATTRIBUTE]) ?? 100;
        const rightOrder = roleOrder.get(right.attrs[ROLE_ATTRIBUTE]) ?? 100;
        return leftOrder - rightOrder || left.entityId.localeCompare(right.entityId);
      });
  }

  _orderedEntityIds(items, roleOrder) {
    return this._orderedEntities(items, roleOrder).map((item) => item.entityId);
  }

  _targetEntityRows(items, roleOrder, targetName) {
    return this._orderedEntities(items, roleOrder).map((item) => {
      const name = nameWithoutTargetPrefix(item.attrs.friendly_name, targetName);
      return name ? { entity: item.entityId, name } : item.entityId;
    });
  }

  _entitiesConfiguration(title, entityIds) {
    return {
      type: "entities",
      title,
      show_header_toggle: false,
      entities: entityIds,
    };
  }

  _messageConfiguration(message) {
    return {
      type: "markdown",
      title: this._title(),
      content: message,
    };
  }

  _configuration() {
    throw new Error("Card configuration is not implemented.");
  }

  _targetSignature() {
    return null;
  }

  _entitySignature() {
    const entities = this._allAdaptiveEntities()
      .map((item) => [
        item.entityId,
        item.attrs[ENTRY_ATTRIBUTE],
        item.attrs[ROLE_ATTRIBUTE],
        item.attrs.robot_entity_id,
        item.attrs.area_id,
        item.attrs.room,
        item.attrs.floor_id,
        item.attrs.bedroom,
        item.attrs.friendly_name,
        item.attrs.failure_code,
        item.attrs.repair_active,
      ])
      .sort(([left], [right]) => left.localeCompare(right));
    return JSON.stringify({
      config: this._config || {},
      entities,
      target: this._targetSignature(),
    });
  }

  _refresh() {
    if (!this._hass || !this._config) return;
    const signature = this._entitySignature();
    if (signature !== this._signature) {
      this._signature = signature;
      this._render();
    }
    if (this._card) this._card.hass = this._hass;
  }

  async _render() {
    if (!this._hass) return;
    const generation = (this._renderGeneration || 0) + 1;
    this._renderGeneration = generation;
    const helpers = await window.loadCardHelpers();
    const card = await helpers.createCardElement(this._configuration());
    if (generation !== this._renderGeneration) return;
    this._card = card;
    card.hass = this._hass;
    this.replaceChildren(card);
  }
}

class AdaptiveRoboVacsGlobalCard extends AdaptiveRoboVacsCardBase {
  static getConfigForm() {
    return {
      ...formDefinition(),
      assertConfig: (config) => assertCardConfig(config),
    };
  }

  static getStubConfig() {
    return {};
  }

  _defaultTitle() {
    return "Scheduler";
  }

  _configuration() {
    const context = this._entryContext();
    if (context.error) return this._messageConfiguration(context.error);
    const entities = context.entities.filter((item) => GLOBAL_ROLES.has(item.attrs[ROLE_ATTRIBUTE]));
    const entityRows = this._orderedEntities(entities, GLOBAL_ROLES).map((item) =>
      item.attrs[ROLE_ATTRIBUTE] === "fault_resume_control"
        ? {
          entity: item.entityId,
          name: "Recheck and resume",
          tap_action: {
            action: "perform-action",
            perform_action: "button.press",
            target: { entity_id: item.entityId },
            confirmation: {
              text: "Recheck the failed request and resume scheduler dispatch? No test clean will be sent.",
            },
          },
        }
        : item.entityId
    );
    return entityRows.length
      ? this._entitiesConfiguration(this._title(), entityRows)
      : this._messageConfiguration("No scheduler controls or status entities are available.");
  }
}

class AdaptiveRoboVacsVacuumCard extends AdaptiveRoboVacsCardBase {
  static getConfigForm() {
    return {
      ...formDefinition({
        name: "vacuum_entity_id",
        required: true,
        selector: { entity: { domain: "vacuum" } },
      }),
      assertConfig: (config) => assertCardConfig(config, "vacuum_entity_id"),
    };
  }

  static getStubConfig() {
    return {};
  }

  _assertConfig(config) {
    assertCardConfig(config, "vacuum_entity_id");
  }

  _defaultTitle() {
    const entityId = this._config?.vacuum_entity_id;
    return this._hass?.states?.[entityId]?.attributes?.friendly_name || entityId || "Vacuum";
  }

  _targetSignature() {
    const entityId = this._config?.vacuum_entity_id;
    const state = this._hass?.states?.[entityId];
    return [entityId, state?.attributes?.friendly_name];
  }

  _configuration() {
    const entityId = this._config?.vacuum_entity_id;
    if (!entityId) return this._messageConfiguration("Select a vacuum in the card editor.");
    const context = this._entryContext();
    if (context.error) return this._messageConfiguration(context.error);
    const entities = context.entities.filter(
      (item) => item.attrs.robot_entity_id === entityId
    );
    const entityRows = this._targetEntityRows(
      entities,
      VACUUM_ROLES,
      this._hass?.states?.[entityId]?.attributes?.friendly_name
    );
    return entityRows.length
      ? this._entitiesConfiguration(this._title(), entityRows)
      : this._messageConfiguration(
        "The selected vacuum is not currently discovered by this Adaptive RoboVacs entry."
      );
  }
}

class AdaptiveRoboVacsRoomCard extends AdaptiveRoboVacsCardBase {
  static getConfigForm() {
    return {
      ...formDefinition({
        name: "area_id",
        required: true,
        selector: { area: {} },
      }),
      assertConfig: (config) => assertCardConfig(config, "area_id"),
    };
  }

  static getStubConfig() {
    return {};
  }

  _assertConfig(config) {
    assertCardConfig(config, "area_id");
  }

  _defaultTitle() {
    const areaId = this._config?.area_id;
    const schedule = this._allAdaptiveEntities().find(
      (item) => item.attrs.area_id === areaId && item.attrs[ROLE_ATTRIBUTE] === "room_schedule"
    );
    return schedule?.attrs?.room || areaId || "Room";
  }

  _configuration() {
    const areaId = this._config?.area_id;
    if (!areaId) return this._messageConfiguration("Select a room in the card editor.");
    const context = this._entryContext();
    if (context.error) return this._messageConfiguration(context.error);
    const entities = context.entities.filter(
      (item) =>
        item.attrs.area_id === areaId &&
        !ROOM_HIDDEN_ROLES.has(item.attrs[ROLE_ATTRIBUTE])
    );
    const roomName = entities.find((item) => item.attrs.room)?.attrs.room || this._defaultTitle();
    const entityRows = this._targetEntityRows(entities, ROOM_ROLES, roomName);
    return entityRows.length
      ? this._entitiesConfiguration(this._title(), entityRows)
      : this._messageConfiguration(
        "The selected room is not currently discovered by this Adaptive RoboVacs entry."
      );
  }
}

customElements.define("adaptive-robovacs-global", AdaptiveRoboVacsGlobalCard);
customElements.define("adaptive-robovacs-vacuum", AdaptiveRoboVacsVacuumCard);
customElements.define("adaptive-robovacs-room", AdaptiveRoboVacsRoomCard);

window.customCards = window.customCards || [];
[
  {
    type: "adaptive-robovacs-global",
    name: "Adaptive RoboVacs Global",
    description: "Scheduler-wide Adaptive RoboVacs status and controls.",
  },
  {
    type: "adaptive-robovacs-vacuum",
    name: "Adaptive RoboVacs Vacuum",
    description: "Status and controls for one Adaptive RoboVacs vacuum.",
  },
  {
    type: "adaptive-robovacs-room",
    name: "Adaptive RoboVacs Room",
    description: "Status and controls for one Adaptive RoboVacs room.",
  },
].forEach((card) => {
  if (!window.customCards.some((registered) => registered.type === card.type)) {
    window.customCards.push({ ...card, documentationURL: DOCUMENTATION_URL });
  }
});
