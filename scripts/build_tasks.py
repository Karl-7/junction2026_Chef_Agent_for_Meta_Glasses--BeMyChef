"""Generate one Harbor task directory per scenario under tasks/.

    python -m scripts.build_tasks --scenarios synthetic-stirfry

Layout per task:
  instruction.md, task.toml
  environment/Dockerfile            assistant container ("main"): no scenario, no annotations
  environment/docker-compose.yaml   adds the "kitchen" sidecar
  environment/kitchen/              sidecar image: kitchen package + the scenario with annotations
  tests/test.sh                     verifier: fetches the server-side score with a secret token
  solution/                         oracle: scripted policy + scenario, driven through the orchestrator
"""
from __future__ import annotations

import argparse
import json
import secrets
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MCP_PIN = "mcp==1.26.0"
PY_IMAGE = "python:3.12-slim-bookworm"

INSTRUCTION = """\
# Cooking assistant

You are a cooking assistant on a user's smart glasses. The glasses are reachable only through the
MCP server named `kitchen`; there is nothing to find on this machine's filesystem.

Loop: call `next_utterance` to hear what the user says, act with the other tools, and repeat until
it returns `{"done": true}`. You speak only through `say`.

Before cooking: when the user asks you to look at the dish, take a frame (`capture_frame`), search
(`search_recipes`), show one candidate (`show_recipe`) and ask whether it is OK. After a "no", show
a different recipe, never one already rejected; search again when candidates run out and say so.
After a "yes", read the recipe (`load_recipe`) and only then close the display (`hide_recipe`).

While cooking: answer only when called by name ("Gemini"). Take a fresh frame before every answer.
Answer from the recipe and what the frame shows, one short action at a time. For "which one"
questions, `highlight` the object (box `[ymin, xmin, ymax, xmax]`, 0..1000) and keep speech short.
Be conservative about doneness. Speak the user's language. Say something within 3 seconds of each
utterance: acknowledge first, then run slow operations.
"""

TASK_TOML = """\
schema_version = "1.4"
artifacts = [
  {{ source = "/var/kitchen/trajectory.json", destination = "kitchen_trajectory.json", service = "kitchen" }},
]

[task]
name = "cooking-assistant/{scenario}"
version = "1.0.0"
description = "Help a user choose a recipe and cook it, by voice and visual highlights, in a replayed kitchen."
keywords = ["assistant", "replay", "mcp", "vision"]

[metadata]
scenario = "{scenario}"

[verifier]
timeout_sec = 120.0

[agent]
timeout_sec = 1800.0

[environment]
build_timeout_sec = 900.0
os = "linux"

[[environment.mcp_servers]]
name = "kitchen"
transport = "streamable-http"
url = "http://kitchen:8000/mcp"
"""

MAIN_DOCKERFILE = f"""\
FROM {PY_IMAGE}
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \\
    && rm -rf /var/lib/apt/lists/*
# The MCP client library is used by the assistant code; it holds no scenario data.
RUN pip install --no-cache-dir "{MCP_PIN}"
WORKDIR /app
"""

KITCHEN_DOCKERFILE = f"""\
FROM {PY_IMAGE}
RUN pip install --no-cache-dir "{MCP_PIN}"
WORKDIR /srv
COPY kitchen /srv/kitchen
COPY scenario.json /srv/scenario.json
ENV KITCHEN_SCENARIO=/srv/scenario.json KITCHEN_LOG_DIR=/var/kitchen
EXPOSE 8000
CMD ["python", "-m", "kitchen.server"]
"""

COMPOSE = """\
services:
  main:
    depends_on:
      kitchen:
        condition: service_healthy
  kitchen:
    build:
      context: ./kitchen
    environment:
      GRADER_TOKEN: "{token}"
    expose:
      - "8000"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"]
      interval: 2s
      timeout: 5s
      retries: 30
      start_period: 3s
"""

TEST_SH = """\
#!/bin/bash
# The score is computed by the kitchen sidecar from its own record; nothing the assistant wrote is used.
set -u
mkdir -p /logs/verifier
if curl -sf -H "x-grader-token: {token}" http://kitchen:8000/_grade -o /logs/verifier/grade.json; then
  python3 -c "import json; d=json.load(open('/logs/verifier/grade.json')); print(d['reward'])" \\
    > /logs/verifier/reward.txt || echo 0 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
cat /logs/verifier/reward.txt
"""

SOLVE_SH = """\
#!/bin/bash
# Scripted policy acting from the annotations, through the same orchestrator as the assistant.
set -e
cd /solution
python3 -m policies.run --url http://kitchen:8000/mcp --scenario scenario.json --policy {policy}
"""

IGNORE = shutil.ignore_patterns("__pycache__")


def build(scenario: str, out: Path, policy: str = "oracle") -> Path:
    src = ROOT / "scenarios" / f"{scenario}.json"
    json.loads(src.read_text())             # fail here, not in the container, on a malformed scenario
    token = secrets.token_hex(24)
    d = out / scenario
    if d.exists():
        shutil.rmtree(d)
    env, sol = d / "environment", d / "solution"
    (env / "kitchen").mkdir(parents=True)
    (d / "tests").mkdir()
    sol.mkdir()

    (d / "instruction.md").write_text(INSTRUCTION)
    (d / "task.toml").write_text(TASK_TOML.format(scenario=scenario))
    (env / "Dockerfile").write_text(MAIN_DOCKERFILE)
    (env / "docker-compose.yaml").write_text(COMPOSE.format(token=token))
    (env / "kitchen" / "Dockerfile").write_text(KITCHEN_DOCKERFILE)
    shutil.copytree(ROOT / "kitchen", env / "kitchen" / "kitchen", ignore=IGNORE)
    shutil.copy(src, env / "kitchen" / "scenario.json")
    (d / "tests" / "test.sh").write_text(TEST_SH.format(token=token))
    (sol / "solve.sh").write_text(SOLVE_SH.format(policy=policy))
    shutil.copy(src, sol / "scenario.json")
    for pkg in ("cooking", "policies"):
        shutil.copytree(ROOT / pkg, sol / pkg, ignore=IGNORE)
    for f in (d / "tests" / "test.sh", sol / "solve.sh"):
        f.chmod(0o755)
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", nargs="+", default=["synthetic-stirfry"])
    ap.add_argument("--out", default=str(ROOT / "tasks"))
    ap.add_argument("--oracle-policy", default="oracle", choices=["oracle", "early_stop"],
                    help="what solution/solve.sh runs (early_stop = the early-stop baseline)")
    a = ap.parse_args()
    for s in a.scenarios:
        print(build(s, Path(a.out), a.oracle_policy))


if __name__ == "__main__":
    main()
