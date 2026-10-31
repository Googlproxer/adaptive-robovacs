// Offline, synthetic-only input for the actual shipped custom element.
window.runDashboardBenchmark = async ({ dimensions, samples, warmup, setup }) => {
  const frame = () => new Promise(resolve => requestAnimationFrame(resolve));
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const plan = {
    revision: 1, edges: [], orphaned_rooms: [], orphaned_sensors: [],
    floors: Array.from({ length: dimensions.floors || 2 }, (_, index) => ({ floor_id: `synthetic-${index}`, rooms: [] })),
  };
  for (let i = 0; i < dimensions.rooms; i++) {
    const captured = setup?.rooms[i];
    const floor = plan.floors[captured?.floor ?? i % 2];
    const sources = captured?.sensors || [{ kind: 'radar', state: 'off' }];
    floor.rooms.push({
      area_id: `synthetic-room-${i}`, name: `Room ${i}`,
      rectangle: { floor_id: floor.floor_id, ...(captured?.rectangle || { x: (Math.floor(i / 2) % 4) * 11 + 3, y: Math.floor(i / 8) * 11 + 3, width: 8, height: 8 }) },
      sensors: sources.map((sensor, index) => ({ registry_id: `synthetic-sensor-${i}-${index}`, entity_id: `binary_sensor.synthetic_${i}_${index}`, kind: sensor.kind, state: sensor.state === 'on' ? 'active' : sensor.state === 'off' ? 'inactive' : 'unavailable', marker: { area_id: `synthetic-room-${i}`, x: 500, y: 500 } })),
    });
    if (floor.rooms.length > 1) plan.edges.push([floor.rooms.at(-2).area_id, floor.rooms.at(-1).area_id]);
  }
  if (setup) plan.edges = setup.edges.map(([left, right]) => [`synthetic-room-${left}`, `synthetic-room-${right}`]);
  const integrationEntities = setup?.integration_entities || dimensions.rooms * 28 + dimensions.robots * 16 + 8;
  const attributes = { adaptive_robovacs_entry_id: 'benchmark', adaptive_robovacs_role: 'scheduler_status', floor_plan: plan };
  const states = { 'sensor.synthetic_scheduler': { state: 'ready', attributes } };
  for (let i = 0; i < dimensions.entities - 1; i++) states[`sensor.synthetic_unrelated_${i}`] = { state: '0', attributes: i < integrationEntities - 1 ? { adaptive_robovacs_entry_id: 'benchmark', adaptive_robovacs_role: 'benchmark_control', area_id: `synthetic-room-${i % dimensions.rooms}` } : {} };
  const noiseId = `sensor.synthetic_unrelated_${dimensions.entities - 2}`;
  let hass = { states, user: { is_admin: false }, callService() { throw new Error('Benchmark attempted a service call'); } };
  let cpu = 0, renders = 0;
  const cards = plan.floors.map(floor => {
    const card = document.createElement('adaptive-robovacs-floorplan');
    for (const method of ['_flushRefresh']) {
      const original = card[method].bind(card);
      card[method] = (...args) => { const start = performance.now(); try { return original(...args); } finally { cpu += performance.now() - start; } };
    }
    const render = card._render.bind(card);
    card._render = (...args) => { renders++; return render(...args); };
    card.setConfig({ floor_id: floor.floor_id });
    document.querySelector('main').append(card);
    card.hass = hass;
    return card;
  });
  await frame(); await frame();
  check(cards.every(card => card.shadowRoot.querySelector('svg')), 'Floorplan did not mount');
  const roomNode = cards[0].shadowRoot.querySelector('[data-layer=rooms] > [data-room]');
  const changedRoom = plan.floors.flatMap(floor => floor.rooms).find(room => room.sensors.length > 0);
  check(changedRoom, 'Setup needs at least one occupancy sensor');
  const results = {};
  let sequence = 0;
  for (const workload of ['unrelated_update', 'occupancy_update', '100_update_burst']) {
    const timings = [], frames = [];
    let measuredRenders = 0;
    for (let sample = -warmup; sample < samples; sample++) {
      const updates = [];
      for (let index = 0; index < (workload === '100_update_burst' ? 100 : 1); index++) {
        sequence++;
        const nextPlan = structuredClone(plan);
        if (workload !== 'unrelated_update') nextPlan.floors.flatMap(floor => floor.rooms).find(room => room.area_id === changedRoom.area_id).sensors[0].state = (sample + warmup) % 2 ? 'active' : 'inactive';
        // HA creates a new states object while reusing unchanged entity states.
        const nextStates = { ...states, [noiseId]: { state: String(sequence), attributes: {} } };
        if (workload !== 'unrelated_update') nextStates['sensor.synthetic_scheduler'] = { state: 'ready', attributes: { ...attributes, floor_plan: nextPlan } };
        updates.push({ ...hass, states: nextStates });
      }
      const oldRenders = renders;
      cpu = 0;
      const start = performance.now();
      const setterStart = performance.now();
      for (const update of updates) for (const card of cards) card.hass = update;
      cpu += performance.now() - setterStart;
      await frame();
      const layoutStart = performance.now();
      void document.body.offsetHeight;
      cpu += performance.now() - layoutStart;
      const elapsed = performance.now() - start;
      if (sample >= 0) { timings.push(cpu); frames.push(elapsed); measuredRenders += renders - oldRenders; }
      check(cards[0].shadowRoot.querySelector('[data-layer=rooms] > [data-room]') === roomNode, 'Live update replaced room nodes');
    }
    check(workload !== 'unrelated_update' || measuredRenders === 0, 'Unrelated updates caused rendering');
    check(workload === 'unrelated_update' || measuredRenders === samples, 'Changed updates were not rendered once per frame');
    results[workload] = { cpu_samples_ms: timings, max_frame_latency_ms: Math.max(...frames), renders: measuredRenders, cards: cards.length, integration_entities: integrationEntities };
  }
  return results;
};
