"""Scripted brains that act from a scenario's annotations (no model).

  Scripted(scenario)                          oracle: the reference policy
  Scripted(scenario, stop_after_accept=True)  early-stop baseline: picks the recipe, then goes silent
"""
from __future__ import annotations

ASK = "Is this recipe OK?"


class Scripted:
    def __init__(self, scenario: dict, stop_after_accept: bool = False):
        self._notes = {u["id"]: u for u in scenario["utterances"]}
        self._stop_after_accept = stop_after_accept
        self._dish = ""
        self._queue: list[str] = []      # candidates not shown yet
        self._shown = ""

    def respond(self, utterance: dict, call) -> None:
        note = self._notes[utterance["utterance_id"]]
        kind = note["kind"]
        if kind == "start":
            call("capture_frame")
            self._dish = note["dish"]
            self._show_next(call)
        elif kind == "reject":
            self._show_next(call)
        elif kind == "accept":
            call("load_recipe", recipe_id=self._shown)
            call("hide_recipe")
            call("say", text="OK, let's cook.")
        elif kind == "help" and not self._stop_after_accept:
            call("capture_frame")
            if "target" in note:
                box = next(o["box"] for o in note["objects"] if o["name"] == note["target"])
                call("highlight", box=box, label=note["target"])
            call("say", text=note["reference_answer"])

    def _show_next(self, call) -> None:
        if not self._queue:
            if self._shown:
                call("say", text="That was the last one I had. Let me search again.")
            self._queue = [c["recipe_id"] for c in call("search_recipes", dish=self._dish)["candidates"]]
        if not self._queue:
            call("say", text="I couldn't find another recipe.")
            return
        self._shown = self._queue.pop(0)
        call("show_recipe", recipe_id=self._shown)
        call("say", text=ASK)
