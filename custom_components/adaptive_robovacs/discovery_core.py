"""Discover robots, rooms, and occupancy sources from Home Assistant registries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from homeassistant.components.vacuum import VacuumEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr

from .const import (
    LABEL_BEDROOM,
    LABEL_BEDROOM_TRANSIT,
    LABEL_EXCLUDE,
    LABEL_RADAR,
)


@dataclass(slots=True)
class RobotProfile:
    """Generic controls discovered from a vacuum's device entities."""

    battery_entity_id: str | None = None
    mode_select_entity_id: str | None = None
    mop_mode_select_entity_id: str | None = None
    mop_intensity_select_entity_id: str | None = None
    passes_select_entity_id: str | None = None
    mode_options: tuple[str, ...] = ()
    mop_mode_options: tuple[str, ...] = ()
    mop_intensity_options: tuple[str, ...] = ()
    passes_options: tuple[str, ...] = ()

    @property
    def supports_mopping(self) -> bool:
        """Return whether the device exposes a mopping control."""

        return bool(self.mode_select_entity_id or self.mop_mode_select_entity_id)

    @property
    def supports_double_pass(self) -> bool:
        """Return whether a pass-count selection is exposed."""

        return bool(self.passes_select_entity_id)


@dataclass(slots=True)
class DiscoveredRobot:
    """One vacuum and the floor served by its dock area."""

    entity_id: str
    name: str
    device_id: str | None
    dock_area_id: str | None
    floor_id: str | None
    supports_area_clean: bool
    profile: RobotProfile


@dataclass(slots=True)
class DiscoveredRoom:
    """One Home Assistant area eligible for a robot queue."""

    area_id: str
    name: str
    floor_id: str
    labels: set[str]
    radar_entity_ids: tuple[str, ...] = ()
    fallback_entity_ids: tuple[str, ...] = ()

    @property
    def is_bedroom(self) -> bool:
        return LABEL_BEDROOM in self.labels

    @property
    def is_bedroom_transit(self) -> bool:
        return LABEL_BEDROOM_TRANSIT in self.labels


@dataclass(slots=True)
class DiscoveryResult:
    """A registry snapshot used by the scheduler."""

    robots: dict[str, DiscoveredRobot] = field(default_factory=dict)
    rooms: dict[str, DiscoveredRoom] = field(default_factory=dict)


def _entity_area_id(entry: er.RegistryEntry, devices: dr.DeviceRegistry) -> str | None:
    """Use an entity area first, then the owning device/dock area."""

    if entry.area_id:
        return entry.area_id
    if entry.device_id and (device := devices.async_get(entry.device_id)):
        return device.area_id
    return None


def _robot_name(
    entry: er.RegistryEntry, devices: dr.DeviceRegistry, hass: HomeAssistant
) -> str:
    """Resolve a stable friendly robot name even while its entity is restoring.

    A vacuum can briefly have no state attributes while Home Assistant starts.
    Falling back through its registry and device names prevents scheduler
    entities from being permanently created with ``vacuum.example`` as their
    display name during that short window.
    """

    state = hass.states.get(entry.entity_id)
    device = devices.async_get(entry.device_id) if entry.device_id else None
    candidates = (
        state.name if state and state.name != entry.entity_id else None,
        entry.name,
        device.name_by_user if device else None,
        device.name if device else None,
        entry.original_name,
    )
    return next((str(name) for name in candidates if name), entry.entity_id)


def _normalised_label(name: str) -> str:
    return name.strip().lower().replace(" ", "-").replace("_", "-")


def _labels_for(
    labels: set[str] | frozenset[str] | None, registry: lr.LabelRegistry
) -> set[str]:
    """Return stable label names as well as raw IDs.

    Home Assistant stores label IDs in registries. Accepting the human-facing
    label name keeps the documented configuration independent of generated IDs.
    """

    result = set(labels or ())
    for label_id in tuple(result):
        if label := registry.async_get_label(label_id):
            result.add(_normalised_label(label.name))
    return result


def _state_options(hass: HomeAssistant, entity_id: str) -> tuple[str, ...]:
    state = hass.states.get(entity_id)
    options = state.attributes.get("options", []) if state else []
    return tuple(str(option) for option in options)


def _find_profile(
    hass: HomeAssistant,
    entries: list[er.RegistryEntry],
    labels: lr.LabelRegistry,
) -> RobotProfile:
    """Infer optional battery, mop, and pass controls from the same device."""

    profile = RobotProfile()
    for entry in entries:
        state = hass.states.get(entry.entity_id)
        domain = entry.entity_id.split(".", 1)[0]
        device_class = (
            state.attributes.get("device_class") if state else None
        ) or getattr(entry, "original_device_class", None)
        if domain == "sensor" and device_class == "battery" and not profile.battery_entity_id:
            profile.battery_entity_id = entry.entity_id
            continue
        if domain != "select":
            continue

        options = _state_options(hass, entry.entity_id)
        normalised_options = {_normalised_label(option) for option in options}
        entity_labels = _labels_for(getattr(entry, "labels", None), labels)

        if (
            not profile.passes_select_entity_id
            and (
                {"one-pass", "two-pass"}.issubset(normalised_options)
                or {"single-pass", "double-pass"}.issubset(normalised_options)
                or "robovac-double-pass" in entity_labels
            )
        ):
            profile.passes_select_entity_id = entry.entity_id
            profile.passes_options = options
            continue
        if not profile.mop_intensity_select_entity_id and (
            {"low", "medium", "high"}.issubset(normalised_options)
            and any("water" in option or "mop" in option for option in normalised_options)
        ):
            profile.mop_intensity_select_entity_id = entry.entity_id
            profile.mop_intensity_options = options
            continue
        if not profile.mop_mode_select_entity_id and any(
            "mop" in option or "deep" in option for option in normalised_options
        ):
            profile.mop_mode_select_entity_id = entry.entity_id
            profile.mop_mode_options = options
            continue
        if not profile.mode_select_entity_id and any(
            option in {"vacuum", "vac-and-mop", "mop"}
            for option in normalised_options
        ):
            profile.mode_select_entity_id = entry.entity_id
            profile.mode_options = options
    return profile


async def async_discover(hass: HomeAssistant) -> DiscoveryResult:
    """Discover scheduler candidates without any deployment-specific mapping."""

    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    areas = ar.async_get(hass)
    labels = lr.async_get(hass)
    entries_by_device: dict[str, list[er.RegistryEntry]] = {}
    for entry in entities.entities.values():
        if entry.device_id:
            entries_by_device.setdefault(entry.device_id, []).append(entry)

    result = DiscoveryResult()
    for entry in entities.entities.values():
        if not entry.entity_id.startswith("vacuum."):
            continue
        dock_area_id = _entity_area_id(entry, devices)
        dock_area = areas.async_get_area(dock_area_id) if dock_area_id else None
        state = hass.states.get(entry.entity_id)
        supported_features = int(state.attributes.get("supported_features", 0)) if state else 0
        profile = _find_profile(
            hass, entries_by_device.get(entry.device_id, []), labels
        )
        result.robots[entry.entity_id] = DiscoveredRobot(
            entity_id=entry.entity_id,
            name=_robot_name(entry, devices, hass),
            device_id=entry.device_id,
            dock_area_id=dock_area_id,
            floor_id=dock_area.floor_id if dock_area else None,
            supports_area_clean=bool(
                supported_features & int(VacuumEntityFeature.CLEAN_AREA)
            ),
            profile=profile,
        )

    occupancy_by_area: dict[str, tuple[list[str], list[str]]] = {}
    for entry in entities.entities.values():
        if not entry.entity_id.startswith("binary_sensor."):
            continue
        state = hass.states.get(entry.entity_id)
        device_class = (
            state.attributes.get("device_class") if state else None
        ) or getattr(entry, "original_device_class", None)
        if device_class not in {"occupancy", "motion"}:
            continue
        area_id = _entity_area_id(entry, devices)
        if not area_id:
            continue
        radars, fallbacks = occupancy_by_area.setdefault(area_id, ([], []))
        entity_labels = _labels_for(getattr(entry, "labels", None), labels)
        (radars if LABEL_RADAR in entity_labels else fallbacks).append(entry.entity_id)

    served_floors = {robot.floor_id for robot in result.robots.values() if robot.floor_id}
    for area in areas.areas.values():
        if not area.floor_id or area.floor_id not in served_floors:
            continue
        area_labels = _labels_for(getattr(area, "labels", None), labels)
        if LABEL_EXCLUDE in area_labels:
            continue
        radars, fallbacks = occupancy_by_area.get(area.id, ([], []))
        result.rooms[area.id] = DiscoveredRoom(
            area_id=area.id,
            name=area.name,
            floor_id=area.floor_id,
            labels=area_labels,
            radar_entity_ids=tuple(sorted(radars)),
            fallback_entity_ids=tuple(sorted(fallbacks)),
        )
    return result
