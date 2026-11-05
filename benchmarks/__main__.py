"""Run real scheduler code against an isolated, synthetic Home Assistant."""

from __future__ import annotations

import asyncio
import cProfile
import gc
import importlib.metadata
import json
import pstats
import tempfile
import tracemalloc
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter_ns, process_time_ns
from unittest.mock import patch

from homeassistant.components.vacuum.const import VacuumEntityFeature
from homeassistant.core import Event, State
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import label_registry as lr
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_test_home_assistant,
)

from benchmarks.report import environment, finish, parser, statistics_ms
from benchmarks.setup_case import cases
from custom_components.adaptive_robovacs.application import SchedulerApplication
from custom_components.adaptive_robovacs.commands import EvaluateCommand
from custom_components.adaptive_robovacs.const import DOMAIN, LABEL_BEDROOM, LABEL_RADAR
from custom_components.adaptive_robovacs.discovery import async_discover
from custom_components.adaptive_robovacs.models import (
    AdjacencyMode,
    CleaningOperation,
    EvaluationCause,
    EvaluationMode,
    WaterReadiness,
)
from custom_components.adaptive_robovacs.projections import build_snapshot
from custom_components.adaptive_robovacs.snapshots import SnapshotDelta
from custom_components.adaptive_robovacs.state import (
    DurationSample,
    FloorPlanRectangle,
    OccupancySample,
    RobotSettings,
    RoomSettings,
    SchedulerState,
)
from custom_components.adaptive_robovacs.watch import WatchSpecification

NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)
ENTRY_DATA = {"observe_only": True, "forecast_confidence": 75}


class MemoryStore:
    """Retain production validation, excluding disk and executor latency."""

    def __init__(self) -> None:
        self.writes = 0

    async def async_save(self, state: SchedulerState, *, payload=None) -> None:
        if payload is None:
            state.encode()
        else:
            SchedulerState._validate_current_schema(payload)
        self.writes += 1


async def synthetic_application(
    hass, dimensions: dict, setup=None
) -> SchedulerApplication:
    """Populate actual HA registries outside every timed region."""
    now = datetime.fromisoformat(setup["at"]) if setup else NOW
    entry_data = {**ENTRY_DATA, **(setup.get("global_settings", {}) if setup else {})}
    entry_data["observe_only"] = True
    entry = MockConfigEntry(domain=DOMAIN, entry_id="benchmark", data=entry_data)
    entry.add_to_hass(hass)
    await hass.config.async_set_time_zone(setup["timezone"] if setup else "UTC")
    floors = [
        fr.async_get(hass).async_create(f"Synthetic floor {i}")
        for i in range(dimensions.get("floors", 2))
    ]
    radar_label = lr.async_get(hass).async_create(LABEL_RADAR)
    bedroom_label = lr.async_get(hass).async_create(LABEL_BEDROOM)
    areas = [
        ar.async_get(hass).async_create(
            f"Synthetic room {i:03}",
            floor_id=floors[setup["rooms"][i]["floor"] if setup else i % 2].floor_id,
            labels={bedroom_label.label_id}
            if setup and setup["rooms"][i].get("bedroom")
            else set(),
        )
        for i in range(dimensions["rooms"])
    ]
    registry = er.async_get(hass)
    for i in range(dimensions["robots"]):
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={("benchmark", f"robot-{i}")},
            name=f"Synthetic robot {i}",
        )
        floor_index = setup["robots"][i]["floor"] if setup else i % 2
        dock = next(
            area for area in areas if area.floor_id == floors[floor_index].floor_id
        )
        dr.async_get(hass).async_update_device(device.id, area_id=dock.id)
        robot = registry.async_get_or_create(
            "vacuum", "benchmark", f"robot-{i}", config_entry=entry, device_id=device.id
        )
        hass.states.async_set(
            robot.entity_id,
            "docked",
            {
                "friendly_name": f"Synthetic robot {i}",
                "supported_features": int(VacuumEntityFeature.CLEAN_AREA),
                "fan_speed_list": setup["robots"][i]["fan_speed_options"]
                if setup
                else [],
            },
        )
        battery = registry.async_get_or_create(
            "sensor",
            "benchmark",
            f"battery-{i}",
            config_entry=entry,
            device_id=device.id,
        )
        hass.states.async_set(
            battery.entity_id,
            str(setup["robots"][i]["battery"] if setup else 100),
            {"device_class": "battery"},
        )
        if setup:
            for j, control in enumerate(setup["robots"][i]["controls"]):
                selector = registry.async_get_or_create(
                    "select",
                    "benchmark",
                    f"robot-{i}-control-{j}",
                    config_entry=entry,
                    device_id=device.id,
                    translation_key=control["kind"],
                )
                hass.states.async_set(
                    selector.entity_id,
                    control["state"],
                    {"options": control["options"]},
                )
    for i, area in enumerate(areas):
        sources = (
            setup["rooms"][i]["sensors"]
            if setup
            else [{"kind": "fallback", "state": "on" if i % 4 == 0 else "off"}]
        )
        for j, source in enumerate(sources):
            sensor = registry.async_get_or_create(
                "binary_sensor", "benchmark", f"occupancy-{i}-{j}", config_entry=entry
            )
            registry.async_update_entity(
                sensor.entity_id,
                area_id=area.id,
                labels={radar_label.label_id} if source["kind"] == "radar" else set(),
            )
            hass.states.async_set(
                sensor.entity_id, source["state"], {"device_class": "occupancy"}
            )
    for i in range(dimensions["entities"] - len(registry.entities)):
        unrelated = registry.async_get_or_create(
            "sensor", "benchmark", f"unrelated-{i}", config_entry=entry
        )
        hass.states.async_set(unrelated.entity_id, "0")
    await hass.async_block_till_done()
    app = SchedulerApplication(hass, entry)
    app.storage = MemoryStore()
    app.discovery = await async_discover(hass)
    assert len(app.discovery.rooms) == dimensions["rooms"]
    assert len(app.discovery.robots) == dimensions["robots"]
    if setup:
        from types import MappingProxyType

        adapted = {}
        for robot, captured in zip(
            app.discovery.robots.values(), setup["robots"], strict=True
        ):
            caps = dict(captured["capabilities"])
            for key in (
                "supported_pass_counts",
                "native_area_pass_counts",
                "vacuum_pass_counts",
                "mop_pass_counts",
                "supported_operations",
            ):
                caps[key] = frozenset(caps[key])
            caps["cleaning_depth_options"] = tuple(caps["cleaning_depth_options"])
            caps["water_readiness"] = WaterReadiness(**caps["water_readiness"])
            capabilities = replace(robot.adapter_capabilities, **caps)
            adapted[robot.entity_id] = replace(
                robot,
                adapter_capabilities=capabilities,
                adapter_id=captured.get("adapter_id", robot.adapter_id),
                adapter_schema_version=captured.get(
                    "adapter_schema_version", robot.adapter_schema_version
                ),
            )
        app.discovery = replace(app.discovery, robots=MappingProxyType(adapted))
    for i, robot in enumerate(app.discovery.robots.values()):
        defaults = app.state.ensure_robot(
            robot.registry_id, robot.profile.supports_mopping
        )
        if setup:
            app.state.robot_settings[robot.registry_id] = RobotSettings.from_mapping(
                setup["robots"][i]["settings"], defaults
            )
        app._ready_since[robot.entity_id] = now - timedelta(hours=1)
    robot_ids = [robot.registry_id for robot in app.discovery.robots.values()]
    for i, room in enumerate(app.discovery.rooms.values()):
        settings, history = app.state.ensure_room(room.area_id, room.is_bedroom)
        captured = setup["rooms"][i] if setup else None
        if captured:
            settings = RoomSettings.from_mapping(captured["settings"], settings)
            app.state.room_settings[room.area_id] = settings
        else:
            settings.ignore_desired_window = True
            settings.adjacency_mode = AdjacencyMode.ALWAYS
        age = captured["last_cleaned_minutes"] if captured else 10 * 24 * 60
        history.cleaning_completed_at = (
            now - timedelta(minutes=age) if age is not None else None
        )
        if setup and setup.get("force_due"):
            history.cleaning_completed_at = now - timedelta(days=365)
            settings.ignore_desired_window = True
        observed = app.observer.room(room)
        history.occupancy = observed.occupancy
        history.occupancy_source = observed.source
        clear = captured["clear_minutes"] if captured else (120 if i % 4 else None)
        if setup and setup.get("force_due") and observed.occupancy == "unoccupied":
            clear = max(clear or 0, 120)
        history.unoccupied_since = (
            now - timedelta(minutes=clear) if clear is not None else None
        )
        count = captured["history_count"] if captured else dimensions["history"]
        history.occupancy_samples = [
            OccupancySample(
                now
                - timedelta(
                    minutes=(j + 1)
                    * (55 * 24 * 60 / max(count, 1) if captured else 240)
                ),
                180 + j % 60,
            )
            for j in range(count)
        ]
        history.duration_samples = [
            DurationSample(
                10.0 + j % 5,
                CleaningOperation.VACUUM,
                1,
                robot_ids[(i % 2 + 2 * j) % len(robot_ids)],
                "observed",
                now,
            )
            for j in range(captured["duration_count"] if captured else 12)
        ]
        app.state.floor_plan.rooms[room.area_id] = FloorPlanRectangle(
            room.floor_id, (i // 2 % 8) * 10, (i // 16) * 10, 8, 8
        )
        if captured and captured.get("rectangle"):
            app.state.floor_plan.rooms[room.area_id] = FloorPlanRectangle(
                room.floor_id, **captured["rectangle"]
            )
    room_ids = list(app.discovery.rooms)
    app.state.floor_plan.edges = set(
        (room_ids[i], room_ids[i + 2]) for i in range(len(room_ids) - 2)
    )
    if setup:
        app.state.floor_plan.edges = {
            tuple(sorted((room_ids[a], room_ids[b]))) for a, b in setup["edges"]
        }
    app._watch_specifications = {
        source.entity_id: WatchSpecification(source.entity_id)
        for room in app.discovery.rooms.values()
        for source in room.occupancy_sources
    }
    return app


async def measure(operation, samples: int, warmup: int) -> dict:
    for _ in range(warmup):
        await operation()
    gc.collect()
    walls, cpus = [], []
    for _ in range(samples):
        start = perf_counter_ns()
        cpu = process_time_ns()
        await operation()
        walls.append((perf_counter_ns() - start) / 1e6)
        cpus.append((process_time_ns() - cpu) / 1e6)
        await asyncio.sleep(0)
    # A continuous heartbeat catches blocking *inside* the command worker too.
    # A single call_soon before an awaited command would miss that work.
    delays = []
    probing = True

    async def heartbeat():
        while probing:
            start = perf_counter_ns()
            await asyncio.sleep(0)
            delays.append((perf_counter_ns() - start) / 1e6)

    probe = asyncio.create_task(heartbeat())
    try:
        await asyncio.sleep(0)
        for _ in range(3):
            await operation()
            await asyncio.sleep(0)
    finally:
        probing = False
        await probe
    # Allocation tracing is deliberately separate from latency samples.
    gc.collect()
    tracemalloc.start()
    try:
        await operation()
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return {
        **statistics_ms(walls),
        "cpu_median_ms": statistics_ms(cpus)["median_ms"],
        "max_event_loop_delay_ms": max(delays),
        "peak_allocated_kib": peak / 1024,
    }


async def run_scenario(args, name, dimensions, setup=None) -> list[dict]:
    now = datetime.fromisoformat(setup["at"]) if setup else NOW
    results = []
    print(f"Preparing {name}: {dimensions}", flush=True)
    with tempfile.TemporaryDirectory(prefix="adaptive-benchmark-") as directory:
        async with async_test_home_assistant(config_dir=directory) as hass:
            with (
                patch(
                    "custom_components.adaptive_robovacs.application.core._now",
                    return_value=now,
                ),
                patch(
                    "homeassistant.core.ServiceRegistry.async_call",
                    side_effect=AssertionError("benchmark attempted a service call"),
                ),
            ):
                app = await synthetic_application(hass, dimensions, setup)
                try:
                    await app.commands.async_start()
                    command = EvaluateCommand(
                        mode=EvaluationMode.PREVIEW, cause=EvaluationCause.SERVICE
                    )

                    async def evaluation():
                        result = await app.async_execute(command)
                        preview = result.as_response()
                        assert setup or len(preview["candidates"]) > 0, (
                            "empty evaluation workload"
                        )
                        assert setup or len(preview["assignments"]) > 0, (
                            f"no eligible robot workload: {preview['robots']}; "
                            f"{preview['candidates'][0]}"
                        )
                        assert not any(app.state.active_jobs.values()), (
                            "preview dispatched a job"
                        )

                    await evaluation()
                    snapshot = build_snapshot(app)
                    changed = replace(
                        snapshot,
                        rooms=(
                            replace(
                                snapshot.rooms[0],
                                occupancy="occupied"
                                if snapshot.rooms[0].occupancy != "occupied"
                                else "unoccupied",
                            ),
                            *snapshot.rooms[1:],
                        ),
                    )
                    payload = app.state.encode()
                    watched = next(iter(app._watch_specifications))
                    old = State(watched, "off", {"noise": 0})
                    events = [
                        Event(
                            "state_changed",
                            {
                                "entity_id": watched
                                if i % 2
                                else "sensor.synthetic_unwatched",
                                "old_state": old,
                                "new_state": State(watched, "off", {"noise": i}),
                            },
                        )
                        for i in range(1000)
                    ]

                    async def discovery():
                        found = await async_discover(hass)
                        assert len(found.rooms) == dimensions["rooms"]

                    async def projection():
                        build_snapshot(app)

                    async def delta():
                        change = SnapshotDelta.between(snapshot, changed)
                        assert len(change.room_ids) == 1

                    async def encode():
                        json.dumps(app.state.encode(), separators=(",", ":"))

                    async def restore():
                        restored, _ = SchedulerState.from_store(payload, ENTRY_DATA)
                        assert len(restored.room_history) == dimensions["rooms"]

                    async def unchanged_save():
                        before = app.storage.writes
                        await app._async_save()
                        assert app.storage.writes == before, (
                            "unchanged state was written"
                        )

                    async def noise():
                        before = app.metrics.state_events["meaningful"]
                        for event in events:
                            app._on_state_changed(event)
                        assert app.metrics.state_events["meaningful"] == before

                    async def burst():
                        before = sum(app.metrics.evaluations.values())
                        await asyncio.gather(
                            *(
                                app.async_execute(replace(command, coalesce=True))
                                for _ in range(100)
                            )
                        )
                        assert sum(app.metrics.evaluations.values()) - before == 1, (
                            "burst was not coalesced"
                        )

                    await app._async_save()
                    for workload, operation, budget in (
                        ("evaluation_preview", evaluation, 50),
                        ("registry_discovery", discovery, 50),
                        ("snapshot_build", projection, 25),
                        ("snapshot_delta_one_room", delta, 5),
                        ("state_encode_json", encode, 25),
                        ("state_restore", restore, 50),
                        ("unchanged_save", unchanged_save, 25),
                        ("1000_noisy_state_events", noise, 10),
                        ("100_coalesced_evaluations", burst, 75),
                    ):
                        print(f"  {workload}", flush=True)
                        results.append(
                            {
                                "scenario": name,
                                "workload": workload,
                                "dimensions": dimensions,
                                "budget_ms": budget,
                                **await measure(operation, args.samples, args.warmup),
                            }
                        )
                    if args.profile:
                        args.profile.mkdir(parents=True, exist_ok=True)
                        profiler = cProfile.Profile()
                        profiler.enable()
                        await evaluation()
                        profiler.disable()
                        profiler.dump_stats(str(args.profile / f"{name}.pstats"))
                        with (args.profile / f"{name}.txt").open(
                            "w", encoding="utf-8"
                        ) as stream:
                            pstats.Stats(
                                profiler, stream=stream
                            ).strip_dirs().sort_stats("cumulative").print_stats(30)
                finally:
                    app.begin_shutdown()
                    await app.async_shutdown()
                    await hass.async_stop()
    return results


async def run(args) -> int:
    results = []
    for name, dimensions, setup in cases(args):
        results.extend(await run_scenario(args, name, dimensions, setup))
    env = environment()
    env["homeassistant"] = importlib.metadata.version("homeassistant")
    return finish(args, "backend", env, results)


def main() -> int:
    options = parser(__doc__)
    options.add_argument(
        "--profile", type=Path, help="Directory for separate CPU profiles"
    )
    return asyncio.run(run(options.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
