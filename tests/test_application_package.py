"""Import and clock contracts for the application package."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from textwrap import dedent


class ApplicationPackageTests(unittest.TestCase):
    def test_fresh_component_import_preserves_public_class_and_shared_clock(
        self,
    ) -> None:
        """Catch circular imports without relying on the suite's module cache."""

        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                dedent("""\
                    import importlib
                    import pkgutil
                    from unittest.mock import patch

                    name = "custom_components.adaptive_robovacs.application"
                    recovery = importlib.import_module(name + ".recovery")
                    application = importlib.import_module(name)
                    core = importlib.import_module(name + ".core")
                    assert application.SchedulerApplication is core.SchedulerApplication
                    assert application.__all__ == ["SchedulerApplication"]
                    for module in pkgutil.iter_modules(application.__path__):
                        importlib.import_module(name + "." + module.name)
                    marker = object()
                    with patch.object(core, "_now", return_value=marker):
                        assert recovery._now() is marker
                """),
            ],
            cwd=Path(__file__).parents[1],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
