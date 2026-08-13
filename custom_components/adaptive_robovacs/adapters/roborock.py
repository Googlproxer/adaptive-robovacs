"""Roborock enhanced adapter with native two-pass segment cleaning."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from homeassistant.helpers import entity_registry as er

from ..models import AdapterCapabilities, AdapterDispatchRequest, AdapterDispatchResult
from .base import AdapterMatchContext, VacuumAdapter
from .generic import GenericVacuumAdapter


@dataclass(frozen=True, slots=True)
class MappingResolution:
    """Transient normalized native targets; never persist or project this value."""

    targets: tuple[int, ...]


class RoborockMappingError(ValueError):
    """A safe normalized area-mapping failure."""

    def __init__(self, code: str, summary: str) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = summary


def _segment_parts(value: object) -> tuple[str | None, int] | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return None, value
    text = str(value)
    if text.isdecimal():
        return None, int(text)
    pieces = text.split("_", maxsplit=1)
    if len(pieces) == 2 and pieces[0].isdecimal() and pieces[1].isdecimal():
        return pieces[0], int(pieces[1])
    return None


def resolve_roborock_area_mapping(
    vacuum_options: Mapping[str, object], area_ids: Sequence[str]
) -> MappingResolution:
    """Resolve current HA area mappings and fail closed on ambiguous map evidence."""

    mapping = vacuum_options.get("area_mapping")
    last_seen = vacuum_options.get("last_seen_segments")
    if not isinstance(mapping, Mapping):
        raise RoborockMappingError(
            "area_mapping_missing",
            "The room is not mapped to this vacuum in Home Assistant.",
        )
    if not isinstance(last_seen, Sequence) or isinstance(last_seen, (str, bytes)):
        raise RoborockMappingError(
            "area_mapping_stale",
            "Home Assistant has no current segment evidence for this vacuum.",
        )

    last_seen_ids: list[object] = []
    for item in last_seen:
        if isinstance(item, Mapping) and "id" in item:
            last_seen_ids.append(item["id"])
    last_seen_text = {str(value) for value in last_seen_ids}
    if not last_seen_text:
        raise RoborockMappingError(
            "area_mapping_stale",
            "Home Assistant has no current segment evidence for this vacuum.",
        )

    raw_targets: list[object] = []
    for area_id in area_ids:
        values = mapping.get(area_id)
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)) or not values:
            raise RoborockMappingError(
                "area_mapping_missing",
                "The room is not mapped to this vacuum in Home Assistant.",
            )
        raw_targets.extend(values)

    parsed_targets: list[tuple[str | None, int]] = []
    seen_raw: set[str] = set()
    for raw_target in raw_targets:
        raw_text = str(raw_target)
        if raw_text in seen_raw:
            continue
        seen_raw.add(raw_text)
        parsed = _segment_parts(raw_target)
        if parsed is None:
            raise RoborockMappingError(
                "area_mapping_ambiguous",
                "The Home Assistant area mapping uses an unsupported segment format.",
            )
        if raw_text not in last_seen_text:
            raise RoborockMappingError(
                "area_mapping_stale",
                "The room mapping is not present in the vacuum's current segments.",
            )
        parsed_targets.append(parsed)

    target_prefixes = {prefix for prefix, _target in parsed_targets}
    last_seen_parsed = [_segment_parts(value) for value in last_seen_ids]
    if any(item is None for item in last_seen_parsed):
        raise RoborockMappingError(
            "area_mapping_ambiguous",
            "The vacuum reports ambiguous segment-map evidence.",
        )
    last_seen_prefixes = {item[0] for item in last_seen_parsed if item is not None}
    if len(target_prefixes) != 1 or len(last_seen_prefixes) != 1:
        raise RoborockMappingError(
            "area_mapping_ambiguous",
            "The room mapping spans more than one vacuum map.",
        )
    if target_prefixes != last_seen_prefixes:
        raise RoborockMappingError(
            "area_mapping_stale",
            "The room mapping does not match the vacuum's current map.",
        )
    targets = tuple(dict.fromkeys(target for _prefix, target in parsed_targets))
    if not targets:
        raise RoborockMappingError(
            "area_mapping_missing",
            "The room is not mapped to this vacuum in Home Assistant.",
        )
    return MappingResolution(targets)


def build_roborock_two_pass_payload(targets: Sequence[int]) -> dict[str, object]:
    """Build the isolated Roborock native repeat payload."""

    return {
        "command": "app_segment_clean",
        "params": [{"segments": list(targets), "repeat": 2}],
    }


class RoborockVacuumAdapter(VacuumAdapter):
    """Enhance compatible Roborock vacuums with native cross-hatching."""

    adapter_id = "roborock"
    schema_version = 1
    priority = 100
    platforms = frozenset({"roborock"})

    def __init__(self, generic: GenericVacuumAdapter) -> None:
        self._generic = generic

    async def async_capabilities(
        self, hass: Any, context: AdapterMatchContext
    ) -> AdapterCapabilities:
        generic = await self._generic.async_capabilities(hass, context)
        pass_counts = set(generic.supported_pass_counts)
        native_pass_counts: set[int] = set()
        if context.supports_area_clean and context.supports_send_command:
            pass_counts.add(2)
            native_pass_counts.add(2)
        return AdapterCapabilities(
            adapter_id=self.adapter_id,
            schema_version=self.schema_version,
            portable_area_clean=generic.portable_area_clean,
            supported_pass_counts=frozenset(pass_counts),
            native_area_pass_counts=frozenset(native_pass_counts),
            supported_operations=generic.supported_operations,
            fan_speed_options=generic.fan_speed_options,
            mode_options=generic.mode_options,
            mop_mode_options=generic.mop_mode_options,
            mop_intensity_options=generic.mop_intensity_options,
            water_readiness=generic.water_readiness,
        )

    @staticmethod
    def _vacuum_options(hass: Any, entity_id: str) -> Mapping[str, object]:
        entry = er.async_get(hass).async_get(entity_id)
        options = getattr(entry, "options", {}) if entry else {}
        vacuum_options = options.get("vacuum", {}) if isinstance(options, Mapping) else {}
        return vacuum_options if isinstance(vacuum_options, Mapping) else {}

    async def async_preflight(
        self,
        hass: Any,
        context: AdapterMatchContext,
        request: AdapterDispatchRequest,
    ) -> AdapterDispatchResult:
        if request.passes != 2:
            return await self._generic.async_preflight(hass, context, request)
        capabilities = await self.async_capabilities(hass, context)
        if not capabilities.supports(request.operation, request.passes):
            return AdapterDispatchResult(
                "unsupported",
                "two_pass_no_longer_supported",
                "This vacuum no longer supports native two-pass room cleaning.",
            )
        try:
            resolve_roborock_area_mapping(
                self._vacuum_options(hass, request.robot_entity_id), request.area_ids
            )
        except RoborockMappingError as err:
            return AdapterDispatchResult("mapping_error", err.code, err.summary)
        return AdapterDispatchResult("ready", "ready", "Ready")

    async def async_dispatch(
        self,
        hass: Any,
        context: AdapterMatchContext,
        request: AdapterDispatchRequest,
    ) -> AdapterDispatchResult:
        if request.passes != 2:
            return await self._generic.async_dispatch(hass, context, request)
        preflight = await self.async_preflight(hass, context, request)
        if not preflight.ready:
            return preflight
        resolved = resolve_roborock_area_mapping(
            self._vacuum_options(hass, request.robot_entity_id), request.area_ids
        )
        payload = build_roborock_two_pass_payload(resolved.targets)
        await hass.services.async_call(
            "vacuum",
            "send_command",
            {"entity_id": request.robot_entity_id, **payload},
            blocking=True,
        )
        return AdapterDispatchResult(
            "accepted",
            "accepted",
            "Cleaning request accepted",
            native_attempted=True,
        )
