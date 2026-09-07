"""Typed commands accepted by the scheduler application."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.core import Context

from .floor_plans import FloorPlanWrite
from .models import EvaluationCause, EvaluationMode, ManualCleanRequest
from .state import FrozenJsonObject


@dataclass(frozen=True, slots=True)
class EvaluateCommand:
    """Evaluate current scheduling state and optionally dispatch work."""

    mode: EvaluationMode
    cause: EvaluationCause
    detail: str | None = None
    coalesce: bool = False

    @property
    def dry_run(self) -> bool:
        return self.mode is EvaluationMode.PREVIEW

    @property
    def reason(self) -> str:
        return self.detail or self.cause.value


@dataclass(frozen=True, slots=True)
class StateChangedCommand:
    """Preserve one watched Home Assistant state transition in FIFO order."""

    entity_id: str
    old_state: str | None
    new_state: str | None
    changed_at: datetime | None


@dataclass(frozen=True, slots=True)
class RefreshDiscoveryCommand:
    """Refresh registry discovery after topology metadata changes."""

    reason: str
    coalesce: bool = True


@dataclass(frozen=True, slots=True)
class SetGlobalCommand:
    """Change one global scheduler setting."""

    key: str
    value: object


@dataclass(frozen=True, slots=True)
class SetRoomSettingCommand:
    """Change one room setting."""

    area_id: str
    key: str
    value: object


@dataclass(frozen=True, slots=True)
class SetRobotSettingCommand:
    """Change one robot setting."""

    robot_entity_id: str
    key: str
    value: object


@dataclass(frozen=True, slots=True)
class SetRoomCleaningPeriodCommand:
    """Change the simplified room cadence/window option."""

    area_id: str
    option: str


@dataclass(frozen=True, slots=True)
class SetRoomCleaningProfileCommand:
    """Change whether a room inherits or customizes its profile."""

    area_id: str
    option: str


@dataclass(frozen=True, slots=True)
class ManualCleanRoomCommand:
    """Start one explicit dashboard manual-clean request."""

    area_id: str
    mode: str
    context_id: str | None = None
    user_id: str | None = None


@dataclass(frozen=True, slots=True)
class RecordManualCleanCommand:
    """Record an observed Home Assistant manual clean."""

    robot_entity_id: str
    area_ids: tuple[str, ...]
    operations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ObservedManualCleanCommand:
    """Checkpoint one parsed user-initiated Home Assistant room clean."""

    request: ManualCleanRequest
    context_id: str


@dataclass(frozen=True, slots=True)
class WaterConfirmationResponseCommand:
    """Apply one Companion notification response in FIFO order."""

    action: str | None = None
    request_id: str | None = None
    tag: str | None = None
    dismissed: bool = False


@dataclass(frozen=True, slots=True)
class ExpireWaterConfirmationCommand:
    """Expire one durable water-confirmation request."""

    request_id: str


@dataclass(frozen=True, slots=True)
class StopAndReturnCommand:
    """Stop one robot and request return to its dock."""

    robot_entity_id: str
    context: Context | None = None


@dataclass(frozen=True, slots=True)
class RecheckAndResumeCommand:
    """Recheck a global or robot-scoped dispatch fault without dispatching."""

    robot_registry_id: str | None = None


@dataclass(frozen=True, slots=True)
class AcknowledgeRoomRecoveryCommand:
    """Allow a later retry of exactly one detached interruption episode."""

    area_id: str
    recovery_id: str


@dataclass(frozen=True, slots=True)
class AcknowledgeRobotErrorCommand:
    """Explicitly abandon an unassociated legacy error checkpoint."""

    robot_registry_id: str
    held_at: str


@dataclass(frozen=True, slots=True)
class RecheckRoomFaultCommand:
    """Recheck one room-scoped dispatch failure."""

    area_id: str


@dataclass(frozen=True, slots=True)
class RecheckTwoPassCompatibilityCommand:
    """Recheck one room's two-pass compatibility."""

    area_id: str


@dataclass(frozen=True, slots=True)
class RecheckCleaningProgramCommand:
    """Recheck one room's ordered cleaning program."""

    area_id: str


@dataclass(frozen=True, slots=True)
class RecheckNotificationTargetsCommand:
    """Recheck Companion notification delivery availability."""


@dataclass(frozen=True, slots=True)
class ClearLegacyDeferralsCommand:
    """Clear reviewed legacy deferrals for selected rooms."""

    area_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SetRoomAdjacencyCommand:
    """Replace one room's direct-neighbor set."""

    area_id: str
    neighbor_area_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SaveFloorPlanCommand:
    """Atomically save one floor-plan dashboard revision."""

    request: FloorPlanWrite


@dataclass(frozen=True, slots=True)
class ListRetainedMapsCommand:
    """Return retained maps for one currently discovered robot."""

    robot_entity_id: str


@dataclass(frozen=True, slots=True)
class CaptureMapSnapshotCommand:
    """Capture one read-only retained-map snapshot."""

    robot_entity_id: str
    trigger: str = "manual"


@dataclass(frozen=True, slots=True)
class ActivateRetainedMapCommand:
    """Activate one retained map after explicit confirmation."""

    robot_entity_id: str
    map_id: str
    confirm: bool


@dataclass(frozen=True, slots=True)
class VerifyRetainedMapCommand:
    """Finish or reject a held map-recovery transaction."""

    robot_entity_id: str
    confirm: bool


@dataclass(frozen=True, slots=True)
class SelectMapPreviewCommand:
    """Select a cached preview without contacting a robot."""

    robot_entity_id: str
    option: str


type SchedulerCommand = (
    EvaluateCommand
    | StateChangedCommand
    | RefreshDiscoveryCommand
    | SetGlobalCommand
    | SetRoomSettingCommand
    | SetRobotSettingCommand
    | SetRoomCleaningPeriodCommand
    | SetRoomCleaningProfileCommand
    | ManualCleanRoomCommand
    | RecordManualCleanCommand
    | ObservedManualCleanCommand
    | WaterConfirmationResponseCommand
    | ExpireWaterConfirmationCommand
    | StopAndReturnCommand
    | RecheckAndResumeCommand
    | RecheckRoomFaultCommand
    | AcknowledgeRoomRecoveryCommand
    | AcknowledgeRobotErrorCommand
    | RecheckTwoPassCompatibilityCommand
    | RecheckCleaningProgramCommand
    | RecheckNotificationTargetsCommand
    | ClearLegacyDeferralsCommand
    | SetRoomAdjacencyCommand
    | SaveFloorPlanCommand
    | ListRetainedMapsCommand
    | CaptureMapSnapshotCommand
    | ActivateRetainedMapCommand
    | VerifyRetainedMapCommand
    | SelectMapPreviewCommand
)


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Immutable result carried through the application command queue."""

    payload: FrozenJsonObject

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> CommandResult:
        """Freeze one boundary response before it leaves the transaction."""

        return cls(FrozenJsonObject.from_mapping(payload))

    def as_response(self) -> dict[str, Any]:
        """Serialize only at a Home Assistant service/test boundary."""

        return self.payload.to_mapping()


type SchedulerCommandResult = CommandResult | None
