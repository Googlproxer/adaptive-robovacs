"""Validated persistence for archived Q10 map captures."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import MAP_RECOVERY_STORAGE_KEY, MAP_RECOVERY_STORE_VERSION
from .map_recovery_models import (
    ArchivedMap,
    ArchivedRoomSummary,
    MapCaptureSet,
    MapRecoveryArchive,
    RobotMapArchive,
)
from .q10_map_frame import MAX_PACKET_BYTES

_ARCHIVE_SCHEMA_VERSION = 1
_MAX_PREVIEW_BYTES = 4 * 1024 * 1024


class MapRecoveryStorageError(ValueError):
    """The map archive cannot safely be decoded."""


@dataclass(frozen=True, slots=True)
class MapRecoveryLoadResult:
    """Typed map archive plus a safe-mode diagnostic."""

    archive: MapRecoveryArchive
    error: str | None = None

    @property
    def safe_mode(self) -> bool:
        """Return whether the original Store must remain untouched."""

        return self.error is not None


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise MapRecoveryStorageError(f"{name} must be an object")
    return value


def _string(value: object, name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value:
        raise MapRecoveryStorageError(f"{name} must be a non-empty string")
    return value


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise MapRecoveryStorageError(f"{name} must be an integer >= {minimum}")
    return value


def _timestamp(value: object, name: str) -> datetime:
    text = _string(value, name)
    assert text is not None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as err:
        raise MapRecoveryStorageError(f"{name} must be an ISO timestamp") from err
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _bytes(value: object, name: str, maximum: int) -> bytes:
    text = _string(value, name)
    assert text is not None
    try:
        decoded = base64.b64decode(text, validate=True)
    except ValueError as err:
        raise MapRecoveryStorageError(f"{name} must be valid base64") from err
    if len(decoded) > maximum:
        raise MapRecoveryStorageError(f"{name} exceeds its size limit")
    return decoded


def _decode_room(value: object) -> ArchivedRoomSummary:
    item = _mapping(value, "decoded room")
    name = _string(item.get("name"), "decoded room name")
    assert name is not None
    return ArchivedRoomSummary(
        room_id=_integer(item.get("room_id"), "decoded room id"),
        name=name,
        order_hint=_integer(item.get("order_hint"), "decoded room order"),
        pixel_count=_integer(item.get("pixel_count"), "decoded room pixels"),
    )


def _decode_map(value: object) -> ArchivedMap:
    item = _mapping(value, "archived map")
    summary = _mapping(item.get("decoded_summary"), "decoded map summary")
    raw_rooms = summary.get("rooms")
    if not isinstance(raw_rooms, list):
        raise MapRecoveryStorageError("decoded map rooms must be a list")
    map_id = _string(item.get("map_id"), "map id")
    name = _string(item.get("name"), "map name")
    digest = _string(item.get("packet_sha256"), "map digest")
    assert map_id is not None and name is not None and digest is not None
    if len(digest) != 64:
        raise MapRecoveryStorageError("map digest must be SHA-256")
    return ArchivedMap(
        map_id=map_id,
        name=name,
        robot_timestamp=_string(
            item.get("robot_timestamp"), "robot timestamp", nullable=True
        ),
        packet_sha256=digest,
        packet=_bytes(item.get("packet_b64"), "map packet", MAX_PACKET_BYTES),
        preview_png=_bytes(
            item.get("preview_png_b64"), "map preview", _MAX_PREVIEW_BYTES
        ),
        width=_integer(summary.get("width"), "map width", minimum=1),
        height=_integer(summary.get("height"), "map height", minimum=1),
        rooms=tuple(_decode_room(room) for room in raw_rooms),
    )


def _decode_capture(value: object) -> MapCaptureSet:
    item = _mapping(value, "map capture")
    raw_maps = item.get("maps")
    if not isinstance(raw_maps, list) or not raw_maps:
        raise MapRecoveryStorageError("map capture maps must be a non-empty list")
    snapshot_id = _string(item.get("snapshot_id"), "snapshot id")
    trigger = _string(item.get("trigger"), "capture trigger")
    digest = _string(item.get("combined_sha256"), "capture digest")
    assert snapshot_id is not None and trigger is not None and digest is not None
    if len(digest) != 64:
        raise MapRecoveryStorageError("capture digest must be SHA-256")
    return MapCaptureSet(
        snapshot_id=snapshot_id,
        captured_at=_timestamp(item.get("captured_at"), "capture timestamp"),
        trigger=trigger,
        combined_sha256=digest,
        active_map_id=_string(
            item.get("active_map_id"), "active map id", nullable=True
        ),
        maps=tuple(_decode_map(map_value) for map_value in raw_maps),
    )


def decode_map_archive(payload: object) -> MapRecoveryArchive:
    """Decode and completely validate one Store payload."""

    if payload is None:
        return MapRecoveryArchive()
    root = _mapping(payload, "map archive")
    if root.get("schema_version") != _ARCHIVE_SCHEMA_VERSION:
        raise MapRecoveryStorageError("unsupported map archive schema")
    robots = _mapping(root.get("robots"), "map archive robots")
    decoded: dict[str, RobotMapArchive] = {}
    for registry_id, raw_archive in robots.items():
        if not isinstance(registry_id, str) or not registry_id:
            raise MapRecoveryStorageError("map archive robot key is invalid")
        archive = _mapping(raw_archive, "robot map archive")
        captures = archive.get("capture_sets")
        if not isinstance(captures, list):
            raise MapRecoveryStorageError("robot capture sets must be a list")
        last_error = _string(
            archive.get("last_error"), "robot map error", nullable=True
        )
        decoded[registry_id] = RobotMapArchive(
            capture_sets=[_decode_capture(capture) for capture in captures],
            last_error=last_error,
        )
    return MapRecoveryArchive(robots=decoded)


def _encode_map(value: ArchivedMap) -> dict[str, object]:
    return {
        "map_id": value.map_id,
        "name": value.name,
        "robot_timestamp": value.robot_timestamp,
        "packet_sha256": value.packet_sha256,
        "packet_b64": base64.b64encode(value.packet).decode("ascii"),
        "preview_png_b64": base64.b64encode(value.preview_png).decode("ascii"),
        "decoded_summary": {
            "width": value.width,
            "height": value.height,
            "rooms": [
                {
                    "room_id": room.room_id,
                    "name": room.name,
                    "order_hint": room.order_hint,
                    "pixel_count": room.pixel_count,
                }
                for room in value.rooms
            ],
        },
    }


def encode_map_archive(value: MapRecoveryArchive) -> dict[str, object]:
    """Encode typed map history for the Store boundary."""

    return {
        "schema_version": _ARCHIVE_SCHEMA_VERSION,
        "robots": {
            registry_id: {
                "capture_sets": [
                    {
                        "snapshot_id": capture.snapshot_id,
                        "captured_at": capture.captured_at.isoformat(),
                        "trigger": capture.trigger,
                        "active_map_id": capture.active_map_id,
                        "combined_sha256": capture.combined_sha256,
                        "maps": [_encode_map(item) for item in capture.maps],
                    }
                    for capture in archive.capture_sets
                ],
                "last_error": archive.last_error,
            }
            for registry_id, archive in value.robots.items()
        },
    }


class MapRecoveryStore:
    """Home Assistant Store wrapper with validate-before-write semantics."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass,
            MAP_RECOVERY_STORE_VERSION,
            f"{MAP_RECOVERY_STORAGE_KEY}.{entry_id}",
        )

    async def async_load(self) -> MapRecoveryLoadResult:
        """Load a complete archive without mutating malformed storage."""

        try:
            payload = await self._store.async_load()
            return MapRecoveryLoadResult(decode_map_archive(payload))
        except Exception:
            return MapRecoveryLoadResult(
                MapRecoveryArchive(),
                "map capture storage is malformed or unavailable",
            )

    async def async_save(self, archive: MapRecoveryArchive) -> None:
        """Persist a fully typed archive."""

        await self._store.async_save(encode_map_archive(archive))
