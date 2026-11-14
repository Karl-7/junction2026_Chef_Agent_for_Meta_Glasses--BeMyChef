"""Recipe candidates from the web: a finder names recipe pages, code reads each page.

Finders: `ClaudeSearch` (cooking.claude_search), which also reports what it read on each page,
and `duckduckgo` here, which needs no key and returns urls only.

Most recipe sites embed a schema.org Recipe (JSON-LD) with the name, picture, ingredient list and
steps; where a page has one, its ingredients and steps are used exactly as published. Where it
does not, the finder's reading of the page is used, with the page's share picture. A page becomes
a candidate only with a picture, an ingredient list and steps: the glasses show the first two.
"""
from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Callable

from .orchestrator import canonical_url, language_of

SEARCH = "https://html.duckduckgo.com/html/?q="
QUERY = {"en": "{dish} recipe", "zh": "{dish} 做法"}
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
PAGES_PER_SEARCH = 8
PAGE_TIMEOUT_S = 8
MAX_RECIPE_CHARS = 20000
SUMMARY_CHARS = 240


def http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=PAGE_TIMEOUT_S) as r:
        return r.read().decode(r.headers.get_content_charset() or "utf-8", errors="replace")


def duckduckgo(get: Callable[[str], str] = http_get) -> Callable[[str, list[str]], list[dict]]:
    """Finder without a key: the result urls of one DuckDuckGo search."""
    def find(dish: str, skip: list[str]) -> list[dict]:
        query = QUERY[language_of(dish)].format(dish=dish)
        return [{"url": url} for url in search_results(get(SEARCH + urllib.parse.quote(query)))]
    return find


def search_results(page: str) -> list[str]:
    """Result page urls from a DuckDuckGo HTML results page, in rank order."""
    out = []
    for href in re.findall(r'class="result__a"[^>]*href="([^"]+)"', page):
        target = re.search(r"[?&]uddg=([^&]+)", html.unescape(href))
        out.append(urllib.parse.unquote(target.group(1)) if target else html.unescape(href))
    return out


def _steps(instructions) -> list[str]:
    if isinstance(instructions, str):
        return [instructions.strip()] if instructions.strip() else []
    if isinstance(instructions, dict):
        return _steps(instructions.get("itemListElement") or instructions.get("text") or "")
    return [s for item in instructions or [] for s in _steps(item)]


def _image(image) -> str:
    if isinstance(image, list):
        return next((url for url in map(_image, image) if url), "")
    if isinstance(image, dict):
        return _image(image.get("url") or "")
    return image.strip() if isinstance(image, str) else ""


def share_picture(page: str) -> str:
    """The page's og:image, the picture sites choose for link previews."""
    for tag in re.findall(r"<meta\b[^>]*>", page):
        if re.search(r"""property=["']og:image["']""", tag):
            content = re.search(r"""content=["']([^"']+)["']""", tag)
            return html.unescape(content.group(1)) if content else ""
    return ""


def structured_recipe(page: str) -> dict | None:
    """{title, image, ingredients, steps} from the page's schema.org Recipe, if it has one."""
    for block in re.findall(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", page, re.S):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        nodes = data if isinstance(data, list) else [data]
        nodes = [n for node in nodes if isinstance(node, dict) for n in (node.get("@graph") or [node])]
        for node in nodes:
            kind = node.get("@type") if isinstance(node, dict) else None
            if kind == "Recipe" or (isinstance(kind, list) and "Recipe" in kind):
                clean = lambda s: html.unescape(re.sub(r"<[^>]+>", "", str(s))).strip()
                return {"title": clean(node.get("name") or ""), "image": _image(node.get("image")),
                        "ingredients": [clean(i) for i in node.get("recipeIngredient") or []],
                        "steps": [clean(s) for s in _steps(node.get("recipeInstructions"))]}
    return None


class WebRecipes:
    """Implements `cooking.device.RecipeSource`."""

    def __init__(self, find: Callable[[str, list[str]], list[dict]], get: Callable[[str], str] = http_get):
        self._find, self._get = find, get
        self._found: dict[str, dict] = {}     # recipe_id -> candidate + "text" (what load_recipe returns)
        self._tried: set[str] = set()         # canonical urls already fetched, usable or not

    def search(self, dish: str, exclude: list[str]) -> list[dict]:
        leads = self._find(dish, sorted(self._tried))
        fresh = [lead for lead in leads if canonical_url(lead["url"]) not in self._tried][:PAGES_PER_SEARCH]
        self._tried |= {canonical_url(lead["url"]) for lead in fresh}
        pool = ThreadPoolExecutor(max_workers=PAGES_PER_SEARCH)
        jobs = [pool.submit(self._page, lead["url"]) for lead in fresh]
        wait(jobs, timeout=PAGE_TIMEOUT_S)       # one slow site must not hold up the others
        pool.shutdown(wait=False, cancel_futures=True)
        found = []
        for lead, job in zip(fresh, jobs):
            page = (job.result() if job.done() else None) or ""
            recipe = structured_recipe(page) or {}
            title = recipe.get("title") or lead.get("title") or ""
            image = recipe.get("image") or share_picture(page)
            # The page's own data wins; the finder's reading fills in for pages without it.
            ingredients, steps = recipe.get("ingredients"), recipe.get("steps")
            if not (ingredients and steps):
                ingredients, steps = lead.get("ingredients") or [], lead.get("steps") or []
            if not (title and image and ingredients and steps):
                continue
            summary = " ".join(steps)
            cand = {"recipe_id": f"w{len(self._found) + 1}", "url": lead["url"], "title": title, "image": image,
                    "ingredients": ingredients,
                    "steps": summary[:SUMMARY_CHARS] + ("…" if len(summary) > SUMMARY_CHARS else ""),
                    "instructions": steps}
            text = "\n".join([title, "", "Ingredients:", *ingredients, "", "Steps:",
                              *(f"{n}. {s}" for n, s in enumerate(steps, 1))])
            self._found[cand["recipe_id"]] = {**cand, "text": text[:MAX_RECIPE_CHARS]}
            found.append(cand)
        return found

    def fetch(self, recipe_id: str) -> str:
        return self._found[recipe_id]["text"]

    def _page(self, url: str) -> str | None:
        try:
            return self._get(url)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            return None
