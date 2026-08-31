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
  ["robot_map_capture_status", 3],
  ["robot_map_capture", 4],
  ["robot_map_snapshot_preview_select", 5],
]);
const ROOM_ROLES = new Map([
  ["room_schedule", 0],
  ["room_last_cleaned", 1],
  ["room_occupancy", 2],
  ["room_cleaning_period_control", 3],
  ["room_cleaning_profile_control", 4],
  ["room_cleaning_program_control", 5],
  ["room_pass_count_control", 6],
  ["room_mop_pass_count_control", 7],
  ["room_fan_speed_control", 8],
  ["room_mode_control", 9],
  ["room_mop_mode_control", 10],
  ["room_mop_intensity_control", 11],
  ["room_cleaning_depth_control", 12],
  ["room_window_start_control", 13],
  ["room_window_end_control", 14],
  ["room_control", 15],
  ["room_manual_clean_control", 16],
  ["room_manual_vacuum_control", 17],
  ["room_manual_mop_control", 18],
]);
const ROOM_HIDDEN_ROLES = new Set([
  "room_manual_status",
  "room_enabled_control",
  "room_ignore_desired_window_control",
]);
const ROOM_PROFILE_OVERRIDE_ROLES = new Set([
  "room_cleaning_program_control",
  "room_pass_count_control",
  "room_mop_pass_count_control",
  "room_fan_speed_control",
  "room_mode_control",
  "room_mop_mode_control",
  "room_mop_intensity_control",
  "room_cleaning_depth_control",
]);
const ROOM_WINDOW_OVERRIDE_ROLES = new Set([
  "room_window_start_control",
  "room_window_end_control",
]);
const EMPTY_ADAPTIVE_ENTITY_INDEX = {
  entities: [],
  entryIds: [],
  byEntry: new Map(),
};
const adaptiveEntityIndexes = new WeakMap();
const pendingCards = new Set();
let pendingAnimationFrame;
let pendingAnimationFrameKind;
let cardHelpersPromise;

function adaptiveEntityIndex(hass) {
  const states = hass?.states;
  if (!states || typeof states !== "object") return EMPTY_ADAPTIVE_ENTITY_INDEX;
  const cached = adaptiveEntityIndexes.get(states);
  if (cached) return cached;

  const byEntry = new Map();
  const entities = [];
  for (const [entityId, state] of Object.entries(states)) {
    const attrs = state?.attributes || {};
    const entryId = attrs[ENTRY_ATTRIBUTE];
    if (!entryId) continue;
    const item = { entityId, state, attrs };
    let entry = byEntry.get(entryId);
    if (!entry) {
      entry = { entities: [], byRobot: new Map(), byArea: new Map() };
      byEntry.set(entryId, entry);
    }
    entry.entities.push(item);
    if (attrs.robot_entity_id) {
      const robotEntities = entry.byRobot.get(attrs.robot_entity_id) || [];
      robotEntities.push(item);
      entry.byRobot.set(attrs.robot_entity_id, robotEntities);
    }
    if (attrs.area_id) {
      const areaEntities = entry.byArea.get(attrs.area_id) || [];
      areaEntities.push(item);
      entry.byArea.set(attrs.area_id, areaEntities);
    }
    entities.push(item);
  }
  const index = {
    entities,
    entryIds: [...byEntry.keys()].sort(),
    byEntry,
  };
  adaptiveEntityIndexes.set(states, index);
  return index;
}

function isDocumentHidden() {
  return typeof document !== "undefined" && document.visibilityState === "hidden";
}

function cancelPendingAnimationFrame() {
  if (pendingAnimationFrame === undefined) return;
  if (pendingAnimationFrameKind === "animation") {
    window.cancelAnimationFrame?.(pendingAnimationFrame);
  } else {
    clearTimeout(pendingAnimationFrame);
  }
  pendingAnimationFrame = undefined;
  pendingAnimationFrameKind = undefined;
}

function flushPendingCards() {
  pendingAnimationFrame = undefined;
  pendingAnimationFrameKind = undefined;
  if (isDocumentHidden()) return;
  const cards = [...pendingCards];
  pendingCards.clear();
  for (const card of cards) card._flushRefresh();
}

function requestPendingCardFlush() {
  if (pendingAnimationFrame !== undefined || isDocumentHidden() || !pendingCards.size) {
    return;
  }
  if (typeof window.requestAnimationFrame === "function") {
    pendingAnimationFrameKind = "animation";
    pendingAnimationFrame = window.requestAnimationFrame(flushPendingCards);
  } else {
    pendingAnimationFrameKind = "timeout";
    pendingAnimationFrame = setTimeout(flushPendingCards, 0);
  }
}

function queueCardRefresh(card) {
  if (!card._connected) return;
  pendingCards.add(card);
  requestPendingCardFlush();
}

function removeQueuedCard(card) {
  pendingCards.delete(card);
  if (!pendingCards.size) cancelPendingAnimationFrame();
}

function loadCardHelpers() {
  if (!cardHelpersPromise) {
    cardHelpersPromise = window.loadCardHelpers().catch((error) => {
      cardHelpersPromise = undefined;
      throw error;
    });
  }
  return cardHelpersPromise;
}

if (typeof document !== "undefined") {
  document.addEventListener("visibilitychange", requestPendingCardFlush);
}

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
    this._refresh();
  }

  set hass(hass) {
    this._hass = hass;
    this._refresh();
  }

  connectedCallback() {
    this._connected = true;
    this._refresh();
  }

  disconnectedCallback() {
    this._connected = false;
    this._renderGeneration = (this._renderGeneration || 0) + 1;
    removeQueuedCard(this);
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
    return adaptiveEntityIndex(this._hass).entities;
  }

  _entryContext() {
    const index = adaptiveEntityIndex(this._hass);
    const configuredEntry = this._config?.entry_id;

    if (configuredEntry) {
      const entry = index.byEntry.get(configuredEntry);
      return entry
        ? { entryId: configuredEntry, entities: entry.entities, entry }
        : { error: "No Adaptive RoboVacs entities were found for the selected integration entry." };
    }
    if (index.entryIds.length === 0) {
      return { error: "No Adaptive RoboVacs entities are currently available." };
    }
    if (index.entryIds.length > 1) {
      return { error: "Select an Adaptive RoboVacs integration entry for this card." };
    }
    const entryId = index.entryIds[0];
    const entry = index.byEntry.get(entryId);
    return { entryId, entities: entry.entities, entry };
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
      if (item.attrs[ROLE_ATTRIBUTE] === "room_last_cleaned") {
        return {
          type: "attribute",
          entity: item.entityId,
          attribute: "last_cleaned_display",
          name: name || "Last cleaned",
        };
      }
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

  _entitySignature() {
    return JSON.stringify({ config: this._config || {}, card: this._configuration() });
  }

  _refresh() {
    if (!this._hass || !this._config) return;
    queueCardRefresh(this);
  }

  _cardModel() {
    const configuration = this._configuration();
    const entityIds = configuration.entities
      ?.map((row) => typeof row === "string" ? row : row?.entity)
      .filter(Boolean) || [];
    return {
      configuration,
      dependencyIds: [...new Set(entityIds)],
    };
  }

  _childInput() {
    return {
      dependencies: this._dependencyIds.map((entityId) => this._hass?.states?.[entityId]),
      config: this._hass?.config,
      language: this._hass?.language,
      locale: this._hass?.locale,
      selectedTheme: this._hass?.selectedTheme,
      themes: this._hass?.themes,
      user: this._hass?.user,
    };
  }

  _sameChildInput(left, right) {
    return Boolean(left && right)
      && left.config === right.config
      && left.language === right.language
      && left.locale === right.locale
      && left.selectedTheme === right.selectedTheme
      && left.themes === right.themes
      && left.user === right.user
      && left.dependencies.length === right.dependencies.length
      && left.dependencies.every((state, index) => state === right.dependencies[index]);
  }

  _pushHassToCard(force = false) {
    if (!this._card || !this._hass) return;
    const input = this._childInput();
    if (force || !this._sameChildInput(input, this._lastChildInput)) {
      this._card.hass = this._hass;
      this._lastChildInput = input;
    }
  }

  _flushRefresh() {
    if (!this._connected || !this._hass || !this._config || isDocumentHidden()) return;
    const model = this._cardModel();
    const signature = JSON.stringify({ config: this._config, card: model.configuration });
    if (signature !== this._signature) {
      this._signature = signature;
      this._dependencyIds = model.dependencyIds;
      void this._render(model);
      return;
    }
    this._dependencyIds = model.dependencyIds;
    this._pushHassToCard();
  }

  async _render(model) {
    if (!this._hass) return;
    const generation = (this._renderGeneration || 0) + 1;
    this._renderGeneration = generation;
    try {
      const helpers = await loadCardHelpers();
      const card = await helpers.createCardElement(model.configuration);
      if (generation !== this._renderGeneration || !this._connected) return;
      this._card = card;
      this._dependencyIds = model.dependencyIds;
      this._lastChildInput = undefined;
      this._pushHassToCard(true);
      this.replaceChildren(card);
    } catch (error) {
      if (generation === this._renderGeneration) {
        this._signature = undefined;
        console.error("Adaptive RoboVacs dashboard could not create its card.", error);
      }
    }
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

  _configuration() {
    const entityId = this._config?.vacuum_entity_id;
    if (!entityId) return this._messageConfiguration("Select a vacuum in the card editor.");
    const context = this._entryContext();
    if (context.error) return this._messageConfiguration(context.error);
    const entities = context.entry.byRobot.get(entityId) || [];
    const entityRows = this._targetEntityRows(
      entities,
      VACUUM_ROLES,
      this._hass?.states?.[entityId]?.attributes?.friendly_name
    );
    const status = entities.find((item) => item.attrs[ROLE_ATTRIBUTE] === "robot_status");
    if (status?.attrs?.mop_profile_summary) {
      entityRows.splice(1, 0, {
        type: "attribute",
        entity: status.entityId,
        attribute: "mop_profile_summary",
        name: "Mopping",
      });
    }
    const mapCapture = entities.find(
      (item) => item.attrs[ROLE_ATTRIBUTE] === "robot_map_capture_status"
    );
    if (mapCapture?.attrs?.state === "map selection pending" || mapCapture?.attrs?.map_selection_pending) {
      entityRows.push({
        type: "button",
        name: "Confirm map selection and resume scheduling",
        icon: "mdi:map-check",
        tap_action: {
          action: "perform-action",
          perform_action: "adaptive_robovacs.confirm_map_selection",
          data: {
            entry_id: context.entryId,
            robot_entity_id: entityId,
            confirm: true,
          },
          confirmation: {
            text: "Confirm the robot has been manually relocalized and its Home Assistant room mapping is correct. Scheduler dispatch will be rechecked but no clean will start.",
          },
        },
      });
    }
    for (const map of mapCapture?.attrs?.available_maps || []) {
      if (!map?.map_id) continue;
      const label = map.name || "Retained map";
      entityRows.push({
        type: "button",
        name: `Activate retained map: ${label}`,
        icon: "mdi:map-marker",
        tap_action: {
          action: "perform-action",
          perform_action: "adaptive_robovacs.activate_retained_map",
          data: {
            entry_id: context.entryId,
            robot_entity_id: entityId,
            map_id: map.map_id,
            confirm: true,
          },
          confirmation: {
            text: "Activate this robot-retained map? The robot will not be started, and Adaptive RoboVacs will hold scheduling until you confirm the selection.",
          },
        },
      });
    }
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
    const entities = (context.entry.byArea.get(areaId) || []).filter(
      (item) => !ROOM_HIDDEN_ROLES.has(item.attrs[ROLE_ATTRIBUTE])
    );
    const profileMode = entities.find(
      (item) => item.attrs[ROLE_ATTRIBUTE] === "room_cleaning_profile_control"
    );
    const periodMode = entities.find(
      (item) => item.attrs[ROLE_ATTRIBUTE] === "room_cleaning_period_control"
    );
    const profileIsCustom = profileMode?.state?.state === "Custom";
    const periodIsCustom = periodMode?.state?.state === "Custom";
    const visibleEntities = entities.filter((item) => {
      const role = item.attrs[ROLE_ATTRIBUTE];
      return (
        (!profileMode || profileIsCustom || !ROOM_PROFILE_OVERRIDE_ROLES.has(role))
        && (!periodMode || periodIsCustom || !ROOM_WINDOW_OVERRIDE_ROLES.has(role))
      );
    });
    const roomName = visibleEntities.find((item) => item.attrs.room)?.attrs.room || this._defaultTitle();
    const entityRows = this._targetEntityRows(visibleEntities, ROOM_ROLES, roomName);
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
