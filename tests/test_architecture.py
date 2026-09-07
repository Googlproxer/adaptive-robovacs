"""AST-enforced dependency boundaries for the layered architecture."""

from __future__ import annotations

import ast
import unittest
from importlib.util import resolve_name
from pathlib import Path

ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "custom_components" / "adaptive_robovacs"
PACKAGE_NAME = "custom_components.adaptive_robovacs"
APPLICATION = PACKAGE / "application"


def imported_modules(path: Path, *, source: str | None = None) -> set[str]:
    """Resolve imports relative to their source, including imported submodules."""

    result: set[str] = set()
    package = ".".join(path.parent.relative_to(ROOT).parts)
    if source is None:
        source = path.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = resolve_name(f"{'.' * node.level}{module}", package)
            result.add(module)
            result.update(
                f"{module}.{alias.name}" for alias in node.names if alias.name != "*"
            )
    return {
        module.removeprefix(PACKAGE_NAME)
        if module == PACKAGE_NAME or module.startswith(f"{PACKAGE_NAME}.")
        else module
        for module in result
    }


def forbidden_imports(imports: set[str], forbidden: set[str]) -> set[str]:
    """Match a forbidden module and its descendants, not similarly named peers."""

    return {
        module
        for module in imports
        if any(module == name or module.startswith(f"{name}.") for name in forbidden)
    }


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
        self.assertFalse(forbidden_imports(imports, forbidden))

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

        path = APPLICATION / "core.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        application = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "SchedulerApplication"
        )
        bases = [base.id for base in application.bases if isinstance(base, ast.Name)]
        self.assertEqual(
            bases,
            [
                "ApplicationLegacyMapMixin",
                "ApplicationSettingsMixin",
                "ApplicationEventsMixin",
                "ApplicationEvaluationMixin",
                "ApplicationDispatchMixin",
                "ApplicationActionsMixin",
                "ApplicationPolicyMixin",
                "ApplicationFaultMixin",
                "ApplicationRecoveryMixin",
                "ApplicationRoomRecoveryMixin",
                "ApplicationJobsMixin",
                "ApplicationWaterMixin",
            ],
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
        paths = list(APPLICATION.rglob("*.py"))
        self.assertTrue(paths, "No application modules were checked")
        for path in paths:
            self.assertFalse(
                forbidden_imports(imported_modules(path), {".coordinator"}),
                path.name,
            )

    def test_platforms_depend_only_on_snapshots_and_typed_commands(self) -> None:
        forbidden = {
            ".application",
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
            "number.py",
            "select.py",
            "sensor.py",
            "switch.py",
        ):
            imports = imported_modules(PACKAGE / name)
            violations = forbidden_imports(imports, forbidden)
            self.assertFalse(
                violations,
                f"{name} crosses into internals: {sorted(violations)}",
            )

    def test_infrastructure_has_no_coordinator_back_reference(self) -> None:
        for name in (
            "gateway.py",
            "notifications.py",
            "repairs.py",
            "repair_service.py",
            "storage.py",
        ):
            self.assertFalse(
                forbidden_imports(imported_modules(PACKAGE / name), {".coordinator"}),
                name,
            )

    def test_obsolete_compatibility_modules_are_gone(self) -> None:
        self.assertFalse((PACKAGE / "runtime.py").exists())
        self.assertFalse((PACKAGE / "discovery_core.py").exists())
        self.assertFalse((PACKAGE / "api.py").exists())
        self.assertFalse((PACKAGE / "application.py").exists())
        self.assertFalse(list(PACKAGE.glob("application_*.py")))

    def test_application_boundary_covers_relative_and_absolute_submodules(self) -> None:
        for source in (
            "from .application import SchedulerApplication",
            "from .application.core import SchedulerApplication",
            "from . import application",
            "import custom_components.adaptive_robovacs.application.core as core",
            "from custom_components.adaptive_robovacs import application",
            "from custom_components.adaptive_robovacs.application.policy import *",
        ):
            with self.subTest(source=source):
                imports = imported_modules(PACKAGE / "sensor.py", source=source)
                self.assertTrue(forbidden_imports(imports, {".application"}))

    def test_nested_coordinator_imports_are_forbidden(self) -> None:
        for source in (
            "from ..coordinator import AdaptiveRoboVacsCoordinator",
            "from .. import coordinator",
            "import custom_components.adaptive_robovacs.coordinator as coordinator",
            "def callback():\n    from .. import coordinator",
        ):
            with self.subTest(source=source):
                imports = imported_modules(APPLICATION / "events.py", source=source)
                self.assertTrue(forbidden_imports(imports, {".coordinator"}))

    def test_boundaries_distinguish_sibling_names_and_relative_depth(self) -> None:
        imports = imported_modules(
            APPLICATION / "core.py",
            source=(
                "from .jobs import ApplicationJobsMixin\n"
                "from ..jobs import active_rooms"
            ),
        )
        self.assertIn(".application.jobs", imports)
        self.assertIn(".jobs", imports)
        self.assertFalse(
            forbidden_imports(
                {".application_notes", ".coordinator_helpers"},
                {".application", ".coordinator"},
            )
        )

    def test_config_entries_use_typed_runtime_data(self) -> None:
        runtime = (PACKAGE / "runtime_data.py").read_text(encoding="utf-8")
        integration = (PACKAGE / "integration_core.py").read_text(encoding="utf-8")
        self.assertIn("ConfigEntry[AdaptiveRoboVacsRuntimeData]", runtime)
        self.assertIn("entry.runtime_data = AdaptiveRoboVacsRuntimeData", integration)
        self.assertNotIn("hass.data[DOMAIN][entry.entry_id]", integration)


if __name__ == "__main__":
    unittest.main()
