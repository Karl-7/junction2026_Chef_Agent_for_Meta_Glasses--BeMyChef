"""The replayed kitchen: plays one scenario's user utterances and frames, answers recipe searches
from the scenario's recorded pages, and records every call with its time.

It enforces nothing about the assistant's behaviour; `kitchen.scoring` judges the record.
Annotations (object boxes, facts, which utterance is a rejection) never leave this process.
"""
from __future__ import annotations

import time
from typing import Callable

CANDIDATE_FIELDS = ("recipe_id", "title", "url", "ingredients", "steps", "instructions")


class Kitchen:
    def __init__(self, scenario: dict, clock: Callable[[], float] = time.monotonic):
        self.scenario = scenario
        self.clock = clock
        self.events: list[dict] = []
        self._next = 0
        self._recipes = {r["recipe_id"]: r for r in scenario["recipes"]}

    def call(self, tool: str, **args) -> dict:
        result = getattr(self, "_" + tool)(**args)
        # Recorded after the tool ran: an utterance's time is when the assistant received it.
        self.events.append({"time": self.clock(), "tool": tool, "args": args, "result": result})
        return result

    def trajectory(self) -> dict:
        return {"scenario": self.scenario["id"], "events": self.events}

    def _next_utterance(self) -> dict:
        utterances = self.scenario["utterances"]
        if self._next >= len(utterances):
            return {"done": True}
        u = utterances[self._next]
        self._next += 1
        return {"utterance_id": u["id"], "text": u["text"], "t": u["t"]}

    def _capture_frame(self) -> dict:
        if self._next == 0:
            return {"frame_id": "f-start", "t": 0.0}
        u = self.scenario["utterances"][self._next - 1]
        return {"frame_id": "f-" + u["id"], "t": u["t"]}

    def _say(self, text: str) -> dict:
        return {"ok": True}

    def _search_recipes(self, dish: str, exclude: list[str] | None = None) -> dict:
        left = [r for r in self.scenario["recipes"] if r["recipe_id"] not in set(exclude or [])]
        page = left[: self.scenario["search_page_size"]]
        return {"candidates": [{k: r[k] for k in CANDIDATE_FIELDS} for r in page]}

    def _show_recipe(self, recipe_id: str) -> dict:
        return {"ok": True} if recipe_id in self._recipes else {"error": f"unknown recipe_id {recipe_id!r}"}

    def _hide_recipe(self) -> dict:
        return {"ok": True}

    def _load_recipe(self, recipe_id: str) -> dict:
        r = self._recipes.get(recipe_id)
        return {"recipe_id": recipe_id, "text": r["page_text"]} if r else {"error": f"unknown recipe_id {recipe_id!r}"}

    def _show_step(self, number: int) -> dict:
        return {"ok": True}

    def _highlight(self, box: list[float], label: str = "", ttl: float = 0.0, frame_id: str = "") -> dict:
        return {"ok": True}

    def _clear_highlight(self) -> dict:
        return {"ok": True}
