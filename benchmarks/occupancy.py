"""Offline normalized robot-location and occupancy checkpoint costs.

Uses the real command queue and synthetic Core registries. Native map parsing,
network/device I/O and scheduler planning are excluded. All service calls fail.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import tempfile
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

from benchmarks.__main__ import NOW, measure, synthetic_application
from benchmarks.report import environment, finish, positive_int
from custom_components.adaptive_robovacs.commands import (
    RobotLocationCommand,
    StateChangedCommand,
)
from custom_components.adaptive_robovacs.models import (
    CleaningOperation,
    JobPhase,
    JobSource,
    OccupancyAttribution,
    RobotLocation,
    RoomObservation,
)
from custom_components.adaptive_robovacs.state import ActiveJob
from tests.ha_runtime import async_test_home_assistant


async def run(args) -> int:
    clock = [NOW]
    dimensions = {"rooms": 17, "robots": 2, "entities": 4000, "history": 800}
    rows = []
    with tempfile.TemporaryDirectory(
        prefix="adaptive-occupancy-benchmark-"
    ) as directory:
        async with async_test_home_assistant(config_dir=directory) as hass:
            with (
                patch(
                    "custom_components.adaptive_robovacs.application.core._now",
                    side_effect=lambda: clock[0],
                ),
                patch(
                    "homeassistant.core.ServiceRegistry.async_call",
                    side_effect=AssertionError("benchmark attempted a service call"),
                ),
            ):
                app = await synthetic_application(hass, dimensions)
                try:
                    robot = next(iter(app.discovery.robots.values()))
                    room = next(
                        room
                        for room in app.discovery.rooms.values()
                        if room.floor_id == robot.floor_id
                        and (room.radar_entity_ids or room.fallback_entity_ids)
                    )
                    sensors = room.radar_entity_ids or room.fallback_entity_ids
                    source = "radars" if room.radar_entity_ids else "motion_fallback"
                    history = app.state.room_history[room.area_id]
                    anchor = NOW - timedelta(hours=4)
                    history.unoccupied_since = anchor
                    history.occupancy = "unoccupied"
                    history.occupancy_source = source
                    history.occupancy_attribution = OccupancyAttribution(
                        raw="unoccupied", vacancy_since=anchor
                    )
                    app.observer.attribution_areas = frozenset({room.area_id})
                    app.observer.location_sources = {
                        robot.registry_id: SimpleNamespace(close=lambda: None)
                    }
                    for entity in sensors:
                        hass.states.async_set(entity, "off")
                    await app.commands.async_start()

                    async def location():
                        await app.async_execute(
                            RobotLocationCommand(
                                RobotLocation(
                                    robot.registry_id, room.area_id, clock[0], True
                                )
                            )
                        )

                    async def occupancy(raw):
                        hass.states.async_set(
                            sensors[0],
                            "on" if raw == "occupied" else "off",
                        )
                        await app.async_execute(
                            StateChangedCommand(
                                sensors[0],
                                None,
                                raw,
                                clock[0],
                                occupancy_observations=(
                                    (room.area_id, RoomObservation(raw, source)),
                                ),
                            )
                        )

                    await location()
                    await occupancy("occupied")
                    if args.cleaning_sessions:
                        dock = next(
                            item
                            for item in app.discovery.rooms.values()
                            if item.floor_id == robot.floor_id
                            and item.area_id != room.area_id
                        )
                        robot = replace(robot, dock_area_id=dock.area_id)
                        app.discovery = replace(
                            app.discovery,
                            robots=MappingProxyType(
                                {**app.discovery.robots, robot.entity_id: robot}
                            ),
                        )
                        app.state.active_jobs[robot.registry_id] = ActiveJob(
                            room.area_id,
                            [room.area_id],
                            CleaningOperation.VACUUM,
                            JobPhase.CLEANING,
                            JobSource.SCHEDULER,
                        )
                        hass.states.async_set(robot.entity_id, "cleaning")
                        await app.async_execute(
                            StateChangedCommand(
                                robot.entity_id,
                                "docked",
                                "cleaning",
                                clock[0],
                                cleaning_room_ids=(room.area_id,),
                            )
                        )
                    counts = (
                        sum(app.metrics.evaluations.values()),
                        app.storage.writes,
                        app.metrics.snapshot_publications,
                    )
                    samples = tuple(history.occupancy_samples)

                    async def heartbeat():
                        clock[0] += timedelta(seconds=1)
                        await app._async_handle_robot_location(
                            RobotLocation(
                                robot.registry_id, room.area_id, clock[0], True
                            )
                        )
                        assert history.unoccupied_since == anchor
                        assert counts == (
                            sum(app.metrics.evaluations.values()),
                            app.storage.writes,
                            app.metrics.snapshot_publications,
                        ), "heartbeat triggered evaluation, storage or publication"

                    async def pulse():
                        clock[0] += timedelta(seconds=1)
                        await location()
                        await occupancy("unoccupied")
                        clock[0] += timedelta(seconds=1)
                        await occupancy("occupied")
                        assert history.unoccupied_since == anchor
                        assert tuple(history.occupancy_samples) == samples, (
                            "robot pulse contaminated forecast samples"
                        )

                    for name, operation in (
                        ("location_heartbeat", heartbeat),
                        ("robot_pulse_checkpoint", pulse),
                    ):
                        print(name, flush=True)
                        rows.append(
                            {
                                "scenario": "history_heavy",
                                "workload": name,
                                "dimensions": dimensions,
                                "budget_ms": 5,
                                **await measure(operation, args.samples, args.warmup),
                            }
                        )
                    rows[0]["evaluations_saves_publications_added"] = [0, 0, 0]
                finally:
                    app.begin_shutdown()
                    await app.async_shutdown()
                    await hass.async_stop()
    env = environment()
    env["homeassistant"] = importlib.metadata.version("homeassistant")
    return finish(
        args, "cleaning_sessions" if args.cleaning_sessions else "occupancy", env, rows
    )


if __name__ == "__main__":
    options = argparse.ArgumentParser(description=__doc__)
    options.add_argument("--samples", type=positive_int, default=100)
    options.add_argument("--warmup", type=positive_int, default=5)
    options.add_argument("--output", type=Path, required=True)
    options.add_argument("--baseline", type=Path)
    options.add_argument("--check", action="store_true")
    options.add_argument("--cleaning-sessions", action="store_true")
    raise SystemExit(asyncio.run(run(options.parse_args())))
