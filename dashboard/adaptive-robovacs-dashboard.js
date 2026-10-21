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
  ["room_status", -1],
  ["room_schedule", 0],
  ["room_last_cleaned", 1],
  ["room_occupancy", 2],
  ["room_adjacency_mode_control", 2.5],
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
const timestampRows = new Set();
const statusRows = new Set();
let statusTimer;

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

function cancelStatusTimer() {
  if (statusTimer === undefined) return;
  clearTimeout(statusTimer);
  statusTimer = undefined;
}

function scheduleStatusRefresh() {
  cancelStatusTimer();
  if (isDocumentHidden() || !statusRows.size) return;
  const delay = 6001 - (Date.now() % 6000);
  statusTimer = setTimeout(() => {
    statusTimer = undefined;
    for (const row of statusRows) row._refreshStatus();
    scheduleStatusRefresh();
  }, delay);
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
  document.addEventListener("visibilitychange", () => {
    requestPendingCardFlush();
    for (const row of timestampRows) row._refreshTimestamp();
    for (const row of statusRows) row._refreshStatus();
    scheduleStatusRefresh();
  });
}

class AdaptiveRoboVacsTimestampRow extends HTMLElement {
  static display(state, now = Date.now()) {
    if (state?.state === "unavailable") return { text: "Unavailable" };
    const due = Date.parse(state?.state);
    if (!Number.isFinite(due)) return { text: "—" };
    if (due <= now) return { text: "Due now" };
    const minutes = Math.ceil((due - now) / 60000);
    const unitMinutes = minutes >= 1440 ? 1440 : minutes >= 60 ? 60 : 1;
    const count = Math.floor(minutes / unitMinutes);
    const unit = unitMinutes === 1440 ? "day" : unitMinutes === 60 ? "hour" : "minute";
    return {
      text: `in ${count} ${unit}${count === 1 ? "" : "s"}`,
      delay: due - now - (count * unitMinutes - 1) * 60000,
    };
  }

  setConfig(config) {
    assertString(config, "entity", true);
    this._config = { ...config };
    this._refreshTimestamp();
  }

  set hass(hass) {
    this._hass = hass;
    this._refreshTimestamp();
  }

  connectedCallback() {
    this._connected = true;
    timestampRows.add(this);
    if (!this._row) {
      const root = this.attachShadow({ mode: "open" });
      const style = document.createElement("style");
      style.textContent = ":host { display: block; cursor: pointer; }";
      this._row = document.createElement("hui-generic-entity-row");
      this._value = document.createElement("span");
      this._value.style.cssText = "text-align:end;white-space:normal;overflow-wrap:anywhere";
      this._row.append(this._value);
      root.append(style, this._row);
      this.setAttribute("role", "button");
      this.tabIndex = 0;
      this.addEventListener("click", () => this._showDetails());
      this.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          this._showDetails();
        }
      });
    }
    this._refreshTimestamp();
  }

  disconnectedCallback() {
    this._connected = false;
    timestampRows.delete(this);
    clearTimeout(this._timer);
    this._timer = undefined;
  }

  _showDetails() {
    this.dispatchEvent(new CustomEvent("hass-more-info", {
      detail: { entityId: this._config.entity }, bubbles: true, composed: true,
    }));
  }

  _refreshTimestamp() {
    clearTimeout(this._timer);
    this._timer = undefined;
    if (!this._connected || !this._hass || !this._config || isDocumentHidden()) return;
    this._row.config = this._config;
    this._row.hass = this._hass;
    const display = AdaptiveRoboVacsTimestampRow.display(this._hass.states[this._config.entity]);
    this._value.textContent = display.text;
    if (display.delay !== undefined) {
      this._timer = setTimeout(() => this._refreshTimestamp(), Math.max(1, display.delay));
    }
  }
}

customElements.define("adaptive-robovacs-timestamp-row", AdaptiveRoboVacsTimestampRow);

class AdaptiveRoboVacsStatusRow extends HTMLElement {
  static display(state, now = Date.now()) {
    if (state?.state === "unavailable") return "Unavailable";
    const attrs = state?.attributes || {};
    const blockers = attrs.adjacency_blockers;
    if (!attrs.adjacency_blocked || !Array.isArray(blockers) || !blockers.length) {
      return state?.state || "—";
    }
    const descriptions = blockers.map((blocker) => {
      const name = blocker?.name || blocker?.area_id || "Adjacent room";
      if (blocker?.occupancy === "occupied") return `${name} (occupied)`;
      if (blocker?.occupancy !== "unoccupied") {
        return `${name} (occupancy unresolved)`;
      }
      const diagnostic = blocker?.vacancy_diagnostic;
      if (!diagnostic) return `${name} (vacancy window unresolved)`;
      const since = Date.parse(diagnostic.unoccupied_since);
      const elapsed = Number.isFinite(since)
        ? `; clear for ${(Math.max(0, now - since) / 60000).toFixed(1)} minutes`
        : "";
      return `${name} (${diagnostic.reason || "vacancy window unresolved"}${elapsed})`;
    });
    return `${attrs.overdue ? "Overdue — " : ""}Adjacent room protection: ${descriptions.join("; ")}`;
  }

  setConfig(config) {
    assertString(config, "entity", true);
    this._config = { ...config };
    this._refreshStatus();
  }

  set hass(hass) {
    this._hass = hass;
    this._refreshStatus();
  }

  connectedCallback() {
    this._connected = true;
    statusRows.add(this);
    if (!this._row) {
      const root = this.attachShadow({ mode: "open" });
      const style = document.createElement("style");
      style.textContent = ":host { display: block; cursor: pointer; }";
      this._row = document.createElement("hui-generic-entity-row");
      this._value = document.createElement("span");
      this._value.style.cssText =
        "text-align:end;white-space:normal;overflow-wrap:anywhere";
      this._row.append(this._value);
      root.append(style, this._row);
      this.setAttribute("role", "button");
      this.tabIndex = 0;
      this.addEventListener("click", () => this._showDetails());
      this.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          this._showDetails();
        }
      });
    }
    this._refreshStatus();
    scheduleStatusRefresh();
  }

  disconnectedCallback() {
    this._connected = false;
    statusRows.delete(this);
    scheduleStatusRefresh();
  }

  _showDetails() {
    this.dispatchEvent(new CustomEvent("hass-more-info", {
      detail: { entityId: this._config.entity }, bubbles: true, composed: true,
    }));
  }

  _refreshStatus() {
    if (!this._connected || !this._hass || !this._config || isDocumentHidden()) {
      return;
    }
    this._row.config = this._config;
    this._row.hass = this._hass;
    this._value.textContent = this.constructor.display(
      this._hass.states[this._config.entity], Date.now(), this._config.kind
    );
  }
}

customElements.define("adaptive-robovacs-status-row", AdaptiveRoboVacsStatusRow);

class AdaptiveRoboVacsEvidenceRow extends AdaptiveRoboVacsStatusRow {
  connectedCallback() {
    // Give long evidence its own full-width line on narrow room cards.
    if (!this._row) {
      const root = this.attachShadow({ mode: "open" });
      const style = document.createElement("style");
      style.textContent = ":host{display:block;cursor:pointer;padding:8px 0;border-top:1px solid var(--divider-color,#ddd)}.label{font-weight:500;margin-bottom:5px}.evidence{display:block;line-height:1.5;white-space:normal;overflow-wrap:anywhere;color:var(--secondary-text-color,#555)}";
      this._row = document.createElement("div");
      this._label = document.createElement("div");
      this._label.className = "label";
      this._value = document.createElement("span");
      this._value.className = "evidence";
      this._row.append(this._label, this._value);
      root.append(style, this._row);
      this.setAttribute("role", "button");
      this.tabIndex = 0;
      this.addEventListener("click", () => this._showDetails());
      this.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          this._showDetails();
        }
      });
    }
    super.connectedCallback();
  }

  _refreshStatus() {
    if (this._label) this._label.textContent = this._config?.name || "Evidence";
    super._refreshStatus();
  }

  static display(state, now = Date.now(), kind = "forecast") {
    if (!state || state.state === "unavailable") return "Unavailable";
    const attrs = state.attributes || {};
    const stamp = (value) => {
      const time = Date.parse(value);
      return Number.isFinite(time) ? new Date(time).toLocaleString() : "No completion recorded";
    };
    if (kind === "program") {
      const occurrence = attrs.occurrence;
      const stage = occurrence?.stages?.[occurrence.current_stage];
      const pending = stage ? `; ${attrs.active_phase ? "Current" : "Next"}: ${stage.operation}, ${stage.passes} pass(es)` : "";
      const warning = occurrence?.follow_up_warning
        || (stage?.reason === "mop_delay_expired" ? "Vacuum must repeat: mop follow-up expired" : "");
      return `Vacuum: ${stamp(attrs.last_vacuum)}; Mop: ${stamp(attrs.last_mop)}; Full program: ${stamp(attrs.last_full_clean)}${pending}${warning ? `; ${warning}` : ""}`;
    }
    if (kind === "opportunity") {
      const item = attrs.opportunity || {};
      if (!attrs.enabled) return "Scheduling disabled";
      if (!attrs.overdue) return "Within cadence";
      const minutes = Object.entries(item.minutes_by_cause || {})
        .filter(([, value]) => value > 0).map(([cause, value]) => `${cause.replaceAll("_", " ")}: ${Math.round(value)} min`);
      return `${item.missed_cadences || 1} cadence deadline(s) missed; ${item.windows?.length || 0} window summaries. ${minutes.join("; ")}${minutes.length ? ". " : ""}${item.advice || "Collecting observed window evidence."}`;
    }
    if (kind === "occupancy") {
      const quality = attrs.occupancy_quality || {};
      const median = Number.isFinite(quality.median_vacancy_seconds) ? `${Math.round(quality.median_vacancy_seconds)} s` : "unavailable";
      return `Last 24 h: ${quality.transitions_24h || 0}${quality.transition_limit_reached ? "+" : ""} observed changes; ${quality.subminute_vacancies_24h || 0} vacancies under 1 min; median ${median}. ${quality.retained_forecast_samples || 0} retained forecast samples (56 days).`;
    }
    const decisions = attrs.robot_eligibility || [];
    const evidence = decisions.filter((item) => item.vacancy_diagnostic);
    if (!evidence.length) return `Not evaluated: ${attrs.block_reason || state.state}`;
    return evidence.map((item) => {
      const forecast = item.vacancy_diagnostic;
      const confidence = forecast.decision_source === "policy"
        ? forecast.reason
        : forecast.confidence_available
        ? `${forecast.successful_sample_count}/${forecast.surviving_sample_count} surviving samples (${Math.round(forecast.forecast_confidence * 100)}%)`
        : "Insufficient surviving history";
      const basis = forecast.decision_source === "stable_vacancy_fallback" ? "; stable-vacancy fallback" : "";
      const selected = attrs.selected_robot_id === item.robot_entity_id ? " (selected)" : "";
      const reason = (item.reason || "blocked").replace(/^insufficient surviving (vacancy )?history; /i, "");
      return `${item.robot_name}${selected}: ${item.operation}, ${item.passes} pass(es), ${item.duration_minutes} min; horizon ${forecast.required_clear_minutes} min; ${confidence}${basis}; ${item.eligible ? "eligible" : reason}`;
    }).join("; ");
  }
}

customElements.define("adaptive-robovacs-evidence-row", AdaptiveRoboVacsEvidenceRow);

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

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "'": "&#39;",
    '"': "&quot;",
  })[character]);
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
      if (item.attrs[ROLE_ATTRIBUTE] === "room_schedule" && item.attrs.device_class === "timestamp") {
        return { type: "custom:adaptive-robovacs-timestamp-row", entity: item.entityId, name: "Next clean" };
      }
      if (item.attrs[ROLE_ATTRIBUTE] === "room_status") {
        return {
          type: "custom:adaptive-robovacs-status-row",
          entity: item.entityId,
          name: "Status",
        };
      }
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
              text: "Recheck all scheduler holds and recoveries, release those now safe, and refresh the schedule? No test clean will be sent; normal safety checks still apply.",
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
      entities.filter((item) => VACUUM_ROLES.has(item.attrs[ROLE_ATTRIBUTE])),
      VACUUM_ROLES,
      this._hass?.states?.[entityId]?.attributes?.friendly_name
    );
    const status = entities.find((item) => item.attrs[ROLE_ATTRIBUTE] === "robot_status");
    for (const [attribute, name] of [
      ["cleaning_run_summary", "Cleaning run"],
      ["battery_learning_summary", "Battery forecast"],
    ]) {
      if (status?.attrs?.[attribute]) {
        entityRows.splice(1, 0, { type: "attribute", entity: status.entityId, attribute, name });
      }
    }
    if (status?.attrs?.mop_profile_summary) {
      entityRows.splice(1, 0, {
        type: "attribute",
        entity: status.entityId,
        attribute: "mop_profile_summary",
        name: "Mopping",
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
    const status = visibleEntities.find((item) => item.attrs[ROLE_ATTRIBUTE] === "room_status")
      || visibleEntities.find((item) => item.attrs[ROLE_ATTRIBUTE] === "room_schedule");
    if (status?.attrs?.duration_model_version === 2) {
      const lastCleanIndex = entityRows.findIndex((row) => row.attribute === "last_cleaned_display");
      entityRows.splice(lastCleanIndex >= 0 ? lastCleanIndex + 1 : 1, 0,
        {
          type: "attribute",
          entity: status.entityId,
          attribute: "predicted_total_minutes",
          name: "Predicted total (min)",
        },
        {
          type: "attribute",
          entity: status.entityId,
          attribute: "required_vacancy_minutes",
          name: "Required vacancy (min)",
        }
      );
    }
    if (status?.attrs?.opportunity) {
      entityRows.push(...[
        ["forecast", "Decision"], ["program", "Cleaning progress"],
        ["opportunity", "Missed windows"], ["occupancy", "Occupancy evidence"],
      ].map(([kind, name]) => ({
        type: "custom:adaptive-robovacs-evidence-row", entity: status.entityId, kind, name,
      })));
    }
    return entityRows.length
      ? this._entitiesConfiguration(this._title(), entityRows)
      : this._messageConfiguration(
        "The selected room is not currently discovered by this Adaptive RoboVacs entry."
      );
  }
}

class AdaptiveRoboVacsFloorPlanEditor extends HTMLElement {
  setConfig(config) {
    this._config = { ...(config || {}) };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  _entryContext() {
    const index = adaptiveEntityIndex(this._hass);
    const entryId = this._config?.entry_id || (index.entryIds.length === 1 ? index.entryIds[0] : undefined);
    const entry = entryId ? index.byEntry.get(entryId) : undefined;
    const scheduler = entry?.entities.find((item) => item.attrs[ROLE_ATTRIBUTE] === "scheduler_status");
    return { entryId, scheduler };
  }

  _change(key, value) {
    const config = { ...this._config, [key]: value || undefined };
    this.dispatchEvent(new CustomEvent("config-changed", {
      detail: { config }, bubbles: true, composed: true,
    }));
  }

  _render() {
    if (!this._hass || !this.attachShadow && !this.shadowRoot) return;
    const root = this.shadowRoot || this.attachShadow({ mode: "open" });
    const context = this._entryContext();
    const floors = context.scheduler?.attrs?.floor_plan?.floors || [];
    const entries = adaptiveEntityIndex(this._hass).entryIds;
    root.innerHTML = `
      <style>:host { display:block; } label { display:block; margin: 12px 0 4px; } select,input { box-sizing:border-box; width:100%; padding:8px; }</style>
      <label>Integration entry</label>
      <select data-field="entry_id"><option value="">${entries.length === 1 ? "Automatic" : "Select an entry"}</option>${entries.map((entryId) => `<option value="${escapeHtml(entryId)}" ${context.entryId === entryId ? "selected" : ""}>${escapeHtml(entryId)}</option>`).join("")}</select>
      <label>Floor</label>
      <select data-field="floor_id"><option value="">Select a floor</option>${floors.map((floor) => `<option value="${escapeHtml(floor.floor_id)}" ${this._config?.floor_id === floor.floor_id ? "selected" : ""}>${escapeHtml(floor.floor_id)}</option>`).join("")}</select>
      <label>Title</label><input data-field="title" value="${escapeHtml(this._config?.title || "")}" />
    `;
    root.querySelectorAll("[data-field]").forEach((element) => {
      element.addEventListener("change", (event) => this._change(event.target.dataset.field, event.target.value));
    });
  }
}

class AdaptiveRoboVacsFloorPlanCard extends HTMLElement {
  static getConfigElement() {
    return document.createElement("adaptive-robovacs-floorplan-editor");
  }

  static getStubConfig() {
    return {};
  }

  setConfig(config) {
    assertCardConfig(config, "floor_id");
    if (this._config?.floor_id !== config.floor_id || this._config?.entry_id !== config.entry_id) {
      this._cancelEditing(false);
    }
    this._config = { ...(config || {}) };
    queueCardRefresh(this);
  }

  set hass(hass) {
    this._hass = hass;
    if (this._editing && hass.user?.is_admin !== true) this._cancelEditing(false);
    if (!this._editing) queueCardRefresh(this);
  }

  connectedCallback() {
    this._connected = true;
    if (this._editing) this._render();
    else queueCardRefresh(this);
  }

  disconnectedCallback() {
    this._connected = false;
    removeQueuedCard(this);
    this._cancelDrag();
  }

  _renderInput(model) {
    if (model.error) return { error: model.error, config: this._config };
    return {
      config: this._config,
      entryId: model.entryId,
      admin: this._hass.user?.is_admin === true,
      rooms: model.floor.rooms.map((room) => ({
        area_id: room.area_id, name: room.name, rectangle: room.rectangle,
        sensors: (room.sensors || []).map((sensor) => ({
          registry_id: sensor.registry_id, name: this._sensorName(sensor),
          kind: sensor.kind, state: sensor.state, marker: sensor.marker,
        })),
      })),
      edges: (model.plan.edges || []).filter((edge) =>
        edge.every((areaId) => model.floor.rooms.some((room) => room.area_id === areaId))
      ),
      orphaned_rooms: model.plan.orphaned_rooms || [],
      orphaned_sensors: model.plan.orphaned_sensors || [],
    };
  }

  _flushRefresh() {
    if (!this._connected || !this._hass || !this._config || this._editing || isDocumentHidden()) return;
    const model = this._model();
    const signature = JSON.stringify(this._renderInput(model));
    if (signature === this._signature) return;
    this._signature = signature;
    this._render(model);
  }

  getCardSize() {
    return 8;
  }

  getGridOptions() {
    return { columns: "full" };
  }

  _context() {
    const index = adaptiveEntityIndex(this._hass);
    const configuredEntry = this._config?.entry_id;
    if (configuredEntry) {
      const entry = index.byEntry.get(configuredEntry);
      return entry ? { entryId: configuredEntry, entry } : { error: "No Adaptive RoboVacs entities were found for the selected integration entry." };
    }
    if (index.entryIds.length !== 1) {
      return { error: index.entryIds.length ? "Select an Adaptive RoboVacs integration entry for this floor plan." : "No Adaptive RoboVacs entities are currently available." };
    }
    return { entryId: index.entryIds[0], entry: index.byEntry.get(index.entryIds[0]) };
  }

  _model() {
    const context = this._context();
    if (context.error) return context;
    const scheduler = context.entry.entities.find((item) => item.attrs[ROLE_ATTRIBUTE] === "scheduler_status");
    const plan = scheduler?.attrs?.floor_plan;
    const floor = plan?.floors?.find((item) => item.floor_id === this._config?.floor_id);
    if (!floor) return { error: "Select a discovered floor in the card editor." };
    return { ...context, plan, floor };
  }

  _sensorName(sensor) {
    const friendlyName = this._hass?.states?.[sensor.entity_id]?.attributes?.friendly_name;
    return typeof friendlyName === "string" && friendlyName.trim() ? friendlyName : sensor.entity_id;
  }

  _beginEditing(model) {
    if (this._hass.user?.is_admin !== true || model.error) return;
    const rooms = {};
    const sensors = {};
    for (const room of model.floor.rooms) {
      if (room.rectangle) rooms[room.area_id] = { ...room.rectangle };
      for (const sensor of room.sensors || []) {
        if (sensor.marker) sensors[sensor.registry_id] = { ...sensor.marker };
      }
    }
    this._draft = {
      revision: model.plan.revision,
      rooms,
      sensors,
      forget_area_ids: [],
      forget_sensor_registry_ids: [],
      edges: (model.plan.edges || []).filter((edge) =>
        edge.length === 2 && model.floor.rooms.some((room) => room.area_id === edge[0])
          && model.floor.rooms.some((room) => room.area_id === edge[1])
      ).map((edge) => [...edge]),
    };
    this._editing = true;
    this._editModel = model;
    this._message = undefined;
    this._render();
  }

  _floorGeometry(model) {
    const rectangles = this._editing ? this._draft.rooms : Object.fromEntries(
      model.floor.rooms.filter((room) => room.rectangle).map((room) => [room.area_id, room.rectangle])
    );
    const height = this._drag?.height ?? Math.max(30, ...Object.values(rectangles).map((room) => room.y + room.height + 2));
    return { rectangles, height };
  }

  _point(event, svg, height) {
    const rect = svg.getBoundingClientRect();
    const scale = Math.min(rect.width / 48, rect.height / height);
    const contentWidth = 48 * scale;
    const contentHeight = height * scale;
    const left = rect.left + (rect.width - contentWidth) / 2;
    const top = rect.top + (rect.height - contentHeight) / 2;
    return {
      x: Math.max(0, Math.min(48, Math.round((event.clientX - left) / scale))),
      y: Math.max(0, Math.min(height, Math.round((event.clientY - top) / scale))),
    };
  }

  _roomAt(point, rectangles) {
    return Object.entries(rectangles).find(([, room]) =>
      point.x >= room.x && point.x <= room.x + room.width && point.y >= room.y && point.y <= room.y + room.height
    )?.[0];
  }

  _setDraftRoom(areaId, rectangle) {
    this._draft.rooms[areaId] = {
      floor_id: this._config.floor_id,
      x: Math.max(0, rectangle.x),
      y: Math.max(0, rectangle.y),
      width: Math.max(2, rectangle.width),
      height: Math.max(2, rectangle.height),
    };
  }

  _defaultRoomRectangle(room) {
    const width = Math.min(18, Math.max(12, Math.ceil(String(room.name).length * 1.15) + 4));
    const height = 9;
    const rectangles = Object.values(this._draft.rooms);
    const bottom = Math.max(24, ...rectangles.map((rectangle) => rectangle.y + rectangle.height));
    for (let y = 2; y <= bottom + height + 4; y += 2) {
      for (let x = 2; x <= 48 - width - 2; x += 2) {
        const overlaps = rectangles.some((rectangle) =>
          x < rectangle.x + rectangle.width + 2
          && x + width + 2 > rectangle.x
          && y < rectangle.y + rectangle.height + 2
          && y + height + 2 > rectangle.y
        );
        if (!overlaps) return { x, y, width, height };
      }
    }
    return { x: 2, y: bottom + 2, width, height };
  }

  _placeUnplacedRoom(areaId, model) {
    const room = model.floor.rooms.find((item) => item.area_id === areaId);
    if (!room || this._draft.rooms[areaId]) return;
    this._setDraftRoom(areaId, this._defaultRoomRectangle(room));
    this._render();
  }

  _placeUnplacedSensor(registryId, areaId) {
    if (!this._draft.rooms[areaId]) return;
    const markerPositions = [
      [500, 500], [700, 500], [300, 500], [500, 700], [500, 300], [700, 700], [300, 300],
    ];
    const existingMarkers = Object.values(this._draft.sensors).filter(
      (marker) => marker.area_id === areaId
    );
    const [x, y] = markerPositions[existingMarkers.length % markerPositions.length];
    this._draft.sensors[registryId] = { area_id: areaId, x, y };
    this._render();
  }

  _edgePoints(source, target) {
    const nearest = (sourceStart, sourceEnd, targetStart, targetEnd) => {
      if (sourceEnd < targetStart) return [sourceEnd, targetStart];
      if (sourceStart > targetEnd) return [sourceStart, targetEnd];
      const shared = Math.round((Math.max(sourceStart, targetStart) + Math.min(sourceEnd, targetEnd)) / 2);
      return [shared, shared];
    };
    const [sourceX, targetX] = nearest(source.x, source.x + source.width, target.x, target.x + target.width);
    const [sourceY, targetY] = nearest(source.y, source.y + source.height, target.y, target.y + target.height);
    const points = [{ x: sourceX, y: sourceY }];
    const deltaX = targetX - sourceX;
    const deltaY = targetY - sourceY;
    if (!deltaX && !deltaY) return points;
    if (deltaX && deltaY && Math.abs(deltaX) !== Math.abs(deltaY)) {
      const diagonal = Math.min(Math.abs(deltaX), Math.abs(deltaY));
      points.push({
        x: sourceX + Math.sign(deltaX) * diagonal,
        y: sourceY + Math.sign(deltaY) * diagonal,
      });
    }
    points.push({ x: targetX, y: targetY });
    return points;
  }

  _setLinkPreview(svg, rectangles, point) {
    const source = rectangles[this._drag?.areaId];
    if (!source) return;
    let preview = svg.querySelector("[data-link-preview]");
    if (!preview) {
      preview = svg.ownerDocument.createElementNS("http://www.w3.org/2000/svg", "g");
      preview.setAttribute("data-link-preview", "true");
      const line = svg.ownerDocument.createElementNS("http://www.w3.org/2000/svg", "polyline");
      line.setAttribute("class", "edge preview");
      line.setAttribute("fill", "none");
      const dot = svg.ownerDocument.createElementNS("http://www.w3.org/2000/svg", "circle");
      dot.setAttribute("class", "link-preview-dot");
      dot.setAttribute("r", "0.7");
      preview.append(line, dot);
      svg.append(preview);
    }
    const targetId = this._roomAt(point, rectangles);
    const target = targetId && targetId !== this._drag.areaId ? rectangles[targetId] : undefined;
    const sourcePoint = { x: source.x + source.width, y: source.y + source.height / 2 };
    const points = target ? this._edgePoints(source, target) : [sourcePoint, point];
    const line = preview.querySelector("polyline");
    const dot = preview.querySelector("circle");
    line.setAttribute("points", points.map((item) => `${item.x},${item.y}`).join(" "));
    if (target) {
      dot.setAttribute("display", "none");
    } else {
      dot.removeAttribute("display");
      dot.setAttribute("cx", point.x);
      dot.setAttribute("cy", point.y);
    }
  }

  _clearLinkPreview(svg) {
    svg.querySelector("[data-link-preview]")?.remove();
  }

  _onPointerDown(event, model) {
    if (!this._editing || this._saving || this._drag || event.button !== 0 || event.isPrimary === false) return;
    const svg = event.currentTarget;
    const geometry = this._floorGeometry(model);
    const point = this._point(event, svg, geometry.height);
    const target = event.target.closest?.("[data-room],[data-sensor],[data-link-source],[data-edge]");
    if (target?.dataset.linkSource) {
      this._drag = { kind: "link", areaId: target.dataset.linkSource };
    } else if (target?.dataset.sensor) {
      const marker = this._draft.sensors[target.dataset.sensor];
      this._drag = { kind: "sensor", registryId: target.dataset.sensor, marker: { ...marker } };
    } else if (target?.dataset.room) {
      const areaId = target.dataset.room;
      this._drag = { kind: target.dataset.resize ? "resize" : "room", areaId, origin: point, rectangle: { ...this._draft.rooms[areaId] } };
    }
    if (!this._drag) return;
    Object.assign(this._drag, { pointerId: event.pointerId, height: geometry.height, svg });
    svg.setPointerCapture?.(event.pointerId);
    if (this._drag.kind === "link") this._setLinkPreview(svg, geometry.rectangles, point);
    this._render();
  }

  _onPointerMove(event, model) {
    if (!this._drag || !this._editing || event.pointerId !== this._drag.pointerId) return;
    const svg = event.currentTarget;
    const geometry = this._floorGeometry(model);
    const point = this._point(event, svg, geometry.height);
    if (this._drag.kind === "link") {
      this._setLinkPreview(svg, geometry.rectangles, point);
      return;
    }
    if (this._drag.kind === "room") {
      this._setDraftRoom(this._drag.areaId, {
        ...this._drag.rectangle,
        x: this._drag.rectangle.x + point.x - this._drag.origin.x,
        y: this._drag.rectangle.y + point.y - this._drag.origin.y,
      });
    } else if (this._drag.kind === "resize") {
      this._setDraftRoom(this._drag.areaId, {
        ...this._drag.rectangle,
        width: point.x - this._drag.rectangle.x,
        height: point.y - this._drag.rectangle.y,
      });
    } else if (this._drag.kind === "create") {
      this._setDraftRoom(this._drag.areaId, {
        x: Math.min(this._drag.origin.x, point.x),
        y: Math.min(this._drag.origin.y, point.y),
        width: Math.abs(point.x - this._drag.origin.x),
        height: Math.abs(point.y - this._drag.origin.y),
      });
    } else if (this._drag.kind === "sensor") {
      const marker = this._draft.sensors[this._drag.registryId];
      const room = this._draft.rooms[marker.area_id];
      if (room) {
        marker.x = Math.max(0, Math.min(1000, Math.round(((point.x - room.x) / room.width) * 1000)));
        marker.y = Math.max(0, Math.min(1000, Math.round(((point.y - room.y) / room.height) * 1000)));
      }
    }
    this._render();
  }

  _onPointerUp(event, model) {
    if (!this._drag || event.pointerId !== this._drag.pointerId) return;
    const svg = event.currentTarget;
    if (this._drag.kind === "link") {
      const geometry = this._floorGeometry(model);
      const target = this._roomAt(this._point(event, svg, geometry.height), geometry.rectangles);
      if (target && target !== this._drag.areaId) {
        const edge = [this._drag.areaId, target].sort();
        if (!this._draft.edges.some((item) => item[0] === edge[0] && item[1] === edge[1])) this._draft.edges.push(edge);
      }
    }
    this._finishDrag();
    this._render();
  }

  _finishDrag() {
    const drag = this._drag;
    this._drag = undefined;
    if (!drag) return;
    this._clearLinkPreview(drag.svg);
    if (drag.svg.hasPointerCapture?.(drag.pointerId)) drag.svg.releasePointerCapture(drag.pointerId);
  }

  _cancelDrag(event) {
    const drag = this._drag;
    if (!drag || event && event.pointerId !== drag.pointerId) return;
    if (drag.kind === "room" || drag.kind === "resize") this._draft.rooms[drag.areaId] = drag.rectangle;
    if (drag.kind === "sensor") this._draft.sensors[drag.registryId] = drag.marker;
    this._finishDrag();
    if (event) this._render();
  }

  _cancelEditing(render = true) {
    this._cancelDrag();
    this._editing = false;
    this._editModel = undefined;
    this._draft = undefined;
    this._message = undefined;
    this._signature = undefined;
    if (render) this._render();
  }

  async _save(model) {
    if (!this._editing || this._saving || this._drag || this._hass.user?.is_admin !== true) return;
    const draft = this._draft;
    this._saving = true;
    this._render();
    try {
      await this._hass.callService("adaptive_robovacs", "save_floor_plan", {
        entry_id: model.entryId,
        floor_id: this._config.floor_id,
        revision: this._draft.revision,
        rooms: this._draft.rooms,
        edges: this._draft.edges,
        sensors: this._draft.sensors,
        forget_area_ids: this._draft.forget_area_ids,
        forget_sensor_registry_ids: this._draft.forget_sensor_registry_ids,
      });
      if (this._draft !== draft) return;
      this._cancelEditing(false);
      this._message = "Floor plan saved.";
    } catch (error) {
      if (this._draft !== draft) return;
      this._message = error?.message || "Could not save the floor plan. Reload and try again.";
    } finally {
      this._saving = false;
      this._render();
    }
  }

  _ensureDom() {
    if (this._svg) return;
    const root = this.shadowRoot || this.attachShadow({ mode: "open" });
    root.innerHTML = `
      <style>
        :host { display:block; } [hidden] { display:none !important; }
        ha-card { display:block; overflow:hidden; }
        .header,.toolbar,.palette,.message { padding:12px 16px; }
        .header { display:flex; justify-content:space-between; align-items:center; }
        .toolbar button,.palette button { margin:2px; }
        .message { color:var(--secondary-text-color); }
        svg { display:block; width:100%; height:calc(100dvh - 112px); min-height:320px; background:var(--card-background-color); background-image:radial-gradient(var(--divider-color) .6px, transparent .7px); background-size:12px 12px; touch-action:auto; }
        .edge { stroke:var(--primary-color); stroke-width:.35; }
        .edge.editable { cursor:pointer; stroke-width:.55; }
        .edge.preview { stroke-dasharray:1 1; pointer-events:none; }
        .link-preview-dot { fill:var(--primary-color); pointer-events:none; }
        .room rect { fill:var(--secondary-background-color); stroke:var(--primary-text-color); stroke-width:.28; }
        .room.active rect { stroke:var(--success-color, #4caf50); stroke-width:.55; }
        .room text { fill:var(--primary-text-color); font-size:1.35px; pointer-events:none; }
        .link-source,.room .resize { fill:var(--primary-color); cursor:crosshair; }
        .sensor { cursor:default; }
        svg.editing .room > rect,svg.editing .sensor,svg.editing .sensor *,svg.editing .link-source { touch-action:none; }
        svg.editing .room > rect,svg.editing .sensor { cursor:move; }
        svg.editing .room > .resize { cursor:crosshair; }
        .sensor circle { fill:var(--disabled-text-color); stroke:var(--primary-text-color); stroke-width:.2; }
        .sensor.active circle { fill:var(--success-color, #4caf50); }
        .sensor.unavailable circle { fill:var(--error-color); }
        .sensor.fallback path { display:none; }
        .sensor path { fill:none; stroke:var(--primary-text-color); stroke-width:.18; }
        .palette { border-top:1px solid var(--divider-color); }
        .warning { color:var(--warning-color); }
      </style>
      <ha-card>
        <div class="header"><span></span><button data-action="edit">Edit plan</button><button data-action="cancel" hidden>Cancel</button></div>
        <div class="message" role="status" hidden></div>
        <div class="toolbar" hidden>Add unplaced rooms and sensors from the palette, then drag room bodies, corner handles, sensor markers, or room connection dots. Tap a connector to remove it. Swipe blank canvas to scroll. <button data-action="save">Save</button></div>
        <svg><g data-layer="edges"></g><g data-layer="rooms"></g></svg>
        <div class="palette" data-palette hidden></div>
        <div class="palette warning" data-warning hidden></div>
      </ha-card>
    `;
    this._svg = root.querySelector("svg");
    for (const [type, method] of [["pointerdown", "_onPointerDown"], ["pointermove", "_onPointerMove"], ["pointerup", "_onPointerUp"], ["pointercancel", "_cancelDrag"], ["lostpointercapture", "_cancelDrag"]]) {
      this._svg.addEventListener(type, (event) => this[method](event, this._editModel));
    }
    root.addEventListener("click", (event) => this._onClick(event));
  }

  _onClick(event) {
    const target = event.target.closest?.("button,[data-edge]");
    if (!target || this._saving) return;
    const model = this._editModel || this._model();
    if (target.dataset.action === "edit") { this._beginEditing(model); return; }
    if (!this._editing || this._hass.user?.is_admin !== true) return;
    if (target.dataset.action === "cancel") { this._cancelEditing(); return; }
    if (target.dataset.action === "save") { void this._save(model); return; }
    if (target.dataset.roomSelect) this._placeUnplacedRoom(target.dataset.roomSelect, model);
    if (target.dataset.sensorSelect) this._placeUnplacedSensor(target.dataset.sensorSelect, target.dataset.sensorRoom);
    if (target.dataset.forgetArea) this._draft.forget_area_ids.push(target.dataset.forgetArea);
    if (target.dataset.forgetSensor) this._draft.forget_sensor_registry_ids.push(target.dataset.forgetSensor);
    if (target.dataset.edge) {
      const [left, right] = target.dataset.edge.split("|");
      this._draft.edges = this._draft.edges.filter((edge) => !(edge[0] === left && edge[1] === right));
    }
    this._render();
  }

  _patchMarkup(parent, markup) {
    const fragment = parent.namespaceURI === "http://www.w3.org/2000/svg"
      ? parent.ownerDocument.createElementNS(parent.namespaceURI, "svg")
      : parent.ownerDocument.createElement("div");
    fragment.innerHTML = markup;
    this._patchChildren(parent, fragment);
  }

  _patchChildren(parent, desired) {
    const key = (node, index) => {
      for (const name of ["data-sensor", "data-link-source", "data-edge", "data-action", "data-room-select", "data-sensor-select", "data-forget-area", "data-forget-sensor", "data-room"]) {
        if (node.hasAttribute?.(name)) return `${node.nodeName}:${name}:${node.getAttribute(name)}`;
      }
      return `${node.nodeName}:${index}`;
    };
    const existing = new Map([...parent.childNodes].map((node, index) => [key(node, index), node]));
    [...desired.childNodes].forEach((candidate, index) => {
      const identity = key(candidate, index);
      let node = existing.get(identity);
      existing.delete(identity);
      if (!node) {
        node = candidate.cloneNode(true);
      } else if (!node.isEqualNode(candidate)) {
        if (node.nodeType === 3) {
          node.data = candidate.data;
        } else {
          for (const attribute of [...node.attributes]) {
            if (!candidate.hasAttribute(attribute.name)) node.removeAttribute(attribute.name);
          }
          for (const attribute of candidate.attributes) {
            if (node.getAttribute(attribute.name) !== attribute.value) node.setAttribute(attribute.name, attribute.value);
          }
          this._patchChildren(node, candidate);
        }
      }
      if (parent.childNodes[index] !== node) parent.insertBefore(node, parent.childNodes[index] || null);
    });
    for (const node of existing.values()) node.remove();
  }

  _render(model = this._editModel || this._model()) {
    if (!this._connected || !this._hass || !this._config) return;
    this._ensureDom();
    const root = this.shadowRoot;
    const admin = this._hass.user?.is_admin === true;
    root.querySelector(".header span").textContent = this._config.title || `${this._config.floor_id} floor plan`;
    root.querySelector("[data-action=edit]").hidden = !admin || Boolean(this._editing) || Boolean(model.error);
    root.querySelector("[data-action=cancel]").hidden = !this._editing;
    root.querySelector("[data-action=cancel]").disabled = Boolean(this._saving);
    root.querySelector("[data-action=save]").disabled = Boolean(this._saving || this._drag);
    root.querySelector(".toolbar").hidden = !this._editing;
    const message = model.error || this._message || "";
    root.querySelector(".message").textContent = message;
    root.querySelector(".message").hidden = !message;
    this._svg.setAttribute("class", this._editing ? "editing" : "");
    this._svg.setAttribute("aria-label", `${this._config.floor_id} floor plan`);
    if (model.error) {
      this._patchMarkup(this._svg.querySelector("[data-layer=edges]"), "");
      this._patchMarkup(this._svg.querySelector("[data-layer=rooms]"), "");
      root.querySelector("[data-palette]").hidden = true;
      root.querySelector("[data-warning]").hidden = true;
      return;
    }
    const geometry = this._floorGeometry(model);
    const rectangles = geometry.rectangles;
    const edges = this._editing ? this._draft.edges : model.plan.edges || [];
    const renderedEdges = edges.filter((edge) => rectangles[edge[0]] && rectangles[edge[1]]).map((edge) => {
      const left = rectangles[edge[0]]; const right = rectangles[edge[1]];
      const points = this._edgePoints(left, right).map((item) => `${item.x},${item.y}`).join(" ");
      return `<polyline class="edge ${this._editing ? "editable" : ""}" data-edge="${escapeHtml(edge.join("|"))}" fill="none" points="${points}" />`;
    }).join("");
    const renderedRooms = model.floor.rooms.filter((room) => rectangles[room.area_id]).map((room) => {
      const rectangle = rectangles[room.area_id];
      const sensorMarkup = (room.sensors || []).map((sensor) => {
        const marker = this._editing ? this._draft.sensors[sensor.registry_id] : sensor.marker;
        if (!marker || marker.area_id !== room.area_id) return "";
        const x = rectangle.x + rectangle.width * marker.x / 1000;
        const y = rectangle.y + rectangle.height * marker.y / 1000;
        const sensorTitle = `${this._sensorName(sensor)}: ${sensor.state}`;
        return `<g class="sensor ${escapeHtml(sensor.kind)} ${escapeHtml(sensor.state)}${this._editing ? " editable" : ""}" data-sensor="${escapeHtml(sensor.registry_id)}" transform="translate(${x} ${y})" style="cursor:${this._editing ? "move" : "default"}" tabindex="0" role="img" aria-label="${escapeHtml(sensorTitle)}"><circle r="0.8" /><path d="M1.2,-1.2 A1.7,1.7 0 0 1 1.2,1.2" /><title>${escapeHtml(sensorTitle)}</title></g>`;
      }).join("");
      const occupancy = (room.sensors || []).some((sensor) => sensor.state === "active") ? "active" : "inactive";
      const editingAffordances = this._editing ? `<circle class="link-source" data-link-source="${escapeHtml(room.area_id)}" cx="${rectangle.x + rectangle.width}" cy="${rectangle.y + rectangle.height / 2}" r="0.7" /><rect class="resize" data-room="${escapeHtml(room.area_id)}" data-resize="true" x="${rectangle.x + rectangle.width - 0.8}" y="${rectangle.y + rectangle.height - 0.8}" width="0.8" height="0.8" />` : "";
      return `<g class="room ${occupancy}" data-room="${escapeHtml(room.area_id)}"><rect x="${rectangle.x}" y="${rectangle.y}" width="${rectangle.width}" height="${rectangle.height}" rx="0.8" /><text x="${rectangle.x + 1}" y="${rectangle.y + 2.1}">${escapeHtml(room.name)}</text>${editingAffordances}${sensorMarkup}</g>`;
    }).join("");
    const unplacedRooms = model.floor.rooms.filter((room) => !rectangles[room.area_id]);
    const unplacedSensors = model.floor.rooms.flatMap((room) => (room.sensors || []).filter((sensor) => !(this._editing ? this._draft.sensors[sensor.registry_id] : sensor.marker)).map((sensor) => ({ ...sensor, area_id: room.area_id, room_name: room.name })));
    const renderedUnplacedSensors = unplacedSensors.map((sensor) => {
      const roomIsPlaced = Boolean(rectangles[sensor.area_id]);
      const sensorName = this._sensorName(sensor);
      const title = roomIsPlaced
        ? `Place ${sensorName} in ${sensor.room_name}`
        : `Place ${sensor.room_name} before adding ${sensorName}`;
      return `<button data-sensor-select="${escapeHtml(sensor.registry_id)}" data-sensor-room="${escapeHtml(sensor.area_id)}" title="${escapeHtml(title)}" ${roomIsPlaced ? "" : "disabled"}>${escapeHtml(sensorName)}</button>`;
    }).join("");
    this._svg.setAttribute("viewBox", `0 0 48 ${geometry.height}`);
    this._patchMarkup(this._svg.querySelector("[data-layer=edges]"), renderedEdges);
    this._patchMarkup(this._svg.querySelector("[data-layer=rooms]"), renderedRooms);
    const palette = root.querySelector("[data-palette]");
    palette.hidden = !this._editing;
    if (this._editing) this._patchMarkup(palette, `<strong>Unplaced rooms</strong><br>${unplacedRooms.map((room) => `<button data-room-select="${escapeHtml(room.area_id)}">${escapeHtml(room.name)}</button>`).join("") || "All rooms are placed."}<br><strong>Unplaced occupancy sensors</strong><br>${renderedUnplacedSensors || "All discovered sensors are placed."}`);
    const warning = root.querySelector("[data-warning]");
    warning.hidden = !(model.plan.orphaned_rooms?.length || model.plan.orphaned_sensors?.length);
    if (!warning.hidden) this._patchMarkup(warning, `Saved unavailable rooms: ${escapeHtml((model.plan.orphaned_rooms || []).join(", ") || "none")}. Saved unavailable sensors: ${escapeHtml((model.plan.orphaned_sensors || []).join(", ") || "none")}.${this._editing ? `<br>${(model.plan.orphaned_rooms || []).map((areaId) => `<button data-forget-area="${escapeHtml(areaId)}">Forget ${escapeHtml(areaId)}</button>`).join("")}${(model.plan.orphaned_sensors || []).map((registryId) => `<button data-forget-sensor="${escapeHtml(registryId)}">Forget sensor</button>`).join("")}` : ""}`);
  }
}

customElements.define("adaptive-robovacs-global", AdaptiveRoboVacsGlobalCard);
customElements.define("adaptive-robovacs-vacuum", AdaptiveRoboVacsVacuumCard);
customElements.define("adaptive-robovacs-room", AdaptiveRoboVacsRoomCard);
customElements.define("adaptive-robovacs-floorplan-editor", AdaptiveRoboVacsFloorPlanEditor);
customElements.define("adaptive-robovacs-floorplan", AdaptiveRoboVacsFloorPlanCard);

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
  {
    type: "adaptive-robovacs-floorplan",
    name: "Adaptive RoboVacs Floor Plan",
    description: "Visual room layout, occupancy sensors, and direct room links for one floor.",
  },
].forEach((card) => {
  if (!window.customCards.some((registered) => registered.type === card.type)) {
    window.customCards.push({ ...card, documentationURL: DOCUMENTATION_URL });
  }
});
