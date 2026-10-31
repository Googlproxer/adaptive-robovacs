"""Shared statistics and portable, synthetic-only benchmark reports."""

from __future__ import annotations

import argparse
import json
import math
import platform
import statistics
import subprocess
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = {
    "small": {"rooms": 12, "robots": 2, "entities": 2000, "history": 28},
    "medium": {"rooms": 48, "robots": 4, "entities": 8000, "history": 112},
    "large": {"rooms": 96, "robots": 8, "entities": 20000, "history": 336},
}


def positive_int(value: str) -> int:
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return result


def parser(description: str) -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=description)
    case = result.add_mutually_exclusive_group()
    case.add_argument("--scenario", choices=["all", *SCENARIOS], default="all")
    case.add_argument("--setup", type=Path, help="Private anonymized setup JSON")
    result.add_argument("--samples", type=positive_int, default=25)
    result.add_argument("--warmup", type=positive_int, default=3)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--baseline", type=Path)
    result.add_argument(
        "--check", action="store_true", help="Fail on budgets or baseline regressions"
    )
    return result


def statistics_ms(values: list[float]) -> dict[str, float]:
    """Use nearest-rank p95, keeping each invocation as an independent sample."""
    ordered = sorted(values)
    return {
        "median_ms": statistics.median(ordered),
        "p95_ms": ordered[math.ceil(len(ordered) * 0.95) - 1],
        "max_ms": ordered[-1],
        "mean_ms": statistics.mean(ordered),
    }


def environment() -> dict[str, object]:
    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()

    return {
        "python": platform.python_version(),
        "platform": platform.system(),
        "architecture": platform.machine(),
        "commit": git("rev-parse", "HEAD"),
        "dirty": bool(git("status", "--porcelain", "--untracked-files=normal")),
    }


def finish(args: argparse.Namespace, suite: str, env: dict, results: list[dict]) -> int:
    warnings = []
    baseline = None
    if args.baseline:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        if baseline["schema"] != 1 or baseline["suite"] != suite:
            raise ValueError("baseline must have the same schema and suite")
        for key in env.keys() - {"commit", "dirty"}:
            if baseline["environment"].get(key) != env[key]:
                raise ValueError(f"baseline environment differs: {key}")
    print(f"\n{suite}: milliseconds per invocation (nearest-rank p95)")
    print(f"{'Scenario / workload':46} {'median':>9} {'p95':>9} {'max':>9}")
    for row in results:
        key = f"{row['scenario']}/{row['workload']}"
        if row["p95_ms"] > row["budget_ms"]:
            warnings.append(f"{key}: p95 exceeds {row['budget_ms']} ms budget")
        if baseline:
            previous = next(
                (
                    item
                    for item in baseline["results"]
                    if (item["scenario"], item["workload"])
                    == (row["scenario"], row["workload"])
                ),
                None,
            )
            if previous is None or previous["dimensions"] != row["dimensions"]:
                raise ValueError(
                    f"baseline workload missing or dimensions differ: {key}"
                )
            increase = row["p95_ms"] - previous["p95_ms"]
            row["baseline_p95_ms"] = previous["p95_ms"]
            if increase > max(1.0, previous["p95_ms"] * 0.25):
                warnings.append(f"{key}: p95 regressed by over 25% and 1 ms")
        print(
            f"{key:46} {row['median_ms']:9.3f}"
            f" {row['p95_ms']:9.3f} {row['max_ms']:9.3f}"
        )
    report = {
        "schema": 1,
        "suite": suite,
        "created_at": datetime.now(UTC).isoformat(),
        "environment": env,
        "warmup": args.warmup,
        "samples": args.samples,
        "results": results,
        "warnings": warnings,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for warning in warnings:
        print(f"REVIEW: {warning}")
    print(f"Report: {args.output}")
    return int(args.check and bool(warnings))
