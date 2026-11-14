"""Following a recipe one step at a time: the spoken step commands, and what each step uses.

Both are decided by code. "Next" and "back" move between steps without the model (and without the
wake word), and the ingredients shown with a step are the recipe lines that the step mentions.
"""
from __future__ import annotations

import re

NEXT, BACK = "next", "back"

# Whole utterances only, after punctuation and an optional wake word are removed, so that
# "it's next to the salt" or "back in a minute" never move the recipe.
COMMANDS = {
    NEXT: {"next", "next step", "next one", "go on", "continue", "下一步", "下一个", "继续"},
    BACK: {"back", "go back", "previous", "previous step", "上一步", "上一个", "返回"},
}

# Words that say how much or how to prepare, not what the ingredient is.
_NOT_NAMES = {
    "g", "kg", "mg", "ml", "l", "oz", "lb", "lbs", "tbsp", "tsp", "tablespoon", "tablespoons", "teaspoon",
    "teaspoons", "cup", "cups", "clove", "cloves", "pinch", "piece", "pieces", "slice", "slices", "handful",
    "bunch", "can", "cans", "stalk", "stalks", "sprig", "sprigs", "dash", "a", "an", "the", "of", "and", "or",
    "to", "for", "about", "taste", "optional", "plus", "more", "fresh", "large", "small", "medium", "sliced",
    "chopped", "minced", "diced", "grated", "crushed", "finely", "thinly", "roughly", "peeled", "cut", "into",
    "light", "dark", "extra", "virgin", "neutral", "vegetable", "ground", "whole", "cooked", "raw", "boneless", "skinless",
}
# Names too general to identify an ingredient alone: "sauce" must not match soy sauce to oyster sauce.
_GENERIC = {"sauce", "oil", "powder", "paste", "stock", "wine", "vinegar", "seed", "leave", "flake", "juice",
            "water", "salt", "pepper"}
_CJK_AMOUNTS = re.compile(r"[\d\s.,/~-]+|克|千克|毫升|升|勺|汤匙|茶匙|小勺|大勺|个|片|根|瓣|颗|块|适量|少许|若干")
_CJK = re.compile(r"[一-鿿]")


def command(text: str) -> str | None:
    """NEXT, BACK, or None when the utterance is anything other than a bare step command."""
    words = re.sub(r"[^\w\s一-鿿]", " ", text.lower()).split()
    if words and words[0] == "gemini":
        words = words[1:]
    phrase = " ".join(words)
    for name, phrases in COMMANDS.items():
        if phrase in phrases:
            return name
    return None


def _words(text: str) -> list[str]:
    return [w.rstrip("s") if len(w) > 3 else w for w in re.findall(r"[a-z]+", text.lower())]


def mentions(step: str, ingredient: str) -> bool:
    """Whether the step text names the ingredient line ("300 g beef, sliced" is named by "the beef")."""
    if _CJK.search(ingredient):
        name = _CJK_AMOUNTS.sub("", ingredient.split("，")[0].split(",")[0])
        return bool(name) and name in step
    name = re.sub(r"\(.*?\)", "", ingredient.split(",")[0])
    keys = [w for w in _words(name) if w not in _NOT_NAMES and len(w) > 1]
    specific = [w for w in keys if w not in _GENERIC] or keys
    said = set(_words(step))
    return any(w in said for w in specific)


def view(instructions: list[str], ingredients: list[str], index: int) -> dict:
    """What the display shows for step `index` (0-based)."""
    text = instructions[index]
    return {"number": index + 1, "total": len(instructions), "text": text,
            "ingredients": [i for i in ingredients if mentions(text, i)]}
