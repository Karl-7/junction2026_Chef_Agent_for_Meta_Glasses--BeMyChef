"""What the WhatsApp bot says, as plain text. Pure functions: the bot only sends what these return.

A recipe goes out as three short messages instead of one long one, so the ingredients and the method
can each be found again while cooking: the dish (sent with its photo), the ingredients, the method.
WhatsApp formatting only: *bold* headings, "- " lists, "1. " steps. Times come from the model's
reading of a video, so they are given as estimates.
"""
from __future__ import annotations

import re

MAX_MESSAGE_CHARS = 1550     # WhatsApp through Twilio rejects bodies over 1600 characters

WELCOME = "Send me a link to a TikTok cooking video and I'll turn it into a recipe."
WORKING = "Got it. Reading the video now, this takes about half a minute."
NOT_A_RECIPE = "That video doesn't look like a recipe. Send a link to a cooking video."
FAILED = "I couldn't open that video. Make sure it's public and send the link again."


def _duration(text: str) -> str:
    """'15 mins' -> '15 min'; empty when the model gave nothing usable."""
    text = re.sub(r"\b(minutes?|mins?)\b", "min", text.strip(), flags=re.I)
    text = re.sub(r"\b(hours?|hrs?)\b", "h", text, flags=re.I)
    return text if re.search(r"\d", text) else ""


def _servings(text: str) -> str:
    """'4 servings' -> '4', '2-3 people' -> '2-3'; empty when there is no number."""
    match = re.search(r"\d+(?:\s*[-–]\s*\d+)?", text)
    return re.sub(r"\s*[-–]\s*", "-", match.group(0)) if match else ""


def summary(dish: str, prep: str, cook: str, servings: str) -> str:
    """First message: the dish name, then how long it takes and how many it serves."""
    times = [f"{t} {what}" for t, what in ((_duration(prep), "prep"), (_duration(cook), "cooking")) if t]
    facts = []
    if times:
        facts.append(f"About {' and '.join(times)}.")
    if serves := _servings(servings):
        facts.append(f"Serves {serves}.")
    return "\n".join([f"*{dish.strip()}*", *([" ".join(facts)] if facts else [])])


def ingredients_message(ingredients: list[str]) -> str:
    return "\n".join(["*Ingredients*", *(f"- {i.strip()}" for i in ingredients if i.strip())])


def method_message(steps: list[str]) -> str:
    lines = [s.strip() for s in steps if s.strip()]
    return "\n".join(["*Method*", *(f"{n}. {s}" for n, s in enumerate(lines, 1))])


def recipe_messages(dish: str, prep: str, cook: str, servings: str,
                    ingredients: list[str], steps: list[str]) -> list[str]:
    """The messages for one recipe, in order. The bot attaches the dish photo to the first."""
    messages = [summary(dish, prep, cook, servings)]
    if any(i.strip() for i in ingredients):
        messages.append(ingredients_message(ingredients))
    if any(s.strip() for s in steps):
        messages.append(method_message(steps))
    return [part for message in messages for part in split_message(message)]


def split_message(text: str, max_len: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split at line breaks so no part is over `max_len`; a single longer line is cut."""
    parts: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if current and len(current) + len(line) > max_len:
            parts.append(current.rstrip())
            current = ""
        while len(line) > max_len:
            parts.append(line[:max_len])
            line = line[max_len:]
        current += line
    if current.strip():
        parts.append(current.rstrip())
    return parts
