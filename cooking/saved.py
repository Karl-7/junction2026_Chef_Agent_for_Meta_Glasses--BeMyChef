"""Recipes the user saved: the Telegram bot stores each recipe it reads from a video, and the glasses
offer one of them when the user names ingredients to use up instead of a dish.

One folder, one recipe per video: `<id>.json`, and `<id>.jpg` when a picture of the dish was taken.
The id comes from the video's address, so sending the same video again replaces its recipe.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path

from . import steps
from .orchestrator import canonical_url

FOLDER = Path(__file__).resolve().parents[1] / "saved_recipes"
MAX_CANDIDATES = 3
SUMMARY_CHARS = 240


class SavedRecipes:
    """The store, and a `cooking.device.RecipeSource` searched by ingredient."""

    def __init__(self, folder: Path = FOLDER):
        self.folder = Path(folder)

    def save(self, title: str, url: str, ingredients: list[str], instructions: list[str], prep_time: str = "",
             cook_time: str = "", servings: str = "", photo: str | Path | None = None) -> dict:
        self.folder.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha1(canonical_url(url).encode()).hexdigest()[:12]
        has_photo = bool(photo) and Path(photo).exists()
        if has_photo:
            shutil.copyfile(photo, self.folder / f"{key}.jpg")
        recipe = {"id": key, "title": title.strip(), "url": url, "ingredients": [i.strip() for i in ingredients if i.strip()],
                  "instructions": [s.strip() for s in instructions if s.strip()], "prep_time": prep_time,
                  "cook_time": cook_time, "servings": servings, "photo": f"{key}.jpg" if has_photo else None,
                  "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        (self.folder / f"{key}.json").write_text(json.dumps(recipe, ensure_ascii=False, indent=2))
        return recipe

    def all(self) -> list[dict]:
        """Every saved recipe, newest first."""
        recipes = [json.loads(p.read_text()) for p in self.folder.glob("*.json")]
        return sorted(recipes, key=lambda r: r["saved_at"], reverse=True)

    def photo(self, name: str) -> Path | None:
        """The file of a saved dish picture, by the name stored with its recipe."""
        known = {r["photo"] for r in self.all()}
        return self.folder / name if name in known else None

    def search(self, ingredients: list[str], exclude: list[str]) -> list[dict]:
        """Saved recipes that use any of `ingredients`: those using the most first, then the newest."""
        scored = [(uses(recipe, ingredients), recipe) for recipe in self.all()]    # stable: newest first within a score
        ranked = sorted((pair for pair in scored if pair[0]), key=lambda pair: -pair[0])
        candidates = [_candidate(recipe) for _, recipe in ranked]
        return [c for c in candidates if c["recipe_id"] not in exclude][:MAX_CANDIDATES]

    def fetch(self, recipe_id: str) -> str:
        recipe = json.loads((self.folder / f"{recipe_id.removeprefix('saved-')}.json").read_text())
        facts = [f"{label}: {recipe[key]}" for label, key in
                 (("Prep", "prep_time"), ("Cooking", "cook_time"), ("Serves", "servings")) if recipe.get(key)]
        return "\n".join([recipe["title"], *facts, "", "Ingredients:", *recipe["ingredients"], "", "Steps:",
                          *(f"{n}. {s}" for n, s in enumerate(recipe["instructions"], 1))])


def uses(recipe: dict, ingredients: list[str]) -> int:
    """How many of the named ingredients appear in the recipe's ingredient list."""
    return sum(any(steps.mentions(named, line) for line in recipe["ingredients"]) for named in ingredients)


def _candidate(recipe: dict) -> dict:
    summary = " ".join(recipe["instructions"])
    return {"recipe_id": f"saved-{recipe['id']}", "url": recipe["url"], "title": recipe["title"],
            "image": f"saved/{recipe['photo']}" if recipe.get("photo") else "",     # served by the glasses server
            "ingredients": recipe["ingredients"],
            "steps": summary[:SUMMARY_CHARS] + ("…" if len(summary) > SUMMARY_CHARS else ""),
            "instructions": recipe["instructions"]}
