"""Typed Home Assistant state-watch specifications."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class WatchChange:
    """Meaningful work required by one watched state event."""

    evaluate: bool
    refresh_discovery: bool


@dataclass(frozen=True, slots=True)
class WatchSpecification:
    """State and attribute dependencies for one discovered HA entity."""

    entity_id: str
    evaluation_attributes: frozenset[str] = field(default_factory=frozenset)
    capability_attributes: frozenset[str] = field(default_factory=frozenset)

    def classify(self, old_state: Any, new_state: Any) -> WatchChange:
        """Ignore same-state changes outside this entity's declared attributes."""

        old_value = getattr(old_state, "state", None)
        new_value = getattr(new_state, "state", None)
        old_attributes = getattr(old_state, "attributes", {}) or {}
        new_attributes = getattr(new_state, "attributes", {}) or {}
        attribute_change = any(
            old_attributes.get(key) != new_attributes.get(key)
            for key in self.evaluation_attributes
        )
        capability_change = any(
            old_attributes.get(key) != new_attributes.get(key)
            for key in self.capability_attributes
        )
        return WatchChange(
            evaluate=(old_value != new_value or attribute_change or capability_change),
            refresh_discovery=capability_change,
        )
