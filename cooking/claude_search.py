"""Claude as the recipe finder: one request with the web search tool, answering in JSON.

The key comes from CLAUDE_API_KEY (environment, or the untracked .env file); the model id from
CLAUDE_MODEL. Token usage of every response is handed to `log`.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Callable

import anthropic

DEFAULT_MODEL = "claude-opus-5-5"
SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 2}   # $10 per 1,000 searches
MAX_PAGES = 8
MAX_CONTINUATIONS = 3
FALLBACK_BETA = "server-side-fallback-2026-07-01"

PROMPT = """\
Find up to {n} web pages that each give one complete recipe for: {dish}

Prefer pages in the language the dish name is written in, on different websites, that show a
photo of the finished dish. Reply with only a JSON array and no other text. Each item has:
  "url": the exact page address from the search results
  "title": the recipe's name as on the page
  "ingredients": array of strings, each ingredient with its quantity, as written on the page
  "steps": array of strings, the method step by step, as written on the page
Keep everything in the page's own language; do not translate."""


def client() -> anthropic.Anthropic:
    key = os.environ.get("CLAUDE_API_KEY")
    env = Path(__file__).resolve().parents[1] / ".env"
    if not key and env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("CLAUDE_API_KEY="):
                key = line.split("=", 1)[1].strip()
    return anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()


def unavailable(api: anthropic.Anthropic, model: str) -> str | None:
    """Why Claude cannot be used right now (no credit, bad key, no network), or None if it can."""
    try:
        api.messages.create(model=model, max_tokens=16, output_config={"effort": "low"},
                            messages=[{"role": "user", "content": "Reply: ok"}])
    except anthropic.APIStatusError as e:
        return f"{e.status_code}: {e.message}"
    except anthropic.APIConnectionError as e:
        return f"unreachable: {e}"
    return None


class ClaudeSearch:
    """Finder for `cooking.webrecipes.WebRecipes`: (dish, urls to skip) -> [{url, title, ingredients, steps}]."""

    def __init__(self, api: anthropic.Anthropic | None = None, model: str | None = None,
                 log: Callable[[dict], None] = lambda record: None):
        self._api = api or client()
        self.model = model or os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL)
        self._log = log

    def __call__(self, dish: str, skip: list[str]) -> list[dict]:
        prompt = PROMPT.format(n=MAX_PAGES, dish=dish)
        if skip:
            prompt += "\n\nDo not return these pages again:\n" + "\n".join(skip)
        messages = [{"role": "user", "content": prompt}]
        try:
            for _ in range(1 + MAX_CONTINUATIONS):
                started = time.monotonic()
                response = self._api.beta.messages.create(
                    model=self.model, max_tokens=16000, output_config={"effort": "low"}, tools=[SEARCH_TOOL],
                    betas=[FALLBACK_BETA], fallbacks="default", messages=messages)
                self._log({"event": "response", "purpose": "search", "model": response.model,
                           "seconds": round(time.monotonic() - started, 3), "usage": response.usage.to_dict()})
                if response.stop_reason != "pause_turn":
                    break
                # The server-side search loop paused: send its turn back and it resumes.
                messages = [messages[0], {"role": "assistant", "content": response.content}]
        except anthropic.APIStatusError as e:
            print(f"claude search: {e.status_code}: {e.message}", file=sys.stderr)
            return []
        except anthropic.APIConnectionError as e:
            print(f"claude search: unreachable: {e}", file=sys.stderr)
            return []
        if response.stop_reason == "refusal":
            return []
        return leads("".join(block.text for block in response.content if block.type == "text"))


def leads(answer: str) -> list[dict]:
    """The usable items of the model's JSON answer."""
    match = re.search(r"\[.*\]", answer, re.S)
    try:
        items = json.loads(match.group(0)) if match else []
    except ValueError:
        return []
    strings = lambda value: [str(v).strip() for v in value if str(v).strip()] if isinstance(value, list) else []
    return [{"url": item["url"].strip(), "title": str(item.get("title") or "").strip(),
             "ingredients": strings(item.get("ingredients")), "steps": strings(item.get("steps"))}
            for item in items if isinstance(item, dict) and isinstance(item.get("url"), str) and item["url"].strip()]
