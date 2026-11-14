"""Recipe search from canned pages (no network)."""
import json
import urllib.error

from cooking.webrecipes import WebRecipes, search_results, structured_recipe


def result(url, title, snippet):
    return (f'<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg={url.replace(":", "%3A").replace("/", "%2F")}&amp;rut=x">'
            f'{title}</a><a class="result__snippet" href="x">{snippet}</a>')


def ld(node):
    return f'<html><script type="application/ld+json">{json.dumps(node)}</script><body><nav>menu</nav><p>Story.</p></body></html>'


RECIPE = {"@type": "Recipe", "name": "Beef stir-fry", "recipeIngredient": ["300 g beef", "2 tbsp soy sauce"],
          "recipeInstructions": [{"@type": "HowToStep", "text": "Sear the beef."},
                                 {"@type": "HowToSection", "itemListElement": [{"@type": "HowToStep", "text": "Add soy sauce."}]}]}
PAGES = {
    "https://a.example/stir-fry": ld({"@graph": [{"@type": "WebSite"}, RECIPE]}),
    "https://b.example/blog": "<html><body><nav>menu</nav><p>Fry the beef, then add sauce.</p><script>x()</script></body></html>",
    "https://d.example/second": ld([{**RECIPE, "@type": ["Recipe", "NewsArticle"], "name": "Second <b>stir-fry</b>",
                                     "recipeInstructions": "Fry everything."}]),
}
RESULTS = "".join([result("https://b.example/blog", "A blog <b>post</b>", "Fry the beef &amp; sauce."),
                   result("https://a.example/stir-fry", "Stir-fry | A", "snippet a"),
                   result("https://c.example/gone", "Gone", "snippet c")])


class Web:
    def __init__(self, results=RESULTS):
        self.results, self.fetched = results, []

    def __call__(self, url):
        self.fetched.append(url)
        if "duckduckgo" in url:
            return self.results
        if url not in PAGES:
            raise urllib.error.URLError("not found")
        return PAGES[url]


def test_search_results_are_read_in_rank_order():
    assert search_results(RESULTS)[0] == {"url": "https://b.example/blog", "title": "A blog post",
                                          "snippet": "Fry the beef & sauce."}


def test_structured_recipe_reads_graphs_type_lists_sections_and_plain_text():
    assert structured_recipe(PAGES["https://a.example/stir-fry"])["steps"] == ["Sear the beef.", "Add soy sauce."]
    second = structured_recipe(PAGES["https://d.example/second"])
    assert second["title"] == "Second stir-fry" and second["steps"] == ["Fry everything."]
    assert structured_recipe(PAGES["https://b.example/blog"]) is None


def test_structured_pages_come_first_and_unreadable_pages_are_dropped():
    web = Web()
    found = WebRecipes(web).search("beef stir-fry", [])
    assert "beef%20stir-fry%20recipe" in web.fetched[0]
    assert [c["url"] for c in found] == ["https://a.example/stir-fry", "https://b.example/blog"]
    assert found[0] == {"recipe_id": "w2", "url": "https://a.example/stir-fry", "title": "Beef stir-fry",
                        "ingredients": ["300 g beef", "2 tbsp soy sauce"], "steps": "Sear the beef. Add soy sauce."}
    assert found[1]["ingredients"] == [] and found[1]["steps"] == "Fry the beef & sauce."


def test_loaded_text_is_the_recipe_not_the_page_chrome():
    recipes = WebRecipes(Web())
    structured, plain = recipes.search("beef stir-fry", [])
    assert recipes.fetch(structured["recipe_id"]) == \
        "Beef stir-fry\n\nIngredients:\n300 g beef\n2 tbsp soy sauce\n\nSteps:\n1. Sear the beef.\n2. Add soy sauce."
    assert recipes.fetch(plain["recipe_id"]) == "Fry the beef, then add sauce."


def test_second_search_fetches_only_pages_not_tried_before():
    web = Web()
    recipes = WebRecipes(web)
    recipes.search("beef stir-fry", [])
    web.results += result("https://d.example/second", "Second", "snippet d")
    web.fetched.clear()
    assert [c["url"] for c in recipes.search("beef stir-fry", [])] == ["https://d.example/second"]
    assert web.fetched[1:] == ["https://d.example/second"]      # the gone page is not retried either


def test_chinese_dish_is_searched_in_chinese():
    web = Web("")
    assert WebRecipes(web).search("麻婆豆腐", []) == []
    assert "%E5%81%9A%E6%B3%95" in web.fetched[0]               # 做法


def test_a_site_that_never_answers_does_not_hold_up_the_search(monkeypatch):
    import threading
    import time

    import cooking.webrecipes as module

    monkeypatch.setattr(module, "PAGE_TIMEOUT_S", 0.2)
    release = threading.Event()

    def get(url):
        if url == "https://b.example/blog":
            release.wait(5)
        return Web()(url)

    started = time.monotonic()
    found = WebRecipes(get).search("beef stir-fry", [])
    release.set()
    assert time.monotonic() - started < 2
    assert [c["url"] for c in found] == ["https://a.example/stir-fry"]
