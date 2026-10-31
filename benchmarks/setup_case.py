"""Load an optional private, anonymized setup description for local replay."""

from __future__ import annotations

import hashlib
import json

from benchmarks.report import SCENARIOS


def cases(args):
    if not args.setup:
        scenarios = (
            SCENARIOS
            if args.scenario == "all"
            else {args.scenario: SCENARIOS[args.scenario]}
        )
        return [(name, dimensions, None) for name, dimensions in scenarios.items()]
    setup = json.loads(args.setup.read_text(encoding="utf-8"))
    if setup.get("schema") != 1 or not setup.get("rooms") or not setup.get("robots"):
        raise ValueError("setup needs schema 1 and nonempty rooms and robots lists")
    floors = setup["floors"]
    for item in (*setup["rooms"], *setup["robots"]):
        if not isinstance(item["floor"], int) or not 0 <= item["floor"] < floors:
            raise ValueError("invalid floor index in setup")
    dimensions = {
        "rooms": len(setup["rooms"]),
        "robots": len(setup["robots"]),
        "entities": setup["entities"],
        "floors": floors,
        "history": max(room["history_count"] for room in setup["rooms"]),
        "total_history": sum(room["history_count"] for room in setup["rooms"]),
        "integration_entities": setup.get("integration_entities", 0),
        "setup_digest": hashlib.sha256(
            json.dumps(setup, sort_keys=True).encode()
        ).hexdigest(),
    }
    # Names distinguish the current snapshot from the ready-to-plan variant.
    return [
        ("local_setup", dimensions, setup),
        ("local_due", dimensions, {**setup, "force_due": True}),
    ]
