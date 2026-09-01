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

from .adapters.base import AdapterEntityEvidence, AdapterMatchContext
from .adapters.registry import async_resolve_adapter
from .const import (
    LABEL_BEDROOM,
    LABEL_BEDROOM_TRANSIT,
    LABEL_EXCLUDE,
    LABEL_RADAR,
)
from .models import AdapterCapabilities, profile_control_kind


@dataclass(slots=True)
class RobotProfile:
    """Generic controls discovered from a vacuum's device entities."""

    battery_entity_id: str | None = None
    cleaning_time_entity_id: str | None = None
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
        """Return whether a mode control can select both physical operations."""

        normalized = {_normalised_label(option) for option in self.mode_options}
        return bool(
            self.mode_select_entity_id
            and normalized.intersection({"vacuum", "vacuum-only"})
            and normalized.intersection({"mop", "mop-only"})
        )

    @property
    def supports_double_pass(self) -> bool:
        """Return whether a pass-count selection is exposed."""

        return bool(self.passes_select_entity_id)


@dataclass(slots=True)
class DiscoveredRobot:
    """One vacuum and the floor served by its dock area."""

    entity_id: str
    name: str
    registry_id: str
    platform: str
    device_id: str | None
    dock_area_id: str | None
    floor_id: str | None
    supports_area_clean: bool
    supports_send_command: bool
    profile: RobotProfile
    adapter_id: str
    adapter_schema_version: int
    adapter_capabilities: AdapterCapabilities
    adapter_diagnostic: str | None = None
    adapter_entities: tuple[AdapterEntityEvidence, ...] = ()


@dataclass(slots=True)
class DiscoveredRoom:
    """One Home Assistant area eligible for a robot queue."""

    area_id: str
    name: str
    floor_id: str
    labels: set[str]
    radar_entity_ids: tuple[str, ...] = ()
    fallback_entity_ids: tuple[str, ...] = ()
    occupancy_sources: tuple["DiscoveredOccupancySource", ...] = ()

    @property
    def is_bedroom(self) -> bool:
        return LABEL_BEDROOM in self.labels

    @property
    def is_bedroom_transit(self) -> bool:
        return LABEL_BEDROOM_TRANSIT in self.labels


@dataclass(frozen=True, slots=True)
class DiscoveredOccupancySource:
    """One registry-backed occupancy source owned by a scheduler room."""

    registry_id: str
    entity_id: str
    kind: str


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


def _occupancy_labels(
    entry: er.RegistryEntry,
    devices: dr.DeviceRegistry,
    registry: lr.LabelRegistry,
) -> set[str]:
    """Return direct occupancy labels, or inherit the owning device labels.

    An entity's labels are an explicit per-entity override.  Only an unlabelled
    occupancy entity inherits its owning device's labels, which makes a device
    label the convenient default for every radar entity it exposes.
    """

    entity_labels = getattr(entry, "labels", None)
    if entity_labels:
        return _labels_for(entity_labels, registry)
    device = devices.async_get(entry.device_id) if entry.device_id else None
    return _labels_for(getattr(device, "labels", None), registry)


def _state_options(hass: HomeAssistant, entity_id: str) -> tuple[str, ...]:
    state = hass.states.get(entity_id)
    options = state.attributes.get("options", []) if state else []
    return tuple(str(option) for option in options)


def _adapter_evidence(
    hass: HomeAssistant, entries: list[er.RegistryEntry]
) -> tuple[AdapterEntityEvidence, ...]:
    """Build transient, redaction-safe evidence for a vendor adapter."""

    evidence: list[AdapterEntityEvidence] = []
    for entry in entries:
        state = hass.states.get(entry.entity_id)
        attributes = state.attributes if state else {}
        evidence.append(
            AdapterEntityEvidence(
                entity_id=entry.entity_id,
                domain=entry.entity_id.split(".", 1)[0],
                platform=str(entry.platform),
                translation_key=(
                    getattr(entry, "translation_key", None)
                    or attributes.get("translation_key")
                    or attributes.get("entity_description_key")
                ),
                device_class=(
                    attributes.get("device_class")
                    or getattr(entry, "original_device_class", None)
                ),
                state=state.state if state else None,
                options=tuple(
                    str(option) for option in attributes.get("options", [])
                ),
            )
        )
    return tuple(evidence)


def _verified_operation_mode(
    evidence: tuple[AdapterEntityEvidence, ...],
) -> AdapterEntityEvidence | None:
    """Return one explicit same-device selector for vacuum and mop operations."""

    for item in evidence:
        options = {_normalised_label(option) for option in item.options}
        if (
            item.domain == "select"
            and options.intersection({"vacuum", "vacuum-only"})
            and options.intersection(
                {"mop", "mop-only", "vac-and-mop", "vacuum-and-mop"}
            )
        ):
            return item
    return None


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
        if domain == "sensor":
            if device_class == "battery" and not profile.battery_entity_id:
                profile.battery_entity_id = entry.entity_id
                continue
            sensor_name = " ".join(
                str(value or "")
                for value in (state.name if state else None, entry.name, entry.original_name, entry.entity_id)
            ).replace("_", " ").lower()
            if (
                device_class == "duration"
                and not profile.cleaning_time_entity_id
                and ("cleaning time" in sensor_name or "clean time" in sensor_name)
            ):
                profile.cleaning_time_entity_id = entry.entity_id
                continue
        if domain != "select":
            continue

        options = _state_options(hass, entry.entity_id)
        entity_labels = _labels_for(getattr(entry, "labels", None), labels)
        attributes = state.attributes if state else {}
        translation_key = (
            getattr(entry, "translation_key", None)
            or attributes.get("translation_key")
            or attributes.get("entity_description_key")
        )
        kind = profile_control_kind(translation_key, options, entity_labels)

        if kind == "passes" and not profile.passes_select_entity_id:
            profile.passes_select_entity_id = entry.entity_id
            profile.passes_options = options
            continue
        if kind == "mop_intensity" and not profile.mop_intensity_select_entity_id:
            profile.mop_intensity_select_entity_id = entry.entity_id
            profile.mop_intensity_options = options
            continue
        if kind == "mode" and not profile.mode_select_entity_id:
            profile.mode_select_entity_id = entry.entity_id
            profile.mode_options = options
            continue
        if kind == "mop_mode" and not profile.mop_mode_select_entity_id:
            profile.mop_mode_select_entity_id = entry.entity_id
            profile.mop_mode_options = options
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
        supports_area_clean = bool(
            supported_features & int(VacuumEntityFeature.CLEAN_AREA)
        )
        supports_send_command = bool(
            supported_features & int(VacuumEntityFeature.SEND_COMMAND)
        )
        platform = str(entry.platform)
        adapter_entities = _adapter_evidence(
            hass, entries_by_device.get(entry.device_id, [])
        )
        if operation_mode := _verified_operation_mode(adapter_entities):
            profile.mode_select_entity_id = operation_mode.entity_id
            profile.mode_options = operation_mode.options
        context = AdapterMatchContext(
            entity_id=entry.entity_id,
            platform=platform,
            supports_area_clean=supports_area_clean,
            supports_send_command=supports_send_command,
            profile=profile,
            fan_speed_options=tuple(
                str(option)
                for option in (
                    state.attributes.get("fan_speed_list", []) if state else []
                )
            ),
            device_id=entry.device_id,
            entities=adapter_entities,
        )
        adapter, capabilities, adapter_diagnostic = await async_resolve_adapter(
            hass, context
        )
        result.robots[entry.entity_id] = DiscoveredRobot(
            entity_id=entry.entity_id,
            name=_robot_name(entry, devices, hass),
            registry_id=str(
                getattr(entry, "id", None) or f"{platform}:{entry.unique_id}"
            ),
            platform=platform,
            device_id=entry.device_id,
            dock_area_id=dock_area_id,
            floor_id=dock_area.floor_id if dock_area else None,
            supports_area_clean=supports_area_clean,
            supports_send_command=supports_send_command,
            profile=profile,
            adapter_id=adapter.adapter_id,
            adapter_schema_version=adapter.schema_version,
            adapter_capabilities=capabilities,
            adapter_diagnostic=adapter_diagnostic,
            adapter_entities=adapter_entities,
        )

    occupancy_by_area: dict[
        str, tuple[list[str], list[str], list[DiscoveredOccupancySource]]
    ] = {}
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
        radars, fallbacks, sources = occupancy_by_area.setdefault(area_id, ([], [], []))
        occupancy_labels = _occupancy_labels(entry, devices, labels)
        kind = "radar" if LABEL_RADAR in occupancy_labels else "fallback"
        (radars if kind == "radar" else fallbacks).append(entry.entity_id)
        sources.append(
            DiscoveredOccupancySource(
                registry_id=str(
                    getattr(entry, "id", None)
                    or f"{entry.platform}:{entry.unique_id}"
                ),
                entity_id=entry.entity_id,
                kind=kind,
            )
        )

    served_floors = {robot.floor_id for robot in result.robots.values() if robot.floor_id}
    for area in areas.areas.values():
        if not area.floor_id or area.floor_id not in served_floors:
            continue
        area_labels = _labels_for(getattr(area, "labels", None), labels)
        if LABEL_EXCLUDE in area_labels:
            continue
        radars, fallbacks, sources = occupancy_by_area.get(area.id, ([], [], []))
        result.rooms[area.id] = DiscoveredRoom(
            area_id=area.id,
            name=area.name,
            floor_id=area.floor_id,
            labels=area_labels,
            radar_entity_ids=tuple(sorted(radars)),
            fallback_entity_ids=tuple(sorted(fallbacks)),
            occupancy_sources=tuple(sorted(sources, key=lambda source: source.registry_id)),
        )
    return result
