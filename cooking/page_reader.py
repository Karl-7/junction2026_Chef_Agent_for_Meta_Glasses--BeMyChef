"""Gemini as the reader of recipe pages that carry no structured data: one request per page,
answering in JSON. Token usage of every response is handed to the `Gemini` log.
"""
from __future__ import annotations

import html
import json
import re
import sys

from .gemini import THINKING_LEVEL, Gemini, _parts, _text

MAX_PAGE_CHARS = 20000      # of the page's visible text

PROMPT = """\
Below is the text of a web page. If it gives one complete recipe, reply with a JSON object:
  "title": the recipe's name as on the page
  "ingredients": array of strings, each ingredient with its quantity, as written on the page
  "steps": array of strings, the method step by step, as written on the page
Keep everything in the page's own language; do not translate, and add nothing the page does not say.
If the page gives no complete recipe, leave the arrays empty.

"""
_STRINGS = {"type": "array", "items": {"type": "string"}}
# All three required: with optional fields the model filled in the title and left the lists out.
SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}, "ingredients": _STRINGS, "steps": _STRINGS},
          "required": ["title", "ingredients", "steps"], "propertyOrdering": ["title", "ingredients", "steps"]}


def visible_text(page: str) -> str:
    """The text a reader of the page sees: no scripts, styles or tags."""
    page = re.sub(r"<(script|style|noscript|svg)\b.*?</\1>", " ", page, flags=re.S | re.I)
    page = re.sub(r"<!--.*?-->", " ", page, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page))).strip()


class GeminiReader:
    """Reader for `cooking.webrecipes.WebRecipes`: page html -> {title, ingredients, steps}, or None."""

    def __init__(self, gemini: Gemini):
        self._gemini = gemini

    def __call__(self, page: str) -> dict | None:
        text = visible_text(page)[:MAX_PAGE_CHARS]
        if not text:
            return None
        try:
            resp = self._gemini.generate("read_page", {
                "contents": [{"role": "user", "parts": [{"text": PROMPT + text}]}],
                "generationConfig": {"responseMimeType": "application/json", "responseSchema": SCHEMA,
                                     "thinkingConfig": {"thinkingLevel": THINKING_LEVEL}}})
        except RuntimeError as e:
            print(f"page reader: {e}", file=sys.stderr)
            return None
        return reading(_text(_parts(resp)))


def reading(answer: str) -> dict | None:
    """The usable part of the model's JSON answer."""
    try:
        item = json.loads(answer)
    except ValueError:
        return None
    if not isinstance(item, dict):
        return None
    strings = lambda value: [str(v).strip() for v in value if str(v).strip()] if isinstance(value, list) else []
    return {"title": str(item.get("title") or "").strip(),
            "ingredients": strings(item.get("ingredients")), "steps": strings(item.get("steps"))}
