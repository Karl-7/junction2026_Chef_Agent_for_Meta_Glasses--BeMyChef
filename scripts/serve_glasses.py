"""Serve the glasses page and run the assistant against it, without glasses.

    python -m scripts.serve_glasses      # Gemini talks and reads the recipe pages a web search finds

Then open http://localhost:8765 in Chrome. The page is the device: display, speech, and the
computer's webcam and microphone standing in for the glasses'.
"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import FileResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from cooking.gemini import Gemini, GeminiBrain
from cooking.orchestrator import Session
from cooking.page_reader import GeminiReader
from cooking.saved import SavedRecipes
from cooking.web import WebDevice
from cooking.webrecipes import WebRecipes, duckduckgo

ROOT = Path(__file__).resolve().parents[1]


def gemini(log_dir: Path) -> Callable:
    """Gemini answers whatever the user says or types. A keyless web search finds the recipe pages;
    Gemini reads the ones that carry no structured recipe."""
    def start(device):
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = (log_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{id(device):x}.jsonl").open("a")

        def log(record):
            log_file.write(json.dumps(record, ensure_ascii=False) + "\n")
            log_file.flush()

        model = Gemini(log=log)
        recipes = WebRecipes(duckduckgo(), read=GeminiReader(model))
        # Nothing on the display until the user calls the assistant.
        return Session(device, recipes, saved=SavedRecipes(), asleep=True), GeminiBrain(model)
    return start


def create_app(start: Callable, bot=None) -> Starlette:
    """`start(device)` -> (session, brain) for each page that connects. `bot` is the Telegram bot module
    when Telegram posts its updates here, None when the bot polls for them or is not run."""
    async def page(_):
        return FileResponse(ROOT / "ui" / "glasses.html", headers={"Cache-Control": "no-cache"})   # edits show on reload

    async def icon(_):
        return FileResponse(ROOT / "ui" / "icon.png", headers={"Cache-Control": "max-age=86400"})

    async def saved_photo(request):
        path = SavedRecipes().photo(request.path_params["name"])
        return FileResponse(path, headers={"Cache-Control": "max-age=86400"}) if path else Response(status_code=404)

    async def socket(ws: WebSocket):
        await ws.accept()
        loop = asyncio.get_running_loop()
        device = WebDevice(lambda message: asyncio.run_coroutine_threadsafe(ws.send_json(message), loop))
        session, brain = start(device)
        threading.Thread(target=session.run, args=(brain,), daemon=True).start()
        try:
            while True:
                device.receive(await ws.receive_json())
        except WebSocketDisconnect:
            pass
        finally:
            device.close()

    async def telegram(request):
        sent = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(sent, bot.webhook_secret()):
            return Response(status_code=403)
        bot.handle_update(await request.json())      # answered in the background: Telegram resends what is slow
        return Response(status_code=200)

    routes = [Route("/", page), Route("/icon.png", icon), Route("/saved/{name}", saved_photo),
              WebSocketRoute("/ws", socket)]
    if bot:
        routes.append(Route("/telegram", telegram, methods=["POST"]))
    return Starlette(routes=routes,
                     middleware=[Middleware(GZipMiddleware)])     # the page is a third of its size on the glasses' link


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    Gemini()                                     # fail now, not on the first question, if the key is missing
    uvicorn.run(create_app(gemini(ROOT / "runs" / "live"), bot=telegram_bot()), host=a.host, port=a.port)


def telegram_bot():
    """The Telegram bot, told to post its updates to this server; None without a token or a public address
    (on a laptop the bot polls instead: `python Telegram_bot.py`)."""
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        return None
    import Telegram_bot as bot
    if not bot.public_url():
        return None
    try:
        bot.register_webhook(bot.public_url())
    except Exception as e:                       # the glasses still work; the address may already be registered
        print(f"[TG] webhook not registered: {e}", file=sys.stderr, flush=True)
    return bot


if __name__ == "__main__":
    main()
