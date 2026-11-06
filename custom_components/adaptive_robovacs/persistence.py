"""Reuse unchanged occupancy history without weakening Store validation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, tzinfo

from .state import PreparedStorePayload, SchedulerState, ValidatedOccupancySamples


@dataclass(frozen=True, slots=True)
class _OccupancyEncoding:
    source: tuple[tuple[datetime, tzinfo | None, int, type[int], int], ...]
    certificate: ValidatedOccupancySamples
    fingerprint: str


class StatePersistenceEncoder:
    """Application-scoped cache keyed by values, never mutable object identity.

    Taking a small tuple of scalar source values also catches in-place edits,
    same-length replacements, pruning and clearing. Cache entries contain only
    immutable data; every Store payload gets fresh dictionaries and lists.
    """

    def __init__(self) -> None:
        self._occupancy: dict[str, _OccupancyEncoding] = {}

    def prepare(self, state: SchedulerState) -> PreparedStorePayload:
        current: dict[str, _OccupancyEncoding] = {}
        records: dict[str, list[dict[str, object]]] = {}
        for area_id, history in state.room_history.items():
            source = tuple(
                (
                    sample.started_at,
                    sample.started_at.tzinfo,
                    sample.started_at.fold,
                    type(sample.minutes),
                    sample.minutes,
                )
                for sample in history.occupancy_samples
            )
            encoding = self._occupancy.get(area_id)
            if encoding is None or encoding.source != source:
                certificate = ValidatedOccupancySamples.from_samples(
                    history.occupancy_samples
                )
                encoded = json.dumps(
                    certificate.to_store(), sort_keys=True, separators=(",", ":")
                ).encode()
                encoding = _OccupancyEncoding(
                    source, certificate, hashlib.sha256(encoded).hexdigest()
                )
            current[area_id] = encoding
            records[area_id] = encoding.certificate.to_store()
        # Drop removed rooms and replaced histories. No persisted-data cache
        # advances here: this is reusable validation evidence, not a write ack.
        self._occupancy = current
        return PreparedStorePayload(
            state.to_store(occupancy_samples=records),
            {key: value.certificate for key, value in current.items()},
            {key: value.fingerprint for key, value in current.items()},
        )
