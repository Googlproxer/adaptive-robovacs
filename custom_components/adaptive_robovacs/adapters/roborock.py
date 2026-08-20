"""Roborock enhanced adapter with native two-pass segment cleaning."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.helpers import entity_registry as er

from ..models import (
    AdapterCapabilities,
    AdapterDispatchRequest,
    AdapterDispatchResult,
    DIRECT_CUSTOM_MOP_INTENSITIES,
    DIRECT_CUSTOM_MOP_ROUTES,
    WaterReadiness,
)
from .base import AdapterEntityEvidence, AdapterMatchContext, VacuumAdapter
from .generic import GenericVacuumAdapter


_LOGGER = logging.getLogger(__name__)


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


WATER_ENTITY_KEYS = {
    # Entity-description key: entity-registry translation key. Home Assistant
    # persists the latter, while accepting the former keeps adapter fixtures
    # explicit about the upstream Roborock data field.
    "water_box_carriage_status": "mop_attached",
    "water_box_status": "water_box_attached",
    "water_shortage": "water_shortage",
}


Q10_CUSTOMER_CLEAN_DP = "62"
Q10_CUSTOMIZED_OPTION = "customized"
Q10_VACUUM_CLEAN_TYPE = 2
Q10_WATER_OFF = 0
# The Q10's line values do not follow the displayed label order: observation
# confirms that line 0 is the daily (fastest) clean and line 1 is fast.
Q10_DEFAULT_CLEAN_LINE = 0
Q10_CUSTOM_CLEAN_SETTLE_SECONDS = 1.2
Q10_CLEANING_DEPTH_LINES = {
    "fast": 1,
    "daily": Q10_DEFAULT_CLEAN_LINE,
    "fine": 2,
}
Q10_FAN_LEVELS = {
    "quiet": 1,
    "balanced": 2,
    "turbo": 3,
    "max": 4,
    "max_plus": 8,
}
DIRECT_CUSTOM_MOP_RETRY_INTERVAL_SECONDS = 5
DIRECT_CUSTOM_MOP_RETRY_ATTEMPTS = 6
DIRECT_CUSTOM_MOP_BLOCK_CODES = frozenset(
    {
        "direct_custom_mop_profile_invalid",
        "direct_custom_mop_control_unavailable",
        "direct_custom_mop_unconfirmed",
    }
)


def _normalised_option(value: object) -> str:
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def _option(options: Sequence[str], wanted: str) -> str | None:
    return next(
        (option for option in options if _normalised_option(option) == wanted), None
    )


def supports_roborock_direct_custom_mop(context: AdapterMatchContext) -> bool:
    """Return whether live controls prove a direct, suction-off mop profile."""

    profile = context.profile
    control_ids = (
        profile.mode_select_entity_id,
        profile.mop_mode_select_entity_id,
        profile.mop_intensity_select_entity_id,
    )
    return bool(
        context.supports_area_clean
        and all(control_ids)
        # A shared generic operation selector must never be mistaken for the
        # independent native Custom, route, and water controls.
        and len(set(control_ids)) == len(control_ids)
        and _option(profile.mode_options, "custom")
        and _option(context.fan_speed_options, "off")
        and any(
            _normalised_option(option) in DIRECT_CUSTOM_MOP_ROUTES
            for option in profile.mop_mode_options
        )
        and any(
            _normalised_option(option) in DIRECT_CUSTOM_MOP_INTENSITIES
            for option in profile.mop_intensity_options
        )
    )


@dataclass(frozen=True, slots=True)
class Q10CustomCleanProfile:
    """Validated Q10 custom-clean settings for one immediate room start."""

    fan_level: int
    clean_line: int
    mode_entity_id: str


class Q10CustomCleanError(ValueError):
    """A safe Q10 custom-clean profile or protocol failure."""

    def __init__(self, code: str, summary: str) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = summary


def resolve_roborock_water_readiness(
    entities: Sequence[AdapterEntityEvidence], supports_mopping: bool
) -> tuple[WaterReadiness, tuple[str, ...]]:
    """Resolve the authoritative same-device Roborock water sensor trio."""

    if not supports_mopping:
        return WaterReadiness.unsupported(), ()
    matches = {
        key: tuple(
            evidence
            for evidence in entities
            if evidence.domain == "binary_sensor"
            and evidence.platform == "roborock"
            and evidence.translation_key in {key, WATER_ENTITY_KEYS[key]}
        )
        for key in WATER_ENTITY_KEYS
    }
    watched = tuple(
        dict.fromkeys(
            evidence.entity_id
            for values in matches.values()
            for evidence in values
        )
    )
    if any(len(matches[key]) != 1 for key in WATER_ENTITY_KEYS):
        return (
            WaterReadiness(
                "confirmation_required",
                "water_telemetry_incomplete",
                ready=False,
                authoritative=False,
            ),
            watched,
        )
    states = {key: matches[key][0].state for key in WATER_ENTITY_KEYS}
    if any(state in {None, "unknown", "unavailable"} for state in states.values()):
        return (
            WaterReadiness(
                "sensor_blocked",
                "water_telemetry_unavailable",
                ready=False,
                authoritative=True,
            ),
            watched,
        )
    ready = (
        states["water_box_carriage_status"] == "on"
        and states["water_box_status"] == "on"
        and states["water_shortage"] == "off"
    )
    return (
        WaterReadiness(
            "sensor_ready" if ready else "sensor_blocked",
            "water_ready" if ready else "water_unavailable",
            ready=ready,
            authoritative=True,
        ),
        watched,
    )


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
    if not targets or len(targets) > 255:
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


def supports_roborock_native_two_pass(vacuum_options: Mapping[str, object]) -> bool:
    """Return whether the mapped vacuum accepts the legacy repeat command.

    Home Assistant's newer B01/Q10 implementation still advertises
    ``vacuum.send_command``, but it only accepts its own DP commands.  Its
    current segment identifiers are unprefixed (for example ``"6"``), unlike
    the V1 implementation's ``"map_segment"`` identifiers.  Do not advertise
    two-pass support to that implementation: attempting the legacy
    ``app_segment_clean`` command is rejected before it can reach the robot.

    Missing or malformed evidence remains eligible here so the normal mapping
    preflight can return its precise, actionable error instead of guessing.
    """

    last_seen = vacuum_options.get("last_seen_segments")
    if not isinstance(last_seen, Sequence) or isinstance(last_seen, (str, bytes)):
        return True
    segment_ids = [
        item["id"]
        for item in last_seen
        if isinstance(item, Mapping) and "id" in item
    ]
    if not segment_ids:
        return True
    for segment_id in segment_ids:
        parsed = _segment_parts(segment_id)
        if parsed is None:
            return True
        if parsed[0] is None:
            return False
    return True


def is_roborock_q10_protocol(vacuum_options: Mapping[str, object]) -> bool:
    """Return whether current mapping evidence identifies the B01/Q10 protocol.

    Q10's Home Assistant integration publishes its current segment IDs as
    unprefixed numeric values. Legacy V1 Roborock devices publish compound
    ``map_segment`` values and accept ``app_segment_clean`` instead. Unknown or
    malformed evidence never opts a vacuum into Q10 custom commands.
    """

    last_seen = vacuum_options.get("last_seen_segments")
    if not isinstance(last_seen, Sequence) or isinstance(last_seen, (str, bytes)):
        return False
    segment_ids = [
        item["id"]
        for item in last_seen
        if isinstance(item, Mapping) and "id" in item
    ]
    if not segment_ids:
        return False
    return all(
        (parsed := _segment_parts(segment_id)) is not None and parsed[0] is None
        for segment_id in segment_ids
    )


def q10_customized_mode_entity(context: AdapterMatchContext) -> str | None:
    """Return the unambiguous Q10 cleaning-mode select with ``customized``."""

    matches = tuple(
        evidence.entity_id
        for evidence in context.entities
        if evidence.domain == "select"
        and evidence.platform == "roborock"
        and evidence.translation_key
        in {"cleaning_mode", "clean_mode", "operation_mode"}
        and Q10_CUSTOMIZED_OPTION in evidence.options
    )
    return matches[0] if len(matches) == 1 else None


def build_q10_customer_clean_payload(
    targets: Sequence[int], *, fan_level: int, clean_count: int, clean_line: int
) -> str:
    """Encode a Q10 customer-clean room profile without retaining room IDs."""

    if not targets:
        raise Q10CustomCleanError(
            "area_mapping_missing",
            "The room is not mapped to this vacuum in Home Assistant.",
        )
    if fan_level not in set(Q10_FAN_LEVELS.values()):
        raise Q10CustomCleanError(
            "profile_option_unsupported",
            "The selected fan speed is not supported for Q10 custom cleaning.",
        )
    if clean_count not in {1, 2, 3}:
        raise Q10CustomCleanError(
            "adapter_request_unsupported",
            "The requested Q10 custom clean count is not supported.",
        )
    if clean_line not in set(Q10_CLEANING_DEPTH_LINES.values()):
        raise Q10CustomCleanError(
            "profile_option_unsupported",
            "The selected cleaning depth is not supported for Q10 custom cleaning.",
        )
    payload: list[int] = [len(targets)]
    for target in targets:
        if isinstance(target, bool) or not isinstance(target, int) or not 1 <= target <= 255:
            raise Q10CustomCleanError(
                "area_mapping_ambiguous",
                "The Home Assistant area mapping uses an unsupported Q10 segment.",
            )
        payload.extend(
            (
                target,
                fan_level,
                Q10_WATER_OFF,
                Q10_VACUUM_CLEAN_TYPE,
                clean_count,
                clean_line,
            )
        )
    return base64.b64encode(bytes(payload)).decode("ascii")


def build_q10_start_payload(targets: Sequence[int]) -> dict[str, object]:
    """Build the B01/Q10 room start command using transient mapped targets."""

    return {
        "command": "dpStartClean",
        "params": {"cmd": 2, "clean_paramters": list(targets)},
    }


def resolve_q10_custom_clean_profile(
    hass: Any,
    context: AdapterMatchContext,
    request: AdapterDispatchRequest,
) -> Q10CustomCleanProfile:
    """Validate Q10-only profile values immediately before a custom start."""

    if request.operation != "vacuum":
        raise Q10CustomCleanError(
            "adapter_request_unsupported",
            "Q10 custom two-pass cleaning currently supports vacuum-only work.",
        )
    mode_entity_id = q10_customized_mode_entity(context)
    if not mode_entity_id:
        raise Q10CustomCleanError(
            "profile_control_unavailable",
            "The Q10 customized cleaning-mode control is unavailable.",
        )
    mode_state = hass.states.get(mode_entity_id)
    if (
        not mode_state
        or mode_state.state in {"unknown", "unavailable"}
        or Q10_CUSTOMIZED_OPTION not in mode_state.attributes.get("options", [])
    ):
        raise Q10CustomCleanError(
            "profile_control_unavailable",
            "The Q10 customized cleaning-mode control is unavailable.",
        )
    fan_speed = request.cleaning_profile.get("fan_speed")
    if fan_speed is None:
        vacuum_state = hass.states.get(request.robot_entity_id)
        fan_speed = (
            vacuum_state.attributes.get("fan_speed") if vacuum_state else None
        )
    if not isinstance(fan_speed, str) or fan_speed not in Q10_FAN_LEVELS:
        raise Q10CustomCleanError(
            "profile_option_unsupported",
            "The selected fan speed is not supported for Q10 custom cleaning.",
        )
    cleaning_depth = request.cleaning_profile.get("cleaning_depth")
    if cleaning_depth is None:
        clean_line = Q10_DEFAULT_CLEAN_LINE
    elif isinstance(cleaning_depth, str) and cleaning_depth in Q10_CLEANING_DEPTH_LINES:
        clean_line = Q10_CLEANING_DEPTH_LINES[cleaning_depth]
    else:
        raise Q10CustomCleanError(
            "profile_option_unsupported",
            "The selected cleaning depth is not supported for Q10 custom cleaning.",
        )
    return Q10CustomCleanProfile(
        fan_level=Q10_FAN_LEVELS[fan_speed],
        clean_line=clean_line,
        mode_entity_id=mode_entity_id,
    )


class RoborockVacuumAdapter(VacuumAdapter):
    """Enhance compatible Roborock vacuums with native cross-hatching."""

    adapter_id = "roborock"
    schema_version = 7
    priority = 100
    platforms = frozenset({"roborock"})

    def __init__(self, generic: GenericVacuumAdapter) -> None:
        self._generic = generic

    async def async_capabilities(
        self, hass: Any, context: AdapterMatchContext
    ) -> AdapterCapabilities:
        generic = await self._generic.async_capabilities(hass, context)
        vacuum_options = self._vacuum_options(hass, context.entity_id)
        is_q10 = is_roborock_q10_protocol(vacuum_options)
        direct_custom_mop = (
            not is_q10 and supports_roborock_direct_custom_mop(context)
        )
        vacuum_pass_counts = set(generic.vacuum_pass_counts)
        mop_pass_counts = set(generic.mop_pass_counts)
        native_vacuum_pass_counts: set[int] = set()
        native_mop_pass_counts: set[int] = set()
        cleaning_depth_options: tuple[str, ...] = ()

        if is_q10:
            # The Q10 command family cannot use the portable/legacy repeat
            # control. It has a vacuum-only customer-clean profile instead.
            vacuum_pass_counts = {1}
            mop_pass_counts = {1} if "mop" in generic.supported_operations else set()
            if (
                context.supports_area_clean
                and context.supports_send_command
                and q10_customized_mode_entity(context)
            ):
                vacuum_pass_counts.add(2)
                native_vacuum_pass_counts.add(1)
                native_vacuum_pass_counts.add(2)
                cleaning_depth_options = tuple(Q10_CLEANING_DEPTH_LINES)
        elif (
            context.supports_area_clean
            and context.supports_send_command
            and supports_roborock_native_two_pass(vacuum_options)
        ):
            vacuum_pass_counts.add(2)
            if "mop" in generic.supported_operations:
                mop_pass_counts.add(2)
            native_vacuum_pass_counts.add(2)
            if "mop" in generic.supported_operations:
                native_mop_pass_counts.add(2)
        water, watched = resolve_roborock_water_readiness(
            context.entities, "mop" in generic.supported_operations
        )
        return AdapterCapabilities(
            adapter_id=self.adapter_id,
            schema_version=self.schema_version,
            portable_area_clean=generic.portable_area_clean,
            supported_pass_counts=frozenset(vacuum_pass_counts),
            native_area_pass_counts=frozenset(native_vacuum_pass_counts),
            supported_operations=generic.supported_operations,
            fan_speed_options=generic.fan_speed_options,
            mode_options=generic.mode_options,
            mop_mode_options=generic.mop_mode_options,
            mop_intensity_options=generic.mop_intensity_options,
            cleaning_depth_options=cleaning_depth_options,
            water_readiness=water,
            vacuum_pass_counts=frozenset(vacuum_pass_counts),
            mop_pass_counts=frozenset(mop_pass_counts),
            native_vacuum_pass_counts=frozenset(native_vacuum_pass_counts),
            native_mop_pass_counts=frozenset(native_mop_pass_counts),
            watched_entity_ids=watched,
            direct_custom_mop=direct_custom_mop,
        )

    @staticmethod
    def _vacuum_options(hass: Any, entity_id: str) -> Mapping[str, object]:
        registry = er.async_get(hass)
        entry = registry.async_get(entity_id) if registry else None
        options = getattr(entry, "options", {}) if entry else {}
        vacuum_options = options.get("vacuum", {}) if isinstance(options, Mapping) else {}
        return vacuum_options if isinstance(vacuum_options, Mapping) else {}

    def _is_q10_request(
        self, hass: Any, context: AdapterMatchContext, request: AdapterDispatchRequest
    ) -> bool:
        return (
            request.operation == "vacuum"
            and is_roborock_q10_protocol(
                self._vacuum_options(hass, context.entity_id)
            )
            and (
                request.passes == 2
                or request.cleaning_profile.get("cleaning_depth") is not None
            )
        )

    @staticmethod
    def _is_direct_custom_mop_request(
        context: AdapterMatchContext, request: AdapterDispatchRequest
    ) -> bool:
        return request.operation == "mop" and supports_roborock_direct_custom_mop(
            context
        )

    @staticmethod
    def _direct_custom_mop_values(
        context: AdapterMatchContext, request: AdapterDispatchRequest
    ) -> tuple[str, str, str, str] | None:
        """Return exact direct controls, or None when a stage is not safe."""

        profile = context.profile
        mode = request.cleaning_profile.get("mode")
        fan_speed = request.cleaning_profile.get("fan_speed")
        route = request.cleaning_profile.get("mop_mode")
        intensity = request.cleaning_profile.get("mop_intensity")
        if not all(isinstance(value, str) for value in (mode, fan_speed, route, intensity)):
            return None
        if (
            _normalised_option(mode) != "custom"
            or _normalised_option(fan_speed) != "off"
            or _normalised_option(route) not in DIRECT_CUSTOM_MOP_ROUTES
            or _normalised_option(intensity) not in DIRECT_CUSTOM_MOP_INTENSITIES
        ):
            return None
        resolved_mode = _option(profile.mode_options, "custom")
        resolved_fan = _option(context.fan_speed_options, "off")
        resolved_route = _option(profile.mop_mode_options, _normalised_option(route))
        resolved_intensity = _option(
            profile.mop_intensity_options, _normalised_option(intensity)
        )
        if not all((resolved_mode, resolved_fan, resolved_route, resolved_intensity)):
            return None
        return resolved_mode, resolved_route, resolved_intensity, resolved_fan

    @staticmethod
    def _direct_control_available(
        hass: Any, entity_id: str | None, option: str
    ) -> bool:
        state = hass.states.get(entity_id) if entity_id else None
        return bool(
            state
            and state.state not in {"unavailable", "unknown"}
            and option in state.attributes.get("options", [])
        )

    async def _async_validate_direct_custom_mop(
        self,
        hass: Any,
        context: AdapterMatchContext,
        request: AdapterDispatchRequest,
    ) -> AdapterDispatchResult:
        values = self._direct_custom_mop_values(context, request)
        if values is None:
            return AdapterDispatchResult(
                "blocked",
                "direct_custom_mop_profile_invalid",
                "Rob needs an explicit direct custom mop profile.",
            )
        mode, route, intensity, _fan_speed = values
        profile = context.profile
        controls = (
            (profile.mode_select_entity_id, mode),
            (profile.mop_mode_select_entity_id, route),
            (profile.mop_intensity_select_entity_id, intensity),
        )
        if not all(
            self._direct_control_available(hass, entity_id, option)
            for entity_id, option in controls
        ):
            return AdapterDispatchResult(
                "blocked",
                "direct_custom_mop_control_unavailable",
                "Rob's direct custom mop controls are unavailable.",
            )
        vacuum_state = hass.states.get(request.robot_entity_id)
        if not vacuum_state or vacuum_state.state in {"unavailable", "unknown"}:
            return AdapterDispatchResult(
                "blocked",
                "direct_custom_mop_control_unavailable",
                "Rob's direct custom mop controls are unavailable.",
            )
        return AdapterDispatchResult("ready", "ready", "Ready")

    async def _async_apply_direct_custom_mop(
        self,
        hass: Any,
        context: AdapterMatchContext,
        request: AdapterDispatchRequest,
    ) -> AdapterDispatchResult:
        """Apply and observe a suction-off custom mop profile before dispatch."""

        validation = await self._async_validate_direct_custom_mop(hass, context, request)
        if not validation.ready:
            return validation
        values = self._direct_custom_mop_values(context, request)
        assert values is not None
        mode, route, intensity, fan_speed = values
        profile = context.profile

        def observed() -> bool:
            mode_state = hass.states.get(profile.mode_select_entity_id)
            route_state = hass.states.get(profile.mop_mode_select_entity_id)
            intensity_state = hass.states.get(profile.mop_intensity_select_entity_id)
            vacuum_state = hass.states.get(request.robot_entity_id)
            return bool(
                mode_state
                and route_state
                and intensity_state
                and vacuum_state
                and mode_state.state == mode
                and route_state.state == route
                and intensity_state.state == intensity
                and vacuum_state.attributes.get("fan_speed") == fan_speed
            )

        for attempt in range(DIRECT_CUSTOM_MOP_RETRY_ATTEMPTS + 1):
            if context.can_mutate and not context.can_mutate():
                return AdapterDispatchResult("ready", "ready", "Ready")
            for entity_id, option in (
                (profile.mode_select_entity_id, mode),
                (profile.mop_mode_select_entity_id, route),
                (profile.mop_intensity_select_entity_id, intensity),
            ):
                await hass.services.async_call(
                    "select",
                    "select_option",
                    {"entity_id": entity_id, "option": option},
                    blocking=True,
                )
            if context.can_mutate and not context.can_mutate():
                return AdapterDispatchResult("ready", "ready", "Ready")
            await hass.services.async_call(
                "vacuum",
                "set_fan_speed",
                {"entity_id": request.robot_entity_id, "fan_speed": fan_speed},
                blocking=True,
            )
            if observed():
                return AdapterDispatchResult("ready", "ready", "Ready")
            if attempt < DIRECT_CUSTOM_MOP_RETRY_ATTEMPTS:
                await asyncio.sleep(DIRECT_CUSTOM_MOP_RETRY_INTERVAL_SECONDS)

        _LOGGER.warning(
            "Adaptive RoboVacs could not confirm Roborock direct custom mop profile: robot=%s retries=%s",
            request.robot_entity_id,
            DIRECT_CUSTOM_MOP_RETRY_ATTEMPTS,
        )
        return AdapterDispatchResult(
            "blocked",
            "direct_custom_mop_unconfirmed",
            "Rob's direct custom mop profile could not be confirmed.",
        )

    async def async_validate_profile(
        self,
        hass: Any,
        context: AdapterMatchContext,
        request: AdapterDispatchRequest,
    ) -> AdapterDispatchResult:
        """Reject unsupported Q10 custom profile values before mutation."""

        if self._is_direct_custom_mop_request(context, request):
            return await self._async_validate_direct_custom_mop(hass, context, request)
        result = await super().async_validate_profile(hass, context, request)
        if not result.ready or not self._is_q10_request(hass, context, request):
            return result
        try:
            resolve_q10_custom_clean_profile(hass, context, request)
        except Q10CustomCleanError as err:
            return AdapterDispatchResult("unsupported", err.code, err.summary)
        return AdapterDispatchResult("ready", "ready", "Ready")

    async def async_apply_profile(
        self,
        hass: Any,
        context: AdapterMatchContext,
        request: AdapterDispatchRequest,
    ) -> AdapterDispatchResult:
        """Leave Q10 custom values for its immediate pre-start sequence."""

        if self._is_direct_custom_mop_request(context, request):
            return await self._async_apply_direct_custom_mop(hass, context, request)
        if self._is_q10_request(hass, context, request):
            return AdapterDispatchResult("ready", "ready", "Ready")
        return await super().async_apply_profile(hass, context, request)

    async def async_preflight(
        self,
        hass: Any,
        context: AdapterMatchContext,
        request: AdapterDispatchRequest,
    ) -> AdapterDispatchResult:
        capabilities = await self.async_capabilities(hass, context)
        if not capabilities.supports(request.operation, request.passes):
            return AdapterDispatchResult(
                "unsupported",
                "two_pass_no_longer_supported",
                "This vacuum no longer supports native two-pass room cleaning.",
            )
        if request.operation == "mop":
            water = capabilities.water_readiness
            ignore_water = bool(request.cleaning_profile.get("ignore_water_readiness"))
            if water.status == "sensor_blocked" and not ignore_water:
                return AdapterDispatchResult("blocked", water.reason, "Water is not ready.")
            if water.status == "confirmation_required" and not ignore_water and not bool(
                request.cleaning_profile.get("water_confirmed")
            ):
                return AdapterDispatchResult(
                    "blocked",
                    "water_confirmation_required",
                    "Water confirmation is required before mopping.",
                )
        if request.passes == 2 or self._is_q10_request(hass, context, request):
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
        preflight = await self.async_preflight(hass, context, request)
        if not preflight.ready:
            return preflight
        if self._is_q10_request(hass, context, request):
            resolved = resolve_roborock_area_mapping(
                self._vacuum_options(hass, request.robot_entity_id), request.area_ids
            )
            try:
                profile = resolve_q10_custom_clean_profile(hass, context, request)
                customer_clean = build_q10_customer_clean_payload(
                    resolved.targets,
                    fan_level=profile.fan_level,
                    clean_count=request.passes,
                    clean_line=profile.clean_line,
                )
            except Q10CustomCleanError as err:
                return AdapterDispatchResult("unsupported", err.code, err.summary)
            if context.can_mutate and not context.can_mutate():
                return AdapterDispatchResult(
                    "blocked", "coordinator_shutting_down", "Coordinator is shutting down."
                )
            try:
                await hass.services.async_call(
                    "vacuum",
                    "send_command",
                    {
                        "entity_id": request.robot_entity_id,
                        "command": "dpCommon",
                        "params": {Q10_CUSTOMER_CLEAN_DP: customer_clean},
                    },
                    blocking=True,
                )
            except Exception:
                # This happens before the physical start command.  Mark a
                # Max+ profile distinctly so the runtime can safely persist
                # its one-way fallback to Max without retrying a clean.
                if request.cleaning_profile.get("fan_speed") == "max_plus":
                    return AdapterDispatchResult(
                        "unsupported",
                        "q10_max_plus_profile_write_failed",
                        "The Q10 rejected the Max+ custom cleaning profile.",
                    )
                raise
            await asyncio.sleep(Q10_CUSTOM_CLEAN_SETTLE_SECONDS)
            if context.can_mutate and not context.can_mutate():
                return AdapterDispatchResult(
                    "blocked", "coordinator_shutting_down", "Coordinator is shutting down."
                )
            await hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": profile.mode_entity_id, "option": Q10_CUSTOMIZED_OPTION},
                blocking=True,
            )
            if context.can_mutate and not context.can_mutate():
                return AdapterDispatchResult(
                    "blocked", "coordinator_shutting_down", "Coordinator is shutting down."
                )
            try:
                await hass.services.async_call(
                    "vacuum",
                    "send_command",
                    {"entity_id": request.robot_entity_id, **build_q10_start_payload(resolved.targets)},
                    blocking=True,
                )
            except Exception:
                # The service might have passed the command to the robot, so
                # the runtime must not retry.  It can still persist Max as the
                # safe setting for subsequent work.
                if request.cleaning_profile.get("fan_speed") == "max_plus":
                    return AdapterDispatchResult(
                        "failed",
                        "q10_max_plus_start_failed",
                        "The Q10 did not accept the Max+ room-clean start.",
                        native_attempted=True,
                        outcome_uncertain=True,
                    )
                raise
            return AdapterDispatchResult(
                "accepted",
                "accepted",
                "Cleaning request accepted",
                native_attempted=True,
            )
        if request.passes != 2:
            return await self._generic.async_dispatch(hass, context, request)
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
