"""Constants for Adaptive RoboVacs."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "adaptive_robovacs"
NAME: Final = "Adaptive RoboVacs"
VERSION: Final = 1
STORAGE_KEY: Final = f"{DOMAIN}.data"

PLATFORMS: Final = ("button", "number", "select", "sensor", "switch")
SCAN_INTERVAL: Final = timedelta(minutes=15)
HISTORY_DAYS: Final = 56
FALLBACK_SAMPLE_COUNT: Final = 6
EXTRA_CLEAR_MINUTES: Final = 10

LABEL_BEDROOM: Final = "robovac-bedroom"
LABEL_BEDROOM_TRANSIT: Final = "robovac-bedroom-transit"
LABEL_EXCLUDE: Final = "robovac-exclude"
LABEL_RADAR: Final = "robovac-radar"

DEFAULT_COMMON_INTERVAL: Final = 84
DEFAULT_BEDROOM_INTERVAL: Final = 168
DEFAULT_MOP_INTERVAL: Final = 168
DEFAULT_EXPECTED_MINUTES: Final = 30
DEFAULT_MINIMUM_BATTERY: Final = 80
DEFAULT_FORECAST_CONFIDENCE: Final = 80
DEFAULT_HALL_START: Final = "09:00"
DEFAULT_HALL_END: Final = "20:00"

SERVICE_EVALUATE: Final = "evaluate"
SERVICE_RECORD_MANUAL_CLEAN: Final = "record_manual_clean"
SERVICE_DECOMMISSION_REPORT: Final = "decommission_report"

SIGNAL_DISCOVERY_UPDATED: Final = f"{DOMAIN}_discovery_updated"

CONF_OBSERVE_ONLY: Final = "observe_only"
CONF_FORECAST_CONFIDENCE: Final = "forecast_confidence"
CONF_HALL_START: Final = "hall_start"
CONF_HALL_END: Final = "hall_end"

EVENT_EVALUATION: Final = f"{DOMAIN}_evaluation"
