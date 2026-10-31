"""Protect performance comparisons from misleading statistics and baselines."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from benchmarks.report import finish, statistics_ms
from benchmarks.setup_case import cases


class BenchmarkReportingTests(unittest.TestCase):
    def test_private_setup_preserves_uneven_history_and_separates_due_variant(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "setup.json"
            fixture = {
                "schema": 1,
                "floors": 1,
                "entities": 50,
                "rooms": [
                    {"floor": 0, "history_count": 0},
                    {"floor": 0, "history_count": 1000},
                ],
                "robots": [{"floor": 0}],
            }
            path.write_text(json.dumps(fixture), encoding="utf-8")
            current, due = cases(argparse.Namespace(setup=path))
            self.assertEqual(current[1]["total_history"], 1000)
            self.assertEqual(current[1]["history"], 1000)
            self.assertNotIn("force_due", current[2])
            self.assertTrue(due[2]["force_due"])
            self.assertEqual(current[2]["rooms"], due[2]["rooms"])
            fixture["rooms"][0]["floor"] = 1
            path.write_text(json.dumps(fixture), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "floor index"):
                cases(argparse.Namespace(setup=path))

    def test_tail_latency_is_not_an_average(self) -> None:
        timing = statistics_ms([1.0] * 18 + [20.0, 100.0])
        self.assertEqual(timing["median_ms"], 1.0)
        self.assertEqual(timing["p95_ms"], 20.0)
        self.assertEqual(timing["max_ms"], 100.0)

    def test_budget_is_advisory_unless_check_is_requested(self) -> None:
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            args = self.options(Path(directory))
            self.assertEqual(finish(args, "backend", {}, [self.row(30)]), 0)
            report = json.loads(args.output.read_text(encoding="utf-8"))
            self.assertEqual(len(report["warnings"]), 1)
            args.check = True
            self.assertEqual(finish(args, "backend", {}, [self.row(30)]), 1)

    def test_baseline_detects_regression_and_rejects_incompatible_comparison(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            args = self.options(Path(directory))
            env = {"python": "3.14.9", "commit": "old"}
            finish(args, "backend", env, [self.row(4)])
            args.baseline = args.output
            args.output = Path(directory) / "new.json"
            args.check = True
            self.assertEqual(finish(args, "backend", env, [self.row(6)]), 1)
            # Sub-millisecond differences must not trigger relative-noise alarms.
            self.assertEqual(finish(args, "backend", env, [self.row(4.5)]), 0)
            with self.assertRaisesRegex(ValueError, "environment differs"):
                finish(args, "backend", {**env, "python": "3.15.0"}, [self.row(4)])
            changed = self.row(4)
            changed["dimensions"] = {"rooms": 100}
            with self.assertRaisesRegex(ValueError, "dimensions differ"):
                finish(args, "backend", env, [changed])
            with self.assertRaisesRegex(ValueError, "same schema and suite"):
                finish(args, "dashboard", env, [self.row(4)])

    @staticmethod
    def options(directory):
        return argparse.Namespace(
            output=directory / "baseline.json",
            baseline=None,
            warmup=3,
            samples=25,
            check=False,
        )

    @staticmethod
    def row(milliseconds):
        return {
            "scenario": "small",
            "workload": "evaluation_preview",
            "dimensions": {"rooms": 12},
            "budget_ms": 25,
            **statistics_ms([milliseconds]),
        }


class EventLoopProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_observes_blocking_inside_an_awaited_worker(self) -> None:
        from benchmarks.__main__ import measure

        def intentional_block():
            # Deliberately block a worker to test the observer, not performance.
            time.sleep(0.01)

        async def worker():
            intentional_block()

        async def queued_call():
            await asyncio.create_task(worker())

        result = await measure(queued_call, samples=1, warmup=1)
        self.assertGreaterEqual(result["max_event_loop_delay_ms"], 5)
