"""Static contracts for actionable Home Assistant Repairs integration."""

import json
from pathlib import Path
import unittest


PACKAGE = Path(__file__).parents[1] / "custom_components" / "adaptive_robovacs"


class RepairsContractTests(unittest.TestCase):
    def test_issue_and_fix_flow_translations_are_complete(self) -> None:
        strings = json.loads((PACKAGE / "strings.json").read_text(encoding="utf-8"))
        translations = json.loads(
            (PACKAGE / "translations" / "en.json").read_text(encoding="utf-8")
        )
        self.assertEqual(strings, translations)
        self.assertNotIn("repairs", translations)
        for key in (
            "scheduler_halted",
            "two_pass_no_longer_supported",
            "notification_delivery_failed",
            "cleaning_program_incompatible",
        ):
            self.assertIn(key, translations["issues"])
            issue = translations["issues"][key]
            self.assertIn("fix_flow", issue)
            self.assertNotIn("description", issue)
            self.assertIn(
                "confirm",
                issue["fix_flow"]["step"],
            )
            self.assertIn("recheck_failed", issue["fix_flow"]["error"])

    def test_scheduler_halt_issue_is_persistent_fixable_and_error_severity(self) -> None:
        source = (PACKAGE / "repairs_manager.py").read_text(encoding="utf-8")
        self.assertIn("is_fixable=True", source)
        self.assertIn("is_persistent=True", source)
        self.assertIn("severity=ir.IssueSeverity.ERROR", source)
        self.assertIn('translation_key="scheduler_halted"', source)

    def test_repair_flow_uses_the_same_non_dispatching_resume_method(self) -> None:
        repairs = (PACKAGE / "repairs.py").read_text(encoding="utf-8")
        coordinator = (PACKAGE / "coordinator.py").read_text(encoding="utf-8")
        self.assertIn("async_recheck_and_resume", repairs)
        method = coordinator[coordinator.index("    async def async_recheck_and_resume"):]
        method = method[:method.index("    def _cancel_start_confirmation")]
        self.assertIn("runtime.async_preflight", method)
        self.assertNotIn("async_dispatch", method)
        self.assertNotIn("services.async_call", method)
        self.assertIn("description_placeholders=_description_placeholders(self)", repairs)

    def test_opening_a_repair_never_submits_its_confirmation(self) -> None:
        repairs = (PACKAGE / "repairs.py").read_text(encoding="utf-8")
        self.assertNotIn("async_step_confirm(user_input)", repairs)
        self.assertEqual(repairs.count("return await self.async_step_confirm()"), 3)

    def test_repairs_and_public_fault_state_exclude_native_targets_and_raw_errors(self) -> None:
        source = "".join(
            (PACKAGE / path).read_text(encoding="utf-8")
            for path in (
                "repairs_manager.py",
                "repairs.py",
                "state.py",
                "sensor.py",
                "projections.py",
            )
        )
        self.assertNotIn('"segments"', source)
        self.assertNotIn('"app_segment_clean"', source)
        self.assertNotIn("raw_exception", source)


if __name__ == "__main__":
    unittest.main()
