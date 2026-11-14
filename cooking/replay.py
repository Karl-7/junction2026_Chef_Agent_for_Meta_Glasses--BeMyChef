"""Replay adapter: the kitchen sidecar stands in for both the glasses and the recipe web.

`call(tool, **args) -> dict` is the transport: `McpClient(url).call` against the sidecar's MCP
server, or `Kitchen.call` in-process.
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
from typing import Callable


class ReplayKitchen:
    """Implements `Device` and `RecipeSource`."""

    def __init__(self, call: Callable[..., dict]):
        self._call = call

    def next_utterance(self):
        u = self._call("next_utterance")
        return None if u.get("done") else u

    def capture_frame(self):
        return self._call("capture_frame")

    def say(self, text):
        self._call("say", text=text)

    def show_recipe(self, recipe):
        self._call("show_recipe", recipe_id=recipe["recipe_id"])

    def hide_recipe(self):
        self._call("hide_recipe")

    def show_highlight(self, frame_id, box, label, ttl):
        self._call("highlight", frame_id=frame_id, box=box, label=label, ttl=ttl)

    def clear_highlight(self):
        self._call("clear_highlight")

    def search(self, dish, exclude):
        return self._call("search_recipes", dish=dish, exclude=exclude)["candidates"]

    def fetch(self, recipe_id):
        return self._call("load_recipe", recipe_id=recipe_id)["text"]


class McpClient:
    """Synchronous `call(tool, **args)` over an MCP streamable-HTTP session (runs in a thread)."""

    def __init__(self, url: str):
        self.url = url
        self._req: queue.Queue = queue.Queue()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=lambda: asyncio.run(self._loop()), daemon=True)
        self._thread.start()
        if not self._ready.wait(30):
            raise ConnectionError(f"no MCP session with {url}")

    async def _loop(self):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(self.url) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                self._ready.set()
                while True:
                    item = await asyncio.to_thread(self._req.get)
                    if item is None:
                        return
                    tool, args, out = item
                    res = await s.call_tool(tool, args)
                    if res.structuredContent is not None:
                        data = res.structuredContent.get("result", res.structuredContent)
                    else:
                        data = json.loads(res.content[0].text)
                    out.put(data)

    def call(self, tool: str, **args) -> dict:
        out: queue.Queue = queue.Queue()
        self._req.put((tool, args, out))
        return out.get(timeout=60)

    def close(self):
        self._req.put(None)
        self._thread.join(5)
