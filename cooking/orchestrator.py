"""Session orchestrator: the state machine between the glasses and the model.

    (asleep ->) idle -> searching -> awaiting_confirmation -> cooking

On the glasses a session starts asleep: nothing is shown or said until the user calls the assistant
("Gemini, I want to cook"). It then asks what to cook, or which ingredients to use up. A dish is
searched on the web; ingredients are matched against the recipes the user saved from videos.

The model (or a scripted policy) is the "brain": for each user utterance it is given `Session.call`
and acts only through the tools below. Everything code can decide is decided here, not by the
brain: state transitions, the wake word, candidate de-duplication, never re-showing a rejected
recipe, waking up and the opening question, which saved recipes use the ingredients the user named,
loading the recipe before the display closes, a fresh frame before each cooking answer,
the spoken acknowledgement before a slow search, highlight timeouts, and which recipe step is on
screen ("next" and "back" move it without the brain or the wake word).
"""
from __future__ import annotations

import time
from typing import Callable, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import steps
from .device import Device, RecipeSource

WAKE_WORD = "gemini"
HIGHLIGHT_TTL_S = 5.0
SEARCH_ACK = {"en": "Let me search.", "zh": "我搜一下。"}
LAST_STEP = {"en": "That's the last step.", "zh": "这是最后一步了。"}
OPENING = {"en": "What do you want to cook? Or are there any ingredients you want to use?",
           "zh": "你想做什么菜？或者有什么想用掉的食材吗？"}
# Whole utterances, after the wake word, that only start the assistant. Anything longer already says
# what to cook and goes to the brain.
START = {"", "i want to cook", "i want to cook something", "let s cook", "lets cook", "start cooking",
         "我想做饭", "我要做饭", "我想做菜", "我要做菜", "开始做饭"}

ASLEEP, IDLE, SEARCHING, AWAITING, COOKING = "asleep", "idle", "searching", "awaiting_confirmation", "cooking"


class Brain(Protocol):
    def respond(self, utterance: dict, call: Callable[..., dict]) -> None: ...


def canonical_url(url: str) -> str:
    """Same page -> same string: host case, fragment, trailing slash and utm_* parameters dropped."""
    u = urlsplit(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
                       if not k.lower().startswith("utm_")])
    return urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path.rstrip("/"), query, ""))


def language_of(text: str) -> str:
    return "zh" if any("一" <= ch <= "鿿" for ch in text) else "en"


def _error(message: str) -> dict:
    return {"error": message}


class Session:
    def __init__(self, device: Device, source: RecipeSource, clock: Callable[[], float] = time.monotonic,
                 highlight_ttl: float = HIGHLIGHT_TTL_S, saved: RecipeSource | None = None, asleep: bool = False):
        """`saved`: the user's saved recipes, searched by ingredient. `asleep`: wait to be called first."""
        self.device, self.source, self.saved, self.clock = device, source, saved, clock
        self.highlight_ttl = highlight_ttl
        self.state = ASLEEP if asleep else IDLE
        self.language = "en"
        self.recipe: dict | None = None          # the confirmed recipe, with its page text
        self.step: int | None = None             # index into the recipe's instructions, while cooking
        self._candidates: dict[str, dict] = {}   # recipe_id -> candidate, one per page
        self._origin: dict[str, RecipeSource] = {}   # recipe_id -> where it was found, and is read from
        self._seen_ids: set[str] = set()         # every id a search returned, duplicates included
        self._pages: set[str] = set()            # canonical urls of self._candidates
        self._rejected: set[str] = set()
        self._shown: str | None = None
        self._frame: dict | None = None          # frame taken since the current utterance
        self._help_request = False               # the current utterance arrived while cooking
        self._spoke = False                      # anything said since the current utterance
        self._highlight_expires: float | None = None
        self._tools = {
            "capture_frame": self.capture_frame, "say": self.say,
            "search_recipes": self.search_recipes, "find_saved_recipes": self.find_saved_recipes,
            "show_recipe": self.show_recipe,
            "hide_recipe": self.hide_recipe, "load_recipe": self.load_recipe,
            "highlight": self.highlight, "clear_highlight": self.clear_highlight,
        }

    # ---- driving the session -------------------------------------------------------------

    def run(self, brain: Brain) -> None:
        while (utterance := self.device.next_utterance()) is not None:
            self.handle(utterance, brain)

    def handle(self, utterance: dict, brain: Brain) -> None:
        self.tick()
        if self.state == ASLEEP:
            if WAKE_WORD not in utterance["text"].lower():
                return                           # not called yet: nothing shown, nothing said
            self.language = language_of(utterance["text"])
            self.state = IDLE
            self.device.show_prompt()
            if steps.phrase(utterance["text"]) in START:
                self.device.say(OPENING[self.language])
                return
        if self.state == COOKING and (move := steps.command(utterance["text"])):
            self.language = language_of(utterance["text"])
            self.move_step(move)
            return
        if self.state == COOKING and WAKE_WORD not in utterance["text"].lower():
            return                               # F1: not addressed to the assistant
        self.language = language_of(utterance["text"])
        self._frame, self._spoke = None, False
        self._help_request = self.state == COOKING
        if self.current_step() is not None:
            utterance = {**utterance, "step": self.current_step()}   # what the user is looking at
        brain.respond(utterance, self.call)
        if self.recipe is not None and self._shown is not None:
            self.hide_recipe()                   # the brain loaded the recipe but left it on screen
        self.tick()

    def call(self, name: str, **args) -> dict:
        self.tick()
        tool = self._tools.get(name)
        if tool is None:
            return _error(f"unknown tool {name!r}")
        try:
            return tool(**args)
        except TypeError as e:                   # wrong or missing arguments from the model
            return _error(str(e))

    # ---- recipe steps ----------------------------------------------------------------------

    def current_step(self) -> dict | None:
        if self.step is None:
            return None
        return steps.view(self.recipe["instructions"], self.recipe.get("ingredients") or [], self.step)

    def move_step(self, direction: str) -> None:
        if self.step is None:
            return
        last = len(self.recipe["instructions"]) - 1
        target = min(last, self.step + 1) if direction == steps.NEXT else max(0, self.step - 1)
        if target != self.step:
            self.step = target
            self.device.show_step(self.current_step())
        elif direction == steps.NEXT:
            self.device.say(LAST_STEP[self.language])   # "back" on the first step needs no answer

    def tick(self) -> None:
        if self._highlight_expires is not None and self.clock() >= self._highlight_expires:
            self.clear_highlight()

    # ---- tools ---------------------------------------------------------------------------

    def capture_frame(self) -> dict:
        self._frame = self.device.capture_frame()
        return self._frame

    def say(self, text: str) -> dict:
        if self._help_request and self._frame is None:
            return _error("call capture_frame first: every cooking answer starts from a fresh frame")
        self.device.say(text)
        self._spoke = True
        return {"ok": True}

    def search_recipes(self, dish: str, exclude: list[str] | None = None) -> dict:
        if self.state == COOKING:
            return _error("a recipe is already confirmed")
        if not self._spoke:
            self.say(SEARCH_ACK[self.language])  # the search is slow; the user hears something first
        self.state = SEARCHING
        return {"candidates": self._add(self.source, self.source.search(dish, sorted(self._seen_ids | set(exclude or []))))}

    def find_saved_recipes(self, ingredients: list[str]) -> dict:
        if self.state == COOKING:
            return _error("a recipe is already confirmed")
        if self.saved is None:
            return {"candidates": []}
        self.state = SEARCHING
        return {"candidates": self._add(self.saved, self.saved.search(ingredients, sorted(self._seen_ids)))}

    def _add(self, source: RecipeSource, found: list[dict]) -> list[dict]:
        """The candidates among `found` that are new pages; these can now be shown."""
        new = []
        for cand in found:
            self._seen_ids.add(cand["recipe_id"])
            page = canonical_url(cand["url"])
            if page in self._pages:
                continue
            self._pages.add(page)
            self._candidates[cand["recipe_id"]] = cand
            self._origin[cand["recipe_id"]] = source
            new.append(cand)
        return new

    def show_recipe(self, recipe_id: str) -> dict:
        if self.state not in (SEARCHING, AWAITING):
            return _error(f"no recipe choice in progress (state: {self.state})")
        if recipe_id not in self._candidates:
            return _error(f"unknown recipe_id {recipe_id!r}")
        if recipe_id in self._rejected:
            return _error(f"{recipe_id} was already rejected; show a different recipe or search again")
        if recipe_id != self._shown:
            if self._shown is not None:
                self._rejected.add(self._shown)  # moving on from a recipe is a rejection
            self.device.show_recipe(self._candidates[recipe_id])
            self._shown = recipe_id
        self.state = AWAITING
        return {"ok": True}

    def load_recipe(self, recipe_id: str) -> dict:
        if self.state != AWAITING or recipe_id != self._shown:
            return _error("only the recipe currently on screen can be loaded")
        text = self._origin[recipe_id].fetch(recipe_id)
        self.recipe = {**self._candidates[recipe_id], "text": text}
        return {"recipe_id": recipe_id, "text": text}

    def hide_recipe(self) -> dict:
        if self._shown is None:
            return {"ok": True}
        if self.recipe is None:
            return _error("call load_recipe first: the display closes only after the recipe is read")
        self.device.hide_recipe()
        self._shown = None
        self.state = COOKING
        if self.recipe.get("instructions"):
            self.step = 0
            self.device.show_step(self.current_step())
        return {"ok": True}

    def highlight(self, box: list[float], label: str = "", ttl: float | None = None) -> dict:
        if self.state != COOKING:
            return _error("highlights are available only while cooking")
        if self._frame is None:
            return _error("call capture_frame first: the box is drawn on a fresh frame")
        if len(box) != 4 or not all(0 <= v <= 1000 for v in box) or box[0] >= box[2] or box[1] >= box[3]:
            return _error("box must be [ymin, xmin, ymax, xmax] within 0..1000")
        ttl = self.highlight_ttl if ttl is None else ttl
        self.device.show_highlight(self._frame["frame_id"], box, label, ttl)
        self._highlight_expires = self.clock() + ttl
        return {"ok": True}

    def clear_highlight(self) -> dict:
        if self._highlight_expires is not None:
            self.device.clear_highlight()
            self._highlight_expires = None
        return {"ok": True}
