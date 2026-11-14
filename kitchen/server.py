"""Kitchen sidecar: MCP server (streamable HTTP) in its own container.

Env:
  KITCHEN_SCENARIO   path to the scenario json (with annotations; never sent to the assistant)
  GRADER_TOKEN       shared secret for /_grade (known to the verifier only)
  KITCHEN_LOG_DIR    where the trajectory is written after every call
"""
from __future__ import annotations

import hmac
import json
import os
import threading
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from .kitchen import Kitchen
from .scoring import score

SCENARIO = json.loads(Path(os.environ["KITCHEN_SCENARIO"]).read_text())
TOKEN = os.environ.get("GRADER_TOKEN", "")
LOG_DIR = Path(os.environ.get("KITCHEN_LOG_DIR", "/var/kitchen"))

_kitchen = Kitchen(SCENARIO)
_lock = threading.Lock()

mcp = FastMCP("kitchen", host=os.environ.get("KITCHEN_HOST", "0.0.0.0"),
              port=int(os.environ.get("KITCHEN_PORT", "8000")))


def _call(tool: str, **args) -> dict:
    with _lock:
        out = _kitchen.call(tool, **args)
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            (LOG_DIR / "trajectory.json").write_text(json.dumps(_kitchen.trajectory(), ensure_ascii=False))
        except OSError:
            pass
        return out


@mcp.tool()
def next_utterance() -> dict:
    """Wait for the user's next utterance: {utterance_id, text, t}; {done: true} when the session is over."""
    return _call("next_utterance")


@mcp.tool()
def capture_frame() -> dict:
    """The current camera frame."""
    return _call("capture_frame")


@mcp.tool()
def say(text: str) -> dict:
    """Speak to the user."""
    return _call("say", text=text)


@mcp.tool()
def search_recipes(dish: str, exclude: list[str] | None = None) -> dict:
    """Search the web for recipes of `dish`; `exclude` lists recipe ids not to return again."""
    return _call("search_recipes", dish=dish, exclude=exclude or [])


@mcp.tool()
def show_recipe(recipe_id: str) -> dict:
    """Show one candidate recipe on the glasses screen."""
    return _call("show_recipe", recipe_id=recipe_id)


@mcp.tool()
def hide_recipe() -> dict:
    """Close the recipe display."""
    return _call("hide_recipe")


@mcp.tool()
def load_recipe(recipe_id: str) -> dict:
    """Fetch the full text of a recipe page."""
    return _call("load_recipe", recipe_id=recipe_id)


@mcp.tool()
def highlight(box: list[float], label: str = "", ttl: float = 5.0, frame_id: str = "") -> dict:
    """Show a frame snapshot with a box [ymin, xmin, ymax, xmax] (0..1000) drawn on it."""
    return _call("highlight", box=box, label=label, ttl=ttl, frame_id=frame_id)


@mcp.tool()
def clear_highlight() -> dict:
    """Remove the highlight from the screen."""
    return _call("clear_highlight")


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


@mcp.custom_route("/_grade", methods=["GET"])
async def grade(request: Request) -> JSONResponse:
    given = request.headers.get("x-grader-token", "")
    if not TOKEN or not hmac.compare_digest(given, TOKEN):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    with _lock:
        return JSONResponse({**score(SCENARIO, _kitchen.events), "trajectory": _kitchen.trajectory()})


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
