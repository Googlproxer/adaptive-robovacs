"""Durable active-job mutations for the Adaptive RoboVacs scheduler."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from .models import manual_deferral

if TYPE_CHECKING:
    from .coordinator import AdaptiveRoboVacCoordinator


def _iso(value: datetime) -> str:
    return value.isoformat()


class JobLifecycle:
    """Apply durable job and audit changes through a coordinator-owned state view."""

    def __init__(self, coordinator: AdaptiveRoboVacCoordinator) -> None:
        self._coordinator = coordinator

    @staticmethod
    def active_rooms(active: dict[str, Any]) -> list[str]:
        """Return a job's tracked rooms, retaining compatibility with v1.0.x."""

        values = active.get("rooms") or [active.get("room")]
        if not isinstance(values, list):
            values = [values]
        return list(dict.fromkeys(value for value in values if isinstance(value, str)))

    def record_manual_event(self, event: dict[str, Any]) -> None:
        """Retain a bounded audit trail without changing normal cadence."""

        data = self._coordinator.data
        data["manual_events"].append(event)
        data["manual_events"] = data["manual_events"][-50:]

    def apply_manual_deferral(
        self,
        robot_entity_id: str,
        area_ids: list[str],
        operations: list[str],
        completed_at: datetime,
    ) -> list[str]:
        """Apply the narrow, one-day manual-clean deferral policy."""

        coordinator = self._coordinator
        changed: list[str] = []
        if robot_entity_id not in coordinator.discovery.robots:
            return changed
        for area_id in area_ids:
            room = coordinator.discovery.rooms.get(area_id)
            if not room:
                continue
            detail = coordinator._room_data(area_id)
            for operation in operations:
                if operation not in {"vacuum", "mop"}:
                    continue
                if operation == "mop" and coordinator._room_settings(room).get("carpet", False):
                    continue
                next_due = coordinator._room_due(room, operation, completed_at)
                deferred = manual_deferral(completed_at, next_due)
                if deferred:
                    detail.setdefault("defer", {})[operation] = _iso(deferred)
                    detail.setdefault("defer", {})["cleaning"] = _iso(deferred)
                    changed.append(f"{area_id}:{operation}")
        return changed

    def cancel(
        self, robot_id: str, active: dict[str, Any], cancelled_at: datetime, reason: str
    ) -> None:
        """Close a user-cancelled job without recording an incomplete room clean."""

        coordinator = self._coordinator
        area_ids = self.active_rooms(active)
        if active.get("source") == "manual_home_assistant":
            self.record_manual_event(
                {
                    "at": _iso(cancelled_at),
                    "robot": robot_id,
                    "rooms": area_ids,
                    "operations": list(active.get("requested_operations", ["vacuum"])),
                    "context_id": active.get("manual_context_id"),
                    "outcome": "cancelled",
                }
            )
        elif active.get("source") == "manual_dashboard":
            self.record_manual_event(
                {
                    "at": _iso(cancelled_at),
                    "robot": robot_id,
                    "rooms": area_ids,
                    "operations": [active.get("operation")],
                    "context_id": active.get("manual_context_id"),
                    "mode": active.get("manual_mode"),
                    "outcome": "cancelled",
                    "reason": reason,
                    "source": "manual_dashboard",
                }
            )
            coordinator.data.get("occurrences", {}).pop(active.get("room"), None)
            coordinator.data.get("water_confirmations", {}).pop(
                str(active.get("occurrence_id")), None
            )
        coordinator.data["recovery_events"].append(
            {"robot": robot_id, "rooms": area_ids, "at": _iso(cancelled_at), "reason": reason}
        )
        coordinator.data["recovery_events"] = coordinator.data["recovery_events"][-20:]
        if active.get("source") == "scheduler" and active.get("occurrence_id"):
            occurrence = coordinator.data.get("occurrences", {}).get(active.get("room"))
            stage_index = active.get("stage_index")
            if occurrence and isinstance(stage_index, int) and stage_index < len(occurrence.get("stages", [])):
                stage = occurrence["stages"][stage_index]
                stage["status"] = "pending"
                stage["started_at"] = None
        coordinator.data["active"][robot_id] = None
        coordinator._cancel_recovery_timer(robot_id)
        cancel_confirmation = getattr(
            coordinator, "_cancel_start_confirmation", None
        )
        if cancel_confirmation:
            cancel_confirmation(robot_id)

    def complete(
        self, robot_id: str, active: dict[str, Any], completion: datetime, confidence: str
    ) -> None:
        """Persist a confirmed completion and only learn direct observations."""

        coordinator = self._coordinator
        area_ids = self.active_rooms(active)
        operation = active["operation"]
        if active.get("source") == "manual_home_assistant":
            changed = self.apply_manual_deferral(
                robot_id,
                area_ids,
                list(active.get("requested_operations", ["vacuum"])),
                completion,
            )
            self.record_manual_event(
                {
                    "at": _iso(completion),
                    "robot": robot_id,
                    "rooms": area_ids,
                    "operations": list(active.get("requested_operations", ["vacuum"])),
                    "context_id": active.get("manual_context_id"),
                    "outcome": "completed",
                    "confidence": confidence,
                    "deferred": changed,
                }
            )
        else:
            detail = coordinator._room_data(active["room"])
            if operation == "vacuum":
                detail["vacuum"] = _iso(completion)
            if operation == "mop":
                detail["mop"] = _iso(completion)
            occurrence = coordinator.data.get("occurrences", {}).get(active["room"])
            occurrence_id = active.get("occurrence_id")
            stage_index = active.get("stage_index")
            if (
                occurrence
                and occurrence.get("occurrence_id") == occurrence_id
                and isinstance(stage_index, int)
                and stage_index < len(occurrence.get("stages", []))
            ):
                stage = occurrence["stages"][stage_index]
                stage["status"] = "completed"
                stage["reason"] = confidence
                stage["completed_at"] = _iso(completion)
                occurrence["current_stage"] = stage_index + 1
                detail["last_stage_outcome"] = "completed"
                detail["last_stage_reason"] = confidence
                detail["last_stage_at"] = _iso(completion)
                occurrence_complete = occurrence["current_stage"] >= len(
                    occurrence["stages"]
                )
                if occurrence_complete:
                    detail["cleaning"] = _iso(completion)
                    coordinator.data["occurrences"].pop(active["room"], None)
                    coordinator.data.get("water_confirmations", {}).pop(
                        str(occurrence_id), None
                    )
                    if operation == "mop":
                        coordinator.data.get("water_notification_episodes", {}).pop(
                            active["room"], None
                        )
                if active.get("source") == "manual_dashboard":
                    self.record_manual_event(
                        {
                            "at": _iso(completion),
                            "robot": robot_id,
                            "rooms": [active["room"]],
                            "operations": [operation],
                            "context_id": active.get("manual_context_id"),
                            "mode": active.get("manual_mode"),
                            "outcome": (
                                "completed" if occurrence_complete else "stage_completed"
                            ),
                            "confidence": confidence,
                            "source": "manual_dashboard",
                        }
                    )
            else:
                # A schema-one scheduler checkpoint completes one whole occurrence.
                detail["cleaning"] = _iso(completion)
        measured = active.get("measured_minutes")
        if (
            active.get("source") in {"scheduler", "manual_dashboard"}
            and confidence == "observed"
            and not active.get("interrupted")
            and isinstance(measured, (float, int))
            and measured > 0
        ):
            detail = coordinator._room_data(active["room"])
            durable_robot_id = coordinator.robot_registry_id(robot_id)
            detail.setdefault("duration_samples", []).append(
                {
                    "minutes": float(measured),
                    "operation": operation,
                    "passes": int(active.get("passes", 1)),
                    "robot": durable_robot_id,
                    "source": active.get("duration_source", "state_transition"),
                    "at": _iso(completion),
                }
            )
            detail["duration_samples"] = detail["duration_samples"][-50:]
        coordinator.data["recovery_events"].append(
            {"robot": robot_id, "rooms": area_ids, "at": _iso(completion), "reason": confidence}
        )
        coordinator.data["recovery_events"] = coordinator.data["recovery_events"][-20:]
        coordinator.data["active"][robot_id] = None
        coordinator._cancel_recovery_timer(robot_id)
        cancel_confirmation = getattr(
            coordinator, "_cancel_start_confirmation", None
        )
        if cancel_confirmation:
            cancel_confirmation(robot_id)

    def rebase_cancelled_floor(self, robot_id: str, cancelled_at: datetime) -> list[str]:
        """Rebase every enabled schedule on a physically cancelled robot's floor."""

        coordinator = self._coordinator
        robot = coordinator.discovery.robots.get(robot_id)
        if not robot or not robot.floor_id:
            return []
        from .models import rebase_due_times

        floor_robots = [
            candidate
            for candidate in coordinator.discovery.robots.values()
            if candidate.floor_id == robot.floor_id
            and candidate.supports_area_clean
            and coordinator._robot_settings(candidate).get("enabled", True)
        ]
        due_times: dict[str, datetime] = {}
        for room in coordinator.discovery.rooms.values():
            if room.floor_id != robot.floor_id or not coordinator._room_settings(room).get("enabled", True):
                continue
            due_times[f"{room.area_id}:cleaning"] = coordinator._room_due(room, "cleaning", cancelled_at)
        rebased = rebase_due_times(due_times, cancelled_at + timedelta(hours=24))
        for key, deferred_until in rebased.items():
            area_id, operation = key.rsplit(":", maxsplit=1)
            coordinator._room_data(area_id).setdefault("defer", {})[operation] = _iso(deferred_until)
        return list(rebased)
