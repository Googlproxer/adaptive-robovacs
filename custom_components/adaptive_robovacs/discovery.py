"""Public discovery API with conservative mopping capability detection."""

from .discovery_core import *  # noqa: F403
from .discovery_core import RobotProfile


def _supports_mopping(profile: RobotProfile) -> bool:
    """Require a dedicated mop control, not merely a mixed-mode option.

    Some vacuum-only installations still expose a generic `vac_and_mop` mode.
    A separate mop route or water-intensity select is a safe capability signal.
    """

    return bool(profile.mop_mode_select_entity_id or profile.mop_intensity_select_entity_id)


RobotProfile.supports_mopping = property(_supports_mopping)
