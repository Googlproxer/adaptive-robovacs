"""AST-enforced dependency boundaries for the layered architecture."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "custom_components" / "adaptive_robovacs"


def imported_modules(path: Path) -> set[str]:
    """Return absolute and relative module names imported by one source file."""

    result: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            prefix = "." * node.level
            result.add(f"{prefix}{node.module or ''}")
    return result


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_domain_and_reducers_have_no_home_assistant_imports(self) -> None:
        for name in ("models.py", "state.py", "planner.py", "jobs.py"):
            imports = imported_modules(PACKAGE / name)
            self.assertFalse(
                any(module.startswith("homeassistant") for module in imports),
                f"{name} imports Home Assistant: {sorted(imports)}",
            )

    def test_coordinator_is_only_a_push_snapshot_adapter(self) -> None:
        path = PACKAGE / "coordinator.py"
        imports = imported_modules(path)
        forbidden = {
            ".application",
            ".application_actions",
            ".application_dispatch",
            ".application_evaluation",
            ".application_events",
            ".application_faults",
            ".application_jobs",
            ".application_policy",
            ".application_settings",
            ".application_water",
            ".dispatch",
            ".gateway",
            ".jobs",
            ".lifecycle",
            ".models",
            ".notifications",
            ".planner",
            ".repair_service",
            ".repairs",
            ".state",
            ".storage",
        }
        self.assertTrue(forbidden.isdisjoint(imports), sorted(forbidden & imports))

        tree = ast.parse(path.read_text(encoding="utf-8"))
        coordinator = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef)
            and node.name == "AdaptiveRoboVacsCoordinator"
        )
        methods = {
            node.name
            for node in coordinator.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        self.assertEqual(
            methods,
            {"__init__", "_handle_application_update", "close", "async_execute"},
        )

    def test_application_core_composes_focused_components(self) -> None:
        """Keep policy, dispatch, recovery, and presentation out of the owner."""

        path = PACKAGE / "application.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        application = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "SchedulerApplication"
        )
        bases = {base.id for base in application.bases if isinstance(base, ast.Name)}
        self.assertEqual(
            bases,
            {
                "ApplicationActionsMixin",
                "ApplicationDispatchMixin",
                "ApplicationEvaluationMixin",
                "ApplicationEventsMixin",
                "ApplicationFaultMixin",
                "ApplicationJobsMixin",
                "ApplicationPolicyMixin",
                "ApplicationRecoveryMixin",
                "ApplicationSettingsMixin",
                "ApplicationWaterMixin",
            },
        )
        core_methods = {
            node.name
            for node in application.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        component_methods = {
            "async_evaluate",
            "async_manual_clean_room",
            "async_set_global",
            "_async_dispatch",
            "_async_reconcile_jobs",
            "_async_prepare_occurrence",
            "_on_state_changed",
            "_room_candidate",
            "_schedule_water_confirmation",
            "_sync_dispatch_fault_issues",
        }
        self.assertTrue(
            component_methods.isdisjoint(core_methods),
            sorted(component_methods & core_methods),
        )

    def test_application_components_do_not_depend_on_coordinator(self) -> None:
        for path in PACKAGE.glob("application_*.py"):
            self.assertNotIn(".coordinator", imported_modules(path), path.name)

    def test_platforms_depend_only_on_snapshots_and_typed_commands(self) -> None:
        forbidden = {
            ".application",
            ".application_actions",
            ".application_dispatch",
            ".application_evaluation",
            ".application_events",
            ".application_faults",
            ".application_jobs",
            ".application_policy",
            ".application_settings",
            ".application_water",
            ".command_queue",
            ".dispatch",
            ".gateway",
            ".jobs",
            ".planner",
            ".state",
            ".storage",
        }
        for name in (
            "button.py",
            "camera.py",
            "number.py",
            "select.py",
            "sensor.py",
            "switch.py",
        ):
            imports = imported_modules(PACKAGE / name)
            self.assertTrue(
                forbidden.isdisjoint(imports),
                f"{name} crosses into internals: {sorted(forbidden & imports)}",
            )

    def test_infrastructure_has_no_coordinator_back_reference(self) -> None:
        for name in (
            "gateway.py",
            "notifications.py",
            "repairs.py",
            "repair_service.py",
            "storage.py",
            "map_recovery.py",
            "map_recovery_store.py",
            "map_recovery_roborock.py",
        ):
            self.assertNotIn(".coordinator", imported_modules(PACKAGE / name), name)

    def test_obsolete_compatibility_modules_are_gone(self) -> None:
        self.assertFalse((PACKAGE / "runtime.py").exists())
        self.assertFalse((PACKAGE / "discovery_core.py").exists())
        self.assertFalse((PACKAGE / "api.py").exists())

    def test_config_entries_use_typed_runtime_data(self) -> None:
        runtime = (PACKAGE / "runtime_data.py").read_text(encoding="utf-8")
        integration = (PACKAGE / "integration_core.py").read_text(encoding="utf-8")
        self.assertIn("ConfigEntry[AdaptiveRoboVacsRuntimeData]", runtime)
        self.assertIn("entry.runtime_data = AdaptiveRoboVacsRuntimeData", integration)
        self.assertNotIn("hass.data[DOMAIN][entry.entry_id]", integration)


if __name__ == "__main__":
    unittest.main()
