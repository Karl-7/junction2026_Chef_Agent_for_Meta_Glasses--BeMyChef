"""Serve the glasses page and run the assistant against it, without glasses.

    python -m scripts.serve_glasses                    # Gemini talks, Claude searches for recipes
    python -m scripts.serve_glasses --brain scripted   # no model: the scenario's lines and recipes

Then open http://localhost:8765 in Chrome. The page is the device: display, speech, and the
computer's webcam and microphone standing in for the glasses'.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import uvicorn
from starlette.applications import Starlette
from starlette.responses import FileResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from cooking import claude_search
from cooking.gemini import Gemini, GeminiBrain
from cooking.orchestrator import Session
from cooking.replay import ReplayKitchen
from cooking.web import WebDevice
from cooking.webrecipes import WebRecipes, duckduckgo
from kitchen.kitchen import Kitchen
from policies.scripted import Scripted

ROOT = Path(__file__).resolve().parents[1]


def scripted(scenario: dict) -> Callable:
    """No model: the page offers the scenario's user lines; recipes come from the scenario file."""
    def start(device):
        lines = [{"utterance_id": u["id"], "text": u["text"]} for u in scenario["utterances"]]
        return Session(device, ReplayKitchen(Kitchen(scenario).call)), Scripted(scenario), lines
    return start


def gemini(log_dir: Path) -> Callable:
    """Gemini answers whatever the user says or types. Claude finds the recipe pages; if Claude
    cannot be used when the server starts, a keyless web search finds them instead."""
    api = claude_search.client()
    model = claude_search.ClaudeSearch(api).model
    problem = claude_search.unavailable(api, model)
    if problem:
        print(f"Claude search is off ({problem}). Recipes come from a plain web search instead.", file=sys.stderr)

    def start(device):
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = (log_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{id(device):x}.jsonl").open("a")

        def log(record):
            log_file.write(json.dumps(record, ensure_ascii=False) + "\n")
            log_file.flush()

        find = duckduckgo() if problem else claude_search.ClaudeSearch(api, model, log)
        return Session(device, WebRecipes(find)), GeminiBrain(Gemini(log=log)), None
    return start


def create_app(start: Callable) -> Starlette:
    """`start(device)` -> (session, brain, scripted user lines or None) for each page that connects."""
    async def page(_):
        return FileResponse(ROOT / "ui" / "glasses.html")

    async def icon(_):
        return FileResponse(ROOT / "ui" / "icon.png")

    async def socket(ws: WebSocket):
        await ws.accept()
        loop = asyncio.get_running_loop()
        device = WebDevice(lambda message: asyncio.run_coroutine_threadsafe(ws.send_json(message), loop))
        session, brain, script = start(device)
        await ws.send_json({"type": "hello", "script": script})
        threading.Thread(target=session.run, args=(brain,), daemon=True).start()
        try:
            while True:
                device.receive(await ws.receive_json())
        except WebSocketDisconnect:
            pass
        finally:
            device.close()

    return Starlette(routes=[Route("/", page), Route("/icon.png", icon), WebSocketRoute("/ws", socket)])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--brain", default="gemini", choices=["gemini", "scripted"])
    ap.add_argument("--scenario", default="synthetic-stirfry", help="for --brain scripted")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    if a.brain == "gemini":
        Gemini()                                 # fail now, not on the first question, if the key is missing
        start = gemini(ROOT / "runs" / "live")
    else:
        start = scripted(json.loads((ROOT / "scenarios" / f"{a.scenario}.json").read_text()))
    uvicorn.run(create_app(start), host=a.host, port=a.port)


if __name__ == "__main__":
    main()
