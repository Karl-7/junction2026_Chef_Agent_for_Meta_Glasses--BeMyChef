"""Run a scripted policy against a live kitchen sidecar, through the shared orchestrator.

    python -m policies.run --url http://kitchen:8000/mcp --scenario scenario.json --policy oracle
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from cooking.orchestrator import Session
from cooking.replay import McpClient, ReplayKitchen
from policies.scripted import Scripted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8000/mcp")
    ap.add_argument("--scenario", required=True, help="scenario json with annotations (oracle only)")
    ap.add_argument("--policy", default="oracle", choices=["oracle", "early_stop"])
    a = ap.parse_args()
    scenario = json.loads(Path(a.scenario).read_text())
    client = McpClient(a.url)
    try:
        kitchen = ReplayKitchen(client.call)
        Session(kitchen, kitchen).run(Scripted(scenario, stop_after_accept=a.policy == "early_stop"))
    finally:
        client.close()


if __name__ == "__main__":
    main()
