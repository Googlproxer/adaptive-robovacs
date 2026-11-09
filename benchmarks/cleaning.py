"""Offline active-clean and sequential telemetry benchmarks; synthetic data only."""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import tempfile
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType
from unittest.mock import patch

from homeassistant.core import Event, State

from benchmarks.__main__ import NOW, measure, synthetic_application
from benchmarks.report import environment, finish, positive_int
from custom_components.adaptive_robovacs.commands import EvaluateCommand
from custom_components.adaptive_robovacs.models import (
    CleaningOperation,
    EvaluationCause,
    EvaluationMode,
    JobPhase,
    JobSource,
)
from custom_components.adaptive_robovacs.state import ActiveJob, OccupancySample
from custom_components.adaptive_robovacs.watch import WatchSpecification
from tests.ha_runtime import async_test_home_assistant


async def run(args) -> int:
    dimensions = {"rooms": 17, "robots": 2, "entities": 4000, "history": 800}
    clock = [NOW]
    results = []
    with tempfile.TemporaryDirectory(prefix="adaptive-clean-benchmark-") as directory:
        async with async_test_home_assistant(config_dir=directory) as hass:
            with (
                patch(
                    "custom_components.adaptive_robovacs.application.core._now",
                    side_effect=lambda: clock[0],
                ),
                patch(
                    "custom_components.adaptive_robovacs.projections._now",
                    side_effect=lambda: clock[0],
                ),
                patch(
                    "homeassistant.core.ServiceRegistry.async_call",
                    side_effect=AssertionError("benchmark attempted a service call"),
                ),
            ):
                app = await synthetic_application(hass, dimensions)
                try:
                    for history in app.state.room_history.values():
                        for index, sample in enumerate(history.occupancy_samples):
                            sample.started_at = NOW - timedelta(
                                days=55 * (index + 1) / dimensions["history"]
                            )
                    robot = next(iter(app.discovery.robots.values()))
                    timer = "sensor.synthetic_cleaning_time"
                    robot = replace(
                        robot,
                        profile=replace(robot.profile, cleaning_time_entity_id=timer),
                    )
                    app.discovery = replace(
                        app.discovery,
                        robots=MappingProxyType(
                            {**app.discovery.robots, robot.entity_id: robot}
                        ),
                    )
                    room = next(
                        item
                        for item in app.discovery.rooms.values()
                        if item.floor_id == robot.floor_id
                    )
                    hass.states.async_set(robot.entity_id, "cleaning")
                    job = ActiveJob(
                        room_id=room.area_id,
                        room_ids=[room.area_id],
                        operation=CleaningOperation.VACUUM,
                        requested_operations=[CleaningOperation.VACUUM],
                        started_at=NOW - timedelta(minutes=2),
                        observed_started_at=NOW - timedelta(minutes=2),
                        last_observed_at=NOW,
                        seen_cleaning=True,
                        phase=JobPhase.CLEANING,
                        source=JobSource.MANUAL_HOME_ASSISTANT,
                        expected_minutes=240,
                        expected_end=NOW + timedelta(minutes=238),
                        timer_start=0,
                        native_timer_progress_seen=True,
                    )
                    app.state.active_jobs[robot.registry_id] = job
                    app._watch_specifications[timer] = WatchSpecification(timer)
                    # Activate any one-time learning migration before restoring
                    # the synthetic history. Baseline and candidate must retain
                    # the same sample count throughout measured invocations.
                    retained_history = {
                        area: list(history.occupancy_samples)
                        for area, history in app.state.room_history.items()
                    }
                    app._observe_occupancy(clock[0])
                    for area, samples in retained_history.items():
                        app.state.room_history[area].occupancy_samples = samples
                    await app.commands.async_start()
                    command = EvaluateCommand(
                        mode=EvaluationMode.PREVIEW,
                        cause=EvaluationCause.STATE_CHANGE,
                    )

                    async def evaluation():
                        clock[0] += timedelta(seconds=1)
                        await app.async_execute(command)
                        assert app.state.active_jobs[robot.registry_id] is job
                        assert job.last_observed_at == clock[0]

                    # Await every event separately: same-turn command coalescing
                    # must not hide the cost of sustained native timer traffic.
                    tasks = []

                    def create_task(coro, *, name=None):
                        task = asyncio.create_task(coro, name=name)
                        tasks.append(task)
                        return task

                    app._async_create_task = create_task

                    async def telemetry():
                        before = sum(app.metrics.evaluations.values())
                        writes = app.storage.writes
                        publications = app.metrics.snapshot_publications
                        for _ in range(16):
                            checkpoint_writes = app.storage.writes
                            old = (clock[0] - NOW).total_seconds() / 60 + 2
                            clock[0] += timedelta(seconds=1)
                            new = (clock[0] - NOW).total_seconds() / 60 + 2
                            event = Event(
                                "state_changed",
                                {
                                    "entity_id": timer,
                                    "old_state": State(timer, str(old)),
                                    "new_state": State(
                                        timer, str(new), {"unit_of_measurement": "min"}
                                    ),
                                },
                            )
                            app._on_state_changed(event)
                            await tasks.pop()
                            assert app.storage.writes > checkpoint_writes, (
                                "a timer update lost its durable checkpoint"
                            )
                        assert app.state.active_jobs[robot.registry_id] is job
                        assert job.last_observed_at == clock[0]
                        assert app.storage.writes - writes >= 16, (
                            "a progress update lost its durable checkpoint"
                        )
                        telemetry.counts = {
                            "evaluations": sum(app.metrics.evaluations.values())
                            - before,
                            "store_writes": app.storage.writes - writes,
                            "publications": app.metrics.snapshot_publications
                            - publications,
                        }

                    await evaluation()
                    for name, operation, budget in (
                        ("active_evaluation", evaluation, 150),
                        ("16_sequential_timer_updates", telemetry, 500),
                    ):
                        print(name, flush=True)
                        row = {
                            "scenario": "history_heavy",
                            "workload": name,
                            "dimensions": dimensions,
                            "budget_ms": budget,
                            **await measure(operation, args.samples, args.warmup),
                        }
                        if operation is telemetry:
                            row["last_invocation_counts"] = telemetry.counts
                            # Outside timing: the real validation boundary must
                            # not reconstruct unchanged vacancy history again.
                            with patch.object(
                                OccupancySample,
                                "from_mapping",
                                wraps=OccupancySample.from_mapping,
                            ) as decode:
                                await telemetry()
                                assert decode.call_count == 0, (
                                    "timer progress decoded unchanged occupancy history"
                                )
                                row["unchanged_occupancy_samples_decoded"] = (
                                    decode.call_count
                                )
                            row["history_cache_verification_counts"] = telemetry.counts
                        results.append(row)
                finally:
                    app.begin_shutdown()
                    await app.async_shutdown()
                    await hass.async_stop()
    env = environment()
    env["homeassistant"] = importlib.metadata.version("homeassistant")
    return finish(args, "cleaning", env, results)


if __name__ == "__main__":
    options = argparse.ArgumentParser(description=__doc__)
    options.add_argument("--samples", type=positive_int, default=30)
    options.add_argument("--warmup", type=positive_int, default=5)
    options.add_argument("--output", type=Path, required=True)
    options.add_argument("--baseline", type=Path)
    options.add_argument("--check", action="store_true")
    raise SystemExit(asyncio.run(run(options.parse_args())))
