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
    if (!entities.length) return null;
    return { type: "entities", title, show_header_toggle: false, entities };
  }

  _configuration() {
    const entities = this._entities();
    const byRole = (role) => entities
      .filter((item) => item.attrs.adaptive_robovacs_role === role)
      .map((item) => item.entity_id);
    const cards = [];

    const overview = [
      ...byRole("scheduler_status"),
      ...byRole("global_control"),
      ...byRole("scheduler_control"),
    ];
    cards.push(this._section("Scheduler", overview));

    const robots = new Map();
    const rooms = new Map();
    entities.forEach((item) => {
      const robot = item.attrs.robot_entity_id;
      const area = item.attrs.area_id;
      if (robot) robots.set(robot, [...(robots.get(robot) || []), item.entity_id]);
      if (area) rooms.set(area, [...(rooms.get(area) || []), item.entity_id]);
    });
    robots.forEach((entityIds, robot) => {
      const state = this._hass.states[robot];
      const title = state?.attributes?.friendly_name || robot;
      cards.push(this._section(title, entityIds));
    });
    rooms.forEach((entityIds, areaId) => {
      const schedule = entities.find((item) =>
        item.attrs.area_id === areaId && item.attrs.adaptive_robovacs_role === "room_schedule"
      );
      const title = schedule?.attrs?.room || areaId;
      cards.push(this._section(title, entityIds));
    });
    return { type: "vertical-stack", cards: cards.filter(Boolean) };
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
