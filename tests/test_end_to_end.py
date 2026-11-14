"""One generated Harbor task, run without Docker: the sidecar as a real MCP server process, the
task's own solution/ as the assistant, and the grade fetched the way tests/test.sh does."""
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from scripts.build_tasks import build


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(url, token=None):
    req = urllib.request.Request(url, headers={"x-grader-token": token} if token else {})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


@pytest.fixture
def task(tmp_path, request):
    """A built task with its sidecar running: (task dir, base url, grader token)."""
    d = build("synthetic-stirfry", tmp_path, policy=request.param)
    token = re.search(r"x-grader-token: (\w+)", (d / "tests" / "test.sh").read_text()).group(1)
    assert token in (d / "environment" / "docker-compose.yaml").read_text()
    port = free_port()
    sidecar = d / "environment" / "kitchen"
    proc = subprocess.Popen(
        [sys.executable, "-m", "kitchen.server"], cwd=sidecar,
        env={**os.environ, "KITCHEN_SCENARIO": str(sidecar / "scenario.json"), "GRADER_TOKEN": token,
             "KITCHEN_LOG_DIR": str(tmp_path / "log"), "KITCHEN_HOST": "127.0.0.1", "KITCHEN_PORT": str(port)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                get(base + "/health")
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("kitchen sidecar did not start")
        yield d, base, token
    finally:
        proc.terminate()
        proc.wait(10)


def solve(d, base):
    policy = re.search(r"--policy (\w+)", (d / "solution" / "solve.sh").read_text()).group(1)
    subprocess.run([sys.executable, "-m", "policies.run", "--url", base + "/mcp",
                    "--scenario", "scenario.json", "--policy", policy],
                   cwd=d / "solution", check=True, timeout=60)


@pytest.mark.parametrize("task", ["oracle"], indirect=True)
def test_oracle_task_scores_full_marks_over_mcp(task, tmp_path):
    d, base, token = task
    solve(d, base)
    grade = get(base + "/_grade", token)
    assert grade["reward"] == 1.0, [c for c in grade["checks"] if not c["passed"]]
    assert all(l["seconds"] <= 3.0 for l in grade["latencies"])
    saved = json.loads((tmp_path / "log" / "trajectory.json").read_text())
    assert saved["events"] == grade["trajectory"]["events"]


@pytest.mark.parametrize("task", ["early_stop"], indirect=True)
def test_early_stop_task_scores_partially_over_mcp(task):
    d, base, token = task
    solve(d, base)
    assert get(base + "/_grade", token)["reward"] == pytest.approx(9 / 14)


@pytest.mark.parametrize("task", ["oracle"], indirect=True)
def test_untouched_task_scores_zero_and_grade_needs_the_token(task):
    d, base, token = task
    assert get(base + "/_grade", token)["reward"] == 0.0
    with pytest.raises(urllib.error.HTTPError) as e:
        get(base + "/_grade", "wrong")
    assert e.value.code == 403


@pytest.mark.parametrize("task", ["oracle"], indirect=True)
def test_assistant_side_of_the_task_holds_no_annotations(task):
    d, _, _ = task
    visible = (d / "instruction.md").read_text() + (d / "task.toml").read_text() \
        + (d / "environment" / "Dockerfile").read_text()
    for secret in ("soy sauce bowl", "reference_answer", "same_as", "scenario.json"):
        assert secret not in visible
