"""Gemini as the brain (text turns, tool calls).

The key comes from GEMINI_API_KEY (environment, or the untracked .env file); the model id from
GEMINI_MODEL. Every response's token usage is handed to `log` so runs can be priced afterwards.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-3.5-flash-lite"
THINKING_LEVEL = "low"            # the answer has to start within 3 seconds
MAX_REQUESTS_PER_TURN = 6
SORRY = {"en": "Sorry, I lost my connection for a moment. Please ask again.",
         "zh": "抱歉，我刚才连接出了问题，请再说一遍。"}

SYSTEM = """\
You are a cooking assistant on a user's smart glasses. The user hears you through the glasses and
has a small screen at the side of their view. Every user message comes with the current camera
frame from the glasses.

You act only through tools. `say` is the only way the user hears you: plain text you write is not
spoken reliably. Reply in the language of the user's latest message. Keep it short and conversational.

Choosing a recipe
- The user starts by naming a dish, or ingredients they want to use up.
- Ingredients instead of a dish ("I have some frozen chicken that I need to use"): call
  `find_saved_recipes` with them. These are recipes the user saved from cooking videos they liked.
  If they want to use up ingredients but have not said which, first ask "Do you have any ingredients you
  want to use?". If a saved recipe comes back, `show_recipe` it and say "I remember you liked this
  video, want to try it out today?". If none does, say so in a few words and `search_recipes` for a
  dish made with those ingredients.
- "What can I make with this?": name the ingredients you can see in the frame in one short sentence,
  then treat them as the ingredients the user wants to use up.
- When the user asks you to look at what they are about to cook, work out the dish from the frame.
  If you cannot tell, ask instead of guessing.
- Call `search_recipes` (do not announce it: the user is told automatically), then `show_recipe`
  for one candidate and ask whether it is OK.
- If the user says no, show a different candidate. When none are left, search again and say so.
- If the user says yes, call `load_recipe` for that recipe, then `hide_recipe`, then confirm briefly.
  From then on follow that recipe.

While cooking
- Answer from the recipe and from what the frame actually shows, not from the recipe alone.
  Give one action at a time.
- For "which one" questions, call `highlight` with a tight box around the object in the frame,
  [ymin, xmin, ymax, xmax] on a 0-1000 scale, and say only a few words such as "This one."
- Be conservative about doneness: when unsure whether food is cooked, tell the user to cook it
  longer. If the view is unclear, say so plainly.
- The screen shows one recipe step at a time; each message says which step is on screen. The user
  moves between steps by saying "next" or "back", which the app handles without you. Answer
  questions about the step on screen unless the user asks about another one.

A tool result with an "error" tells you what to do instead. Call `say` in the same step as the
action it comments on, so the user is not kept waiting."""


def _fn(name, description, properties=None, required=None):
    f = {"name": name, "description": description}
    if properties:
        f["parameters"] = {"type": "object", "properties": properties,
                           "required": list(properties) if required is None else required}
    return f


_STR = {"type": "string"}
TOOLS = [{"functionDeclarations": [
    _fn("say", "Speak to the user.", {"text": _STR}),
    _fn("search_recipes", "Search the web for recipes of a dish. Returns new candidates only.",
        {"dish": {"type": "string", "description": "Dish name, in the language to search in."}}),
    _fn("find_saved_recipes", "Find recipes the user saved from videos that use the given ingredients. "
        "Returns new candidates only, best match first.",
        {"ingredients": {"type": "array", "items": _STR,
                         "description": "One ingredient name per item, e.g. [\"chicken\"]. Saved recipes may be in "
                                        "another language than the user's: give each name in English as well."}}),
    _fn("show_recipe", "Show one candidate on the glasses screen.", {"recipe_id": _STR}),
    _fn("load_recipe", "Read the full recipe page of the candidate on screen. Returns its text.", {"recipe_id": _STR}),
    _fn("hide_recipe", "Close the recipe display. Only after load_recipe."),
    _fn("highlight", "Show the current frame with a box drawn around one object.",
        {"box": {"type": "array", "items": {"type": "number"}, "description": "[ymin, xmin, ymax, xmax], 0-1000"},
         "label": {"type": "string", "description": "Two or three words naming the object."}}),
    _fn("clear_highlight", "Remove the highlight."),
]}]


def api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY")
    env = Path(__file__).resolve().parents[1] / ".env"
    if not key and env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("GEMINI_API_KEY="):
                key = line.split("=", 1)[1].strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    return key


def model_id() -> str:
    return os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)


class Gemini:
    """One model, one key, one log."""

    def __init__(self, model: str | None = None, key: str | None = None, log: Callable[[dict], None] = lambda rec: None):
        self.model, self._key, self._log = model or model_id(), key or api_key(), log

    def generate(self, purpose: str, body: dict) -> dict:
        req = urllib.request.Request(API.format(model=self.model), data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "x-goog-api-key": self._key})
        started = time.monotonic()
        for attempt in (1, 2):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    resp = json.loads(r.read())
                break
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:500]
                self._log({"event": "http_error", "purpose": purpose, "code": e.code, "detail": detail})
                if e.code not in (500, 503) or attempt == 2:
                    raise RuntimeError(f"Gemini {e.code}: {detail}") from e
            except (urllib.error.URLError, TimeoutError) as e:
                self._log({"event": "net_error", "purpose": purpose, "detail": str(e)})
                if attempt == 2:
                    raise RuntimeError(f"Gemini unreachable: {e}") from e
        self._log({"event": "response", "purpose": purpose, "model": self.model,
                   "seconds": round(time.monotonic() - started, 3), "usage": resp.get("usageMetadata", {})})
        return resp


def _parts(resp: dict) -> list[dict]:
    return ((resp.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []


def _text(parts: list[dict]) -> str:
    return "".join(p["text"] for p in parts if "text" in p and not p.get("thought")).strip()


class GeminiBrain:
    """Implements `cooking.orchestrator.Brain`."""

    def __init__(self, gemini: Gemini):
        self._gemini = gemini
        self._contents: list[dict] = []

    def respond(self, utterance: dict, call) -> None:
        frame = call("capture_frame")            # F6: every turn starts from a fresh frame
        for content in self._contents:           # only the newest frame stays in the context
            content["parts"] = [p for p in content["parts"] if "inlineData" not in p]
        parts = [{"text": utterance["text"]}]
        if frame.get("jpeg"):
            parts.insert(0, {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(frame["jpeg"]).decode()}})
        else:
            parts.append({"text": "(The camera gave no image this time.)"})
        if step := utterance.get("step"):
            parts.append({"text": f"(On screen: step {step['number']} of {step['total']}: {step['text']})"})
        self._contents.append({"role": "user", "parts": parts})
        try:
            self._turn(call)
        except RuntimeError as e:
            print(f"gemini: {e}", file=sys.stderr)
            call("say", text=SORRY["zh" if re.search("[一-鿿]", utterance["text"]) else "en"])

    def _turn(self, call) -> None:
        said = False
        for _ in range(MAX_REQUESTS_PER_TURN):
            resp = self._gemini.generate("turn", {
                "systemInstruction": {"parts": [{"text": SYSTEM}]}, "contents": self._contents, "tools": TOOLS,
                "generationConfig": {"thinkingConfig": {"thinkingLevel": THINKING_LEVEL}}})
            parts = _parts(resp)
            self._contents.append({"role": "model", "parts": parts or [{"text": ""}]})   # verbatim: keeps thought signatures
            calls = [p["functionCall"] for p in parts if "functionCall" in p]
            if not calls:
                if _text(parts) and not said:
                    call("say", text=_text(parts))   # the model answered in text instead of calling say
                return
            results, failed = [], False
            for c in calls:
                result = call(c["name"], **(c.get("args") or {}))
                failed |= "error" in result
                said |= c["name"] == "say" and "error" not in result
                response = {"name": c["name"], "response": {"result": result}}
                if "id" in c:
                    response["id"] = c["id"]
                results.append({"functionResponse": response})
            self._contents.append({"role": "user", "parts": results})
            if said and not failed and not any(c["name"] in ("search_recipes", "find_saved_recipes") for c in calls):
                return                           # spoken, nothing to repair, no results to act on: turn over
