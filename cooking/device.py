"""The two seams of the assistant: the glasses hardware and the recipe web.

The orchestrator talks only to these. Implementations: `cooking.replay.ReplayKitchen` (Harbor
evaluation). The real-glasses adapter is not written yet.
"""
from __future__ import annotations

from typing import Protocol

Box = list[float]  # [ymin, xmin, ymax, xmax], each 0..1000 relative to the frame (Gemini's convention)


class Device(Protocol):
    def next_utterance(self) -> dict | None:
        """Block until the user has finished speaking: {"utterance_id", "text"}. None ends the session."""

    def capture_frame(self) -> dict:
        """Current camera frame: {"frame_id", ...}."""

    def say(self, text: str) -> None: ...

    def show_prompt(self) -> None:
        """The assistant was called: the display, empty until now, invites the user to say what to cook."""

    def show_recipe(self, recipe: dict) -> None: ...

    def hide_recipe(self) -> None: ...

    def show_highlight(self, frame_id: str, box: Box, label: str, ttl: float) -> None:
        """Show the snapshot `frame_id` with `box` drawn on it; replaces any earlier highlight."""

    def clear_highlight(self) -> None: ...

    def show_step(self, step: dict) -> None:
        """Show the recipe step the user is on: {"number", "total", "text", "ingredients"}."""


class RecipeSource(Protocol):
    def search(self, dish: str, exclude: list[str]) -> list[dict]:
        """`dish` is a dish name on the web; for the user's saved recipes it is a list of ingredients.
        Candidates {"recipe_id", "title", "url", "ingredients", "steps", "instructions"}, none in `exclude`.

        `steps` is a short summary for the model; `instructions` is the method as published, one
        string per step, which the display walks through while cooking."""

    def fetch(self, recipe_id: str) -> str:
        """Full page text of one candidate, in its original language."""
