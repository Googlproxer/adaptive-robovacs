"""Typed config-entry runtime container for Adaptive RoboVacs."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry

from .application import SchedulerApplication
from .coordinator import AdaptiveRoboVacsCoordinator
from .lifecycle import SchedulerRuntime


@dataclass(slots=True)
class AdaptiveRoboVacsRuntimeData:
    """Objects owned by one loaded config entry."""

    coordinator: AdaptiveRoboVacsCoordinator
    application: SchedulerApplication
    lifecycle: SchedulerRuntime


type AdaptiveRoboVacsConfigEntry = ConfigEntry[AdaptiveRoboVacsRuntimeData]
