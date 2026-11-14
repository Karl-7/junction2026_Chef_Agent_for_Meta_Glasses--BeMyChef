"""Recipe search without a model: a web search, then each result page's own structured data.

Most recipe sites embed a schema.org Recipe (JSON-LD) with the name, ingredient list and steps, so
code can read a candidate exactly instead of asking a model to summarise it. Pages without one
are still offered, with the search snippet as their summary and the page text as the recipe.

(Gemini's Google Search grounding would do the search itself, but it is not available on the
free tier of the API.)
"""
from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from html.parser import HTMLParser
from typing import Callable

from .orchestrator import canonical_url, language_of

SEARCH = "https://html.duckduckgo.com/html/?q="
QUERY = {"en": "{dish} recipe", "zh": "{dish} 做法"}
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
PAGES_PER_SEARCH = 6
PAGE_TIMEOUT_S = 8
MAX_RECIPE_CHARS = 20000
SUMMARY_CHARS = 240


def http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=PAGE_TIMEOUT_S) as r:
        return r.read().decode(r.headers.get_content_charset() or "utf-8", errors="replace")


def search_results(page: str) -> list[dict]:
    """[{url, title, snippet}] from a DuckDuckGo HTML results page, in rank order."""
    strip = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', page, re.S)
    out = []
    for i, (href, title) in enumerate(re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S)):
        target = re.search(r"[?&]uddg=([^&]+)", html.unescape(href))
        out.append({"url": urllib.parse.unquote(target.group(1)) if target else html.unescape(href),
                    "title": strip(title), "snippet": strip(snippets[i]) if i < len(snippets) else ""})
    return out


def _steps(instructions) -> list[str]:
    if isinstance(instructions, str):
        return [instructions.strip()] if instructions.strip() else []
    if isinstance(instructions, dict):
        return _steps(instructions.get("itemListElement") or instructions.get("text") or "")
    return [s for item in instructions or [] for s in _steps(item)]


def structured_recipe(page: str) -> dict | None:
    """{title, ingredients, steps} from the page's schema.org Recipe, if it has one."""
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
                return {"title": clean(node.get("name") or ""),
                        "ingredients": [clean(i) for i in node.get("recipeIngredient") or []],
                        "steps": [clean(s) for s in _steps(node.get("recipeInstructions"))]}
    return None


class _PageText(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "header", "form"}

    def __init__(self):
        super().__init__()
        self.chunks: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag, attrs):
        self._skipping += tag in self.SKIP

    def handle_endtag(self, tag):
        self._skipping -= tag in self.SKIP and self._skipping > 0

    def handle_data(self, data):
        if not self._skipping and data.strip():
            self.chunks.append(data.strip())


def page_text(page: str) -> str:
    parser = _PageText()
    parser.feed(page)
    return "\n".join(parser.chunks)


class WebRecipes:
    """Implements `cooking.device.RecipeSource`."""

    def __init__(self, get: Callable[[str], str] = http_get):
        self._get = get
        self._found: dict[str, dict] = {}     # recipe_id -> candidate + "text"
        self._tried: set[str] = set()         # canonical urls already fetched, readable or not

    def search(self, dish: str, exclude: list[str]) -> list[dict]:
        query = QUERY[language_of(dish)].format(dish=dish)
        results = search_results(self._get(SEARCH + urllib.parse.quote(query)))
        fresh = [r for r in results if canonical_url(r["url"]) not in self._tried][:PAGES_PER_SEARCH]
        self._tried |= {canonical_url(r["url"]) for r in fresh}
        pool = ThreadPoolExecutor(max_workers=PAGES_PER_SEARCH)
        jobs = [pool.submit(self._page, r["url"]) for r in fresh]
        wait(jobs, timeout=PAGE_TIMEOUT_S)       # one slow site must not hold up the others
        pool.shutdown(wait=False, cancel_futures=True)
        pages = [job.result() if job.done() else None for job in jobs]
        found = []
        for result, page in zip(fresh, pages):
            if page is None:
                continue
            recipe = structured_recipe(page)
            if recipe and recipe["steps"]:
                steps = " ".join(recipe["steps"])
                text = "\n".join([recipe["title"], "", "Ingredients:", *recipe["ingredients"], "", "Steps:",
                                  *(f"{n}. {s}" for n, s in enumerate(recipe["steps"], 1))])
                cand = {"title": recipe["title"] or result["title"], "ingredients": recipe["ingredients"],
                        "steps": steps[:SUMMARY_CHARS] + ("…" if len(steps) > SUMMARY_CHARS else "")}
            else:
                text = page_text(page)
                cand = {"title": result["title"], "ingredients": [], "steps": result["snippet"]}
            cand = {"recipe_id": f"w{len(self._found) + 1}", "url": result["url"], **cand}
            self._found[cand["recipe_id"]] = {**cand, "text": text[:MAX_RECIPE_CHARS]}
            found.append(cand)
        # Pages that publish a structured recipe first: their ingredients and steps are exact.
        return sorted(found, key=lambda c: not c["ingredients"])

    def fetch(self, recipe_id: str) -> str:
        return self._found[recipe_id]["text"]

    def _page(self, url: str) -> str | None:
        try:
            return self._get(url)
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            return None
