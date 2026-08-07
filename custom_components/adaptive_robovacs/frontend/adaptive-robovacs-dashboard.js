class AdaptiveRoboVacsDashboard extends HTMLElement {
  setConfig(config) {
    this._config = config || {};
  }

  set hass(hass) {
    this._hass = hass;
    const signature = this._entitySignature();
    if (signature !== this._signature) {
      this._signature = signature;
      this._render();
    }
    if (this._card) this._card.hass = hass;
  }

  getCardSize() {
    return 12;
  }

  _entities() {
    if (!this._hass) return [];
    const entryId = this._config.entry_id;
    return Object.entries(this._hass.states)
      .filter(([, state]) => {
        const attrs = state.attributes || {};
        return attrs.adaptive_robovacs_entry_id &&
          (!entryId || attrs.adaptive_robovacs_entry_id === entryId);
      })
      .map(([entity_id, state]) => ({ entity_id, state, attrs: state.attributes || {} }))
      .sort((a, b) => a.entity_id.localeCompare(b.entity_id));
  }

  _entitySignature() {
    return this._entities().map((item) => item.entity_id).join("|");
  }

  _section(title, entities) {
    return entities.length
      ? { type: "entities", title, show_header_toggle: false, entities }
      : null;
  }

  _humanize(value) {
    return String(value || "Unassigned")
      .replace(/[_-]+/g, " ")
      .replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  _roomGroups(rooms) {
    return rooms.flatMap(({ name, entityIds }) => [
      { type: "section", label: name },
      ...entityIds,
    ]);
  }

  _configuration() {
    const entities = this._entities();
    const hiddenAreaIds = new Set(this._config.hidden_area_ids || []);
    const byRole = (role) => entities
      .filter((item) => item.attrs.adaptive_robovacs_role === role)
      .map((item) => item.entity_id);

    const robots = new Map();
    const rooms = new Map();
    entities.forEach((item) => {
      if (item.attrs.robot_entity_id) {
        const robot = item.attrs.robot_entity_id;
        robots.set(robot, [...(robots.get(robot) || []), item.entity_id]);
      }
      if (item.attrs.area_id) {
        const room = item.attrs.area_id;
        rooms.set(room, [...(rooms.get(room) || []), item.entity_id]);
      }
    });

    const schedulerAndRobots = [
      { type: "section", label: "Scheduler" },
      ...byRole("scheduler_status"),
      ...byRole("global_control"),
      ...byRole("scheduler_control"),
    ];
    [...robots.entries()].forEach(([robot, entityIds]) => {
      const state = this._hass.states[robot];
      schedulerAndRobots.push(
        { type: "section", label: state?.attributes?.friendly_name || robot },
        ...entityIds,
      );
    });

    const floors = new Map();
    const bedrooms = [];
    [...rooms.entries()].forEach(([areaId, entityIds]) => {
      if (hiddenAreaIds.has(areaId)) return;
      const schedule = entities.find((item) =>
        item.attrs.area_id === areaId && item.attrs.adaptive_robovacs_role === "room_schedule"
      );
      if (!schedule) return;
      const room = { name: schedule.attrs.room || areaId, entityIds };
      if (schedule.attrs.bedroom) {
        bedrooms.push(room);
        return;
      }
      const floorId = schedule.attrs.floor_id || "unassigned";
      floors.set(floorId, [...(floors.get(floorId) || []), room]);
    });

    const cards = [this._section("Scheduler & robot settings", schedulerAndRobots)];
    [...floors.entries()]
      .sort(([left], [right]) => left.localeCompare(right))
      .forEach(([floorId, roomGroups]) => {
        cards.push(this._section(this._humanize(floorId), this._roomGroups(roomGroups)));
      });
    cards.push(this._section("Bedrooms", this._roomGroups(bedrooms)));

    const requestedColumns = Number(this._config.columns);
    const columns = Number.isInteger(requestedColumns) && requestedColumns > 0
      ? requestedColumns
      : 3;
    return { type: "grid", columns, square: false, cards: cards.filter(Boolean) };
  }

  async _render() {
    if (!this._hass) return;
    const helpers = await window.loadCardHelpers();
    const card = await helpers.createCardElement(this._configuration());
    this._card?.remove();
    this._card = card;
    card.hass = this._hass;
    this.appendChild(card);
  }
}

customElements.define("adaptive-robovacs-dashboard", AdaptiveRoboVacsDashboard);
window.customCards = window.customCards || [];
window.customCards.push({
  type: "adaptive-robovacs-dashboard",
  name: "Adaptive RoboVacs Dashboard",
  description: "Dynamically renders Adaptive RoboVacs controls and room status.",
});
