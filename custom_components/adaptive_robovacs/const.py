"""Constants for Adaptive RoboVacs."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "adaptive_robovacs"
NAME: Final = "Adaptive RoboVacs"
# Keep the Home Assistant Store envelope stable so existing persisted payloads
# reach the schema-versioned codec in state.py for migration.
STORE_VERSION: Final = 1
STORAGE_KEY: Final = f"{DOMAIN}.data"
MAP_RECOVERY_STORE_VERSION: Final = 1
MAP_RECOVERY_STORAGE_KEY: Final = f"{DOMAIN}.map_recovery"
MAP_RECOVERY_RETENTION: Final = 10

PLATFORMS: Final = ("button", "camera", "number", "select", "sensor", "switch")
SCAN_INTERVAL: Final = timedelta(minutes=15)
HISTORY_DAYS: Final = 56
FALLBACK_SAMPLE_COUNT: Final = 6
EXTRA_CLEAR_MINUTES: Final = 10
START_CONFIRMATION_TIMEOUT: Final = timedelta(minutes=2)
READY_CONFIRMATION_DELAY: Final = timedelta(seconds=10)

# Home Assistant normalizes label IDs from display names with underscores.
# The documented labels may be shown as ``robovac-bedroom`` in the UI, but
# their registry IDs are ``robovac_bedroom`` and so on.
LABEL_BEDROOM: Final = "robovac_bedroom"
LABEL_BEDROOM_TRANSIT: Final = "robovac_bedroom_transit"
LABEL_EXCLUDE: Final = "robovac_exclude"
LABEL_RADAR: Final = "robovac_radar"

DEFAULT_COMMON_INTERVAL: Final = 84
DEFAULT_BEDROOM_INTERVAL: Final = 168
DEFAULT_MOP_INTERVAL: Final = 168
DEFAULT_EXPECTED_MINUTES: Final = 30
DEFAULT_MINIMUM_BATTERY: Final = 80
DEFAULT_FORECAST_CONFIDENCE: Final = 80
DEFAULT_HALL_START: Final = "09:00"
DEFAULT_HALL_END: Final = "20:00"
DEFAULT_UNRESOLVED_START: Final = "01:00"
DEFAULT_UNRESOLVED_END: Final = "05:00"

SERVICE_EVALUATE: Final = "evaluate"
SERVICE_RECORD_MANUAL_CLEAN: Final = "record_manual_clean"
SERVICE_MANUAL_CLEAN_ROOM: Final = "manual_clean_room"
SERVICE_LIST_RETAINED_MAPS: Final = "list_retained_maps"
SERVICE_CAPTURE_MAP_SNAPSHOT: Final = "capture_map_snapshot"
SERVICE_ACTIVATE_RETAINED_MAP: Final = "activate_retained_map"
SERVICE_CONFIRM_MAP_SELECTION: Final = "confirm_map_selection"
SERVICE_LIST_LEGACY_DEFERRALS: Final = "list_legacy_deferrals"
SERVICE_CLEAR_LEGACY_DEFERRALS: Final = "clear_legacy_deferrals"
SERVICE_SAVE_FLOOR_PLAN: Final = "save_floor_plan"
SERVICE_SET_ROOM_ADJACENCY: Final = "set_room_adjacency"

SIGNAL_DISCOVERY_UPDATED: Final = f"{DOMAIN}_discovery_updated"

CONF_OBSERVE_ONLY: Final = "observe_only"
CONF_FORECAST_CONFIDENCE: Final = "forecast_confidence"
CONF_HALL_START: Final = "hall_start"
CONF_HALL_END: Final = "hall_end"
CONF_UNRESOLVED_START: Final = "unresolved_start"
CONF_UNRESOLVED_END: Final = "unresolved_end"

EVENT_EVALUATION: Final = f"{DOMAIN}_evaluation"
