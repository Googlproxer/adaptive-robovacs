"""Bounded, privacy-safe runtime performance metrics."""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Mapping
from typing import Any


class RuntimeMetrics:
    """Collect inexpensive counters without persisting household identifiers."""

    def __init__(self) -> None:
        self.state_events: Counter[str] = Counter()
        self.command_coalesces: Counter[str] = Counter()
        self.evaluations: Counter[str] = Counter()
        self.queue_high_water = 0
        self.discovery_count = 0
        self.storage_attempts = 0
        self.storage_writes = 0
        self.storage_skips = 0
        self.snapshot_publications = 0
        self.snapshot_equal_skips = 0
        self.entity_writes: Counter[str] = Counter()
        self.entity_skips: Counter[str] = Counter()
        self._discovery_durations: deque[float] = deque(maxlen=128)
        self._evaluation_durations: deque[float] = deque(maxlen=128)

    def record_queue_depth(self, depth: int) -> None:
        """Retain only the maximum observed queue depth."""

        self.queue_high_water = max(self.queue_high_water, depth)

    def record_discovery(self, duration: float) -> None:
        """Record one registry discovery duration in seconds."""

        self.discovery_count += 1
        self._discovery_durations.append(max(0.0, duration))

    def record_evaluation(self, reason: str, duration: float) -> None:
        """Record one completed evaluation by bounded stable cause."""

        cause = reason.split(":", 1)[0]
        self.evaluations[cause] += 1
        self._evaluation_durations.append(max(0.0, duration))

    @staticmethod
    def _duration_summary(values: deque[float]) -> dict[str, float | int]:
        if not values:
            return {"samples": 0, "last_ms": 0.0, "mean_ms": 0.0, "max_ms": 0.0}
        milliseconds = [value * 1000 for value in values]
        return {
            "samples": len(milliseconds),
            "last_ms": round(milliseconds[-1], 3),
            "mean_ms": round(sum(milliseconds) / len(milliseconds), 3),
            "max_ms": round(max(milliseconds), 3),
        }

    def as_dict(self, topology: Mapping[str, int]) -> dict[str, Any]:
        """Return diagnostics containing counts only, never entity identities."""

        return {
            "topology": dict(topology),
            "state_events": dict(sorted(self.state_events.items())),
            "command_coalesces": dict(sorted(self.command_coalesces.items())),
            "queue_high_water": self.queue_high_water,
            "discovery": {
                "count": self.discovery_count,
                **self._duration_summary(self._discovery_durations),
            },
            "evaluations": {
                "by_cause": dict(sorted(self.evaluations.items())),
                **self._duration_summary(self._evaluation_durations),
            },
            "storage": {
                "attempts": self.storage_attempts,
                "writes": self.storage_writes,
                "skipped_unchanged": self.storage_skips,
            },
            "snapshots": {
                "published": self.snapshot_publications,
                "skipped_equal": self.snapshot_equal_skips,
            },
            "entities": {
                "writes_by_scope": dict(sorted(self.entity_writes.items())),
                "skips_by_scope": dict(sorted(self.entity_skips.items())),
            },
        }
