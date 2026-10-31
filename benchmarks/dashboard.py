"""Benchmark the shipped floorplan card in a real, headless Chromium browser."""

from __future__ import annotations

import importlib.metadata

from playwright.sync_api import sync_playwright

from benchmarks.report import (
    ROOT,
    environment,
    finish,
    parser,
    statistics_ms,
)
from benchmarks.setup_case import cases


def main() -> int:
    args = parser(__doc__).parse_args()
    results = []
    env = environment()
    env["playwright"] = importlib.metadata.version("playwright")
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        env["chromium"] = browser.version
        try:
            for name, dimensions, setup in cases(args):
                if name == "local_due":
                    continue
                print(f"Preparing dashboard {name}: {dimensions}", flush=True)
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                errors = []
                page.on(
                    "pageerror", lambda error, errors=errors: errors.append(str(error))
                )
                try:
                    page.set_content("""<!doctype html><style>
                      :root { font:16px system-ui; --primary-color:#03a9f4;
                        --primary-text-color:#212121; --secondary-text-color:#727272;
                        --secondary-background-color:#eee; --card-background-color:#fff;
                        --divider-color:#ddd; --success-color:#4caf50;
                        --warning-color:#ff9800; --error-color:#db4437;
                        --disabled-text-color:#aaa; }
                      body { margin:16px; } main { display:grid; gap:16px; }
                    </style><main></main>""")
                    # No HA connection, resource server, or remote scripts.
                    page.add_script_tag(
                        content=(
                            "customElements.define('ha-card', "
                            "class extends HTMLElement {});"
                        )
                    )
                    page.add_script_tag(
                        path=str(
                            ROOT
                            / "custom_components/adaptive_robovacs/frontend"
                            / "adaptive-robovacs-dashboard.js"
                        )
                    )
                    page.add_script_tag(
                        path=str(ROOT / "benchmarks/dashboard_fixture.js")
                    )
                    measurements = page.evaluate(
                        "options => window.runDashboardBenchmark(options)",
                        {
                            "dimensions": dimensions,
                            "samples": args.samples,
                            "warmup": args.warmup,
                            "setup": setup,
                        },
                    )
                    if errors:
                        raise RuntimeError("; ".join(errors))
                    for workload, measurement in measurements.items():
                        results.append(
                            {
                                "scenario": name,
                                "workload": workload,
                                "dimensions": dimensions,
                                "budget_ms": 16.7,
                                **statistics_ms(measurement.pop("cpu_samples_ms")),
                                **measurement,
                            }
                        )
                    if name == "small":
                        args.output.parent.mkdir(parents=True, exist_ok=True)
                        page.screenshot(
                            path=str(args.output.parent / "dashboard.png"),
                            full_page=True,
                        )
                finally:
                    page.close()
        finally:
            browser.close()
    return finish(args, "dashboard", env, results)


if __name__ == "__main__":
    raise SystemExit(main())
