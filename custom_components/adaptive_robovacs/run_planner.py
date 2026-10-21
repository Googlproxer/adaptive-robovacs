"""Pure ordered, complete-program admission for managed cleaning runs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .const import EXTRA_CLEAR_MINUTES, FALLBACK_SAMPLE_COUNT
from .models import (
    RUN_TURNAROUND_MINUTES,
    AdjacencyMode,
    Forecast,
    desired_window_allows,
    forecast_vacancy,
    in_daytime_window,
    run_battery_fits,
    run_vacancy_horizon,
)
from .planner import ScheduleCandidate


@dataclass(frozen=True, slots=True)
class RunVacancy:
    occupancy: str
    no_sensor: bool
    clear_since: datetime | None
    samples: tuple[tuple[datetime, float], ...]
    horizon: int
    confidence: float
    fallback_enabled: bool
    fallback_minutes: int

    def forecast(self, now: datetime, duration: float, delay: float) -> Forecast:
        if self.no_sensor:
            return Forecast(True, 1, "no-sensor policy")
        if self.occupancy != "unoccupied":
            return Forecast(False, 0, "room is not clear")
        return forecast_vacancy(
            ({"start": at, "minutes": minutes} for at, minutes in self.samples),
            now,
            self.clear_since,
            run_vacancy_horizon(duration, delay, self.horizon, EXTRA_CLEAR_MINUTES),
            self.confidence,
            FALLBACK_SAMPLE_COUNT,
            fallback_enabled=self.fallback_enabled,
            fallback_minutes=self.fallback_minutes,
        )


@dataclass(frozen=True, slots=True)
class RunRoomOption:
    candidate: ScheduleCandidate
    stage_minutes: tuple[float, ...]
    battery_percent: float | None
    sample_count: int
    vacancy: RunVacancy
    neighbors: tuple[RunVacancy, ...]
    adjacency_mode: AdjacencyMode
    night_start: str
    night_end: str
    window_start: str
    window_end: str
    ignore_window: bool

    @property
    def duration_minutes(self) -> float:
        return sum(self.stage_minutes) + RUN_TURNAROUND_MINUTES * len(
            self.stage_minutes
        )

    def block_reason(
        self, now: datetime, delay: float, *, current: bool = False
    ) -> str | None:
        stage_offset = delay
        for duration in self.stage_minutes:
            start = (now.astimezone(UTC) + timedelta(minutes=stage_offset)).astimezone(
                now.tzinfo
            )
            if not desired_window_allows(
                self.ignore_window, start, self.window_start, self.window_end
            ):
                return "waiting for desired cleaning window"
            adjacency_active = self.adjacency_mode == AdjacencyMode.ALWAYS or (
                self.adjacency_mode == AdjacencyMode.NIGHT_ONLY
                and in_daytime_window(start, self.night_start, self.night_end)
            )
            if adjacency_active:
                for neighbor in self.neighbors:
                    if not neighbor.forecast(now, duration, stage_offset).allowed:
                        return "queued room adjacency is blocked"
            stage_offset += duration + RUN_TURNAROUND_MINUTES
        if not current:
            forecast = self.vacancy.forecast(now, self.duration_minutes, delay)
            if not forecast.allowed:
                return f"queued room vacancy: {forecast.reason}"
        return None


@dataclass(frozen=True, slots=True)
class PlannedCleaningRun:
    robot_id: str
    rooms: tuple[RunRoomOption, ...]

    @property
    def expected_minutes(self) -> float:
        return sum(room.duration_minutes for room in self.rooms)

    @property
    def battery_percent(self) -> float:
        return sum(room.battery_percent or 0 for room in self.rooms)

    def as_attributes(self, now: datetime) -> dict[str, object]:
        """Publish the same cumulative vacancy evidence used for admission."""

        delay = 0.0
        timings: list[dict[str, object]] = []
        for room in self.rooms:
            forecast = room.vacancy.forecast(now, room.duration_minutes, delay)
            timings.append(
                {
                    "room": room.candidate.room_id,
                    "start_at": (
                        now.astimezone(UTC) + timedelta(minutes=delay)
                    ).isoformat(),
                    "finish_at": (
                        now.astimezone(UTC)
                        + timedelta(minutes=delay + room.duration_minutes)
                    ).isoformat(),
                    "required_clear_minutes": forecast.required_minutes,
                    "forecast_confidence": forecast.confidence,
                    "forecast_reason": forecast.reason,
                    "forecast_source": forecast.decision_source,
                    "battery_percent": room.battery_percent,
                }
            )
            delay += room.duration_minutes
        return {
            "robot": self.robot_id,
            "rooms": [room.candidate.room_id for room in self.rooms],
            "expected_minutes": self.expected_minutes,
            "battery_percent": self.battery_percent,
            "battery_reserve_percent": 20,
            "timings": timings,
        }


def run_queue_block(
    rooms: tuple[RunRoomOption, ...],
    now: datetime,
    battery: float | None,
    *,
    current_room_id: str | None = None,
) -> str | None:
    """Validate real-now evidence through each complete delayed program."""

    if any(room.battery_percent is None for room in rooms):
        return "learning battery use (three samples per stage profile required)"
    if not run_battery_fits(battery, sum(room.battery_percent or 0 for room in rooms)):
        return "waiting for recharge to cover the complete run and 20% reserve"
    delay = 0.0
    for room in rooms:
        reason = room.block_reason(
            now, delay, current=room.candidate.room_id == current_room_id
        )
        if reason:
            return reason
        delay += room.duration_minutes
    return None


def build_run_queue(
    options: tuple[RunRoomOption, ...],
    now: datetime,
    battery: float | None,
) -> tuple[tuple[RunRoomOption, ...], tuple[tuple[str, str], ...]]:
    """Append by priority, trying earlier insertion only when necessary."""

    queue: tuple[RunRoomOption, ...] = ()
    blocks: list[tuple[str, str]] = []
    for room in options:
        if room.battery_percent is not None and not run_battery_fits(
            100, room.battery_percent
        ):
            blocks.append(
                (
                    room.candidate.room_id,
                    "complete room program exceeds one charge with 20% reserve",
                )
            )
            continue
        for position in range(len(queue), -1, -1):
            trial = (*queue[:position], room, *queue[position:])
            reason = run_queue_block(trial, now, battery)
            if reason is None:
                queue = trial
                break
        else:
            blocks.append((room.candidate.room_id, reason or "run cannot fit"))
    return queue, tuple(blocks)
