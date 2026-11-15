"""Recipe search from canned pages (no network)."""
import json
import threading
import time
import urllib.error

import cooking.webrecipes as module
from cooking.webrecipes import WebRecipes, duckduckgo, search_results, structured_recipe


def result(url):
    return (f'<a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg='
            f'{url.replace(":", "%3A").replace("/", "%2F")}&amp;rut=x">A <b>title</b></a>')


def ld(node):
    return f'<html><script type="application/ld+json">{json.dumps(node)}</script><body><p>Story.</p></body></html>'


RECIPE = {"@type": "Recipe", "name": "Beef stir-fry", "image": ["https://a.example/wok.jpg", "https://a.example/2.jpg"],
          "recipeIngredient": ["300 g beef", "2 tbsp soy sauce"],
          "recipeInstructions": [{"@type": "HowToStep", "text": "Sear the beef."},
                                 {"@type": "HowToSection", "itemListElement": [{"@type": "HowToStep", "text": "Add soy sauce."}]}]}
PAGES = {
    "https://a.example/stir-fry": ld({"@graph": [{"@type": "WebSite"}, RECIPE]}),
    "https://b.example/blog": "<html><body><p>Fry the beef, then add sauce.</p></body></html>",
    "https://d.example/second": ld([{**RECIPE, "@type": ["Recipe", "NewsArticle"], "name": "Second <b>stir-fry</b>",
                                     "image": {"@type": "ImageObject", "url": "https://d.example/pan.jpg"},
                                     "recipeInstructions": "Fry everything."}]),
    "https://e.example/no-picture": ld({**RECIPE, "image": None}),
}
RESULTS = "".join(map(result, ["https://b.example/blog", "https://e.example/no-picture",
                               "https://a.example/stir-fry", "https://c.example/gone"]))


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
    assert search_results(RESULTS)[:2] == ["https://b.example/blog", "https://e.example/no-picture"]


def test_structured_recipe_reads_graphs_type_lists_sections_and_image_forms():
    first = structured_recipe(PAGES["https://a.example/stir-fry"])
    assert first["steps"] == ["Sear the beef.", "Add soy sauce."] and first["image"] == "https://a.example/wok.jpg"
    second = structured_recipe(PAGES["https://d.example/second"])
    assert second == {"title": "Second stir-fry", "image": "https://d.example/pan.jpg",
                      "ingredients": ["300 g beef", "2 tbsp soy sauce"], "steps": ["Fry everything."]}
    assert structured_recipe(PAGES["https://b.example/blog"]) is None


def test_only_pages_with_a_picture_and_an_ingredient_list_are_candidates():
    web = Web()
    found = WebRecipes(duckduckgo(web), web).search("beef stir-fry", [])
    assert "beef%20stir-fry%20recipe" in web.fetched[0]
    assert found == [{"recipe_id": "w1", "url": "https://a.example/stir-fry", "title": "Beef stir-fry",
                      "image": "https://a.example/wok.jpg", "ingredients": ["300 g beef", "2 tbsp soy sauce"],
                      "steps": "Sear the beef. Add soy sauce.", "instructions": ["Sear the beef.", "Add soy sauce."]}]


def test_loaded_text_is_the_recipe_itself():
    web = Web()
    recipes = WebRecipes(duckduckgo(web), web)
    (found,) = recipes.search("beef stir-fry", [])
    assert recipes.fetch(found["recipe_id"]) == \
        "Beef stir-fry\n\nIngredients:\n300 g beef\n2 tbsp soy sauce\n\nSteps:\n1. Sear the beef.\n2. Add soy sauce."


def test_second_search_fetches_only_pages_not_tried_before():
    web = Web()
    recipes = WebRecipes(duckduckgo(web), web)
    recipes.search("beef stir-fry", [])
    web.results += result("https://d.example/second")
    web.fetched.clear()
    assert [c["url"] for c in recipes.search("beef stir-fry", [])] == ["https://d.example/second"]
    assert web.fetched[1:] == ["https://d.example/second"]      # rejected and unreadable pages are not retried


def test_chinese_dish_is_searched_in_chinese():
    web = Web("")
    assert WebRecipes(duckduckgo(web), web).search("麻婆豆腐", []) == []
    assert "%E5%81%9A%E6%B3%95" in web.fetched[0]               # 做法


def test_a_site_that_never_answers_does_not_hold_up_the_search(monkeypatch):
    monkeypatch.setattr(module, "PAGE_TIMEOUT_S", 0.2)
    release = threading.Event()

    def get(url):
        if url == "https://b.example/blog":
            release.wait(5)
        return Web()(url)

    started = time.monotonic()
    found = WebRecipes(duckduckgo(get), get).search("beef stir-fry", [])
    release.set()
    assert time.monotonic() - started < 2
    assert [c["url"] for c in found] == ["https://a.example/stir-fry"]


OG_PAGE = '<html><head><meta content="https://f.example/dish.jpg" property="og:image"></head><body>做法见下</body></html>'


def test_a_page_without_structured_data_is_read_by_the_reader_and_uses_the_share_picture():
    pages = {"https://f.example/mapo": OG_PAGE, "https://a.example/stir-fry": PAGES["https://a.example/stir-fry"],
             "https://g.example/no-picture": "<html><body>plain</body></html>"}
    reading = {"title": "麻婆豆腐", "ingredients": ["豆腐 400克", "牛肉末 100克"], "steps": ["豆腐切块焯水。", "炒香豆瓣酱。"]}
    read_pages = []
    read = lambda page: read_pages.append(page) or reading
    find = lambda dish, skip: [{"url": url} for url in pages]
    recipes = WebRecipes(find, pages.__getitem__, read)
    mapo, stir_fry = recipes.search("麻婆豆腐", [])
    assert read_pages == [OG_PAGE]      # not the page with its own recipe data, not the one without a picture
    assert mapo == {"recipe_id": "w1", "url": "https://f.example/mapo", "title": "麻婆豆腐",
                    "image": "https://f.example/dish.jpg", "ingredients": ["豆腐 400克", "牛肉末 100克"],
                    "steps": "豆腐切块焯水。 炒香豆瓣酱。", "instructions": ["豆腐切块焯水。", "炒香豆瓣酱。"]}
    assert recipes.fetch("w1").endswith("Steps:\n1. 豆腐切块焯水。\n2. 炒香豆瓣酱。")
    assert stir_fry["title"] == "Beef stir-fry" and stir_fry["ingredients"] == ["300 g beef", "2 tbsp soy sauce"]


def test_finder_is_told_which_pages_were_already_tried():
    asked = []
    web = Web()
    recipes = WebRecipes(lambda dish, skip: asked.append(skip) or [{"url": "https://a.example/stir-fry/"}], web)
    recipes.search("beef stir-fry", [])
    assert recipes.search("beef stir-fry", []) == []
    assert asked == [[], ["https://a.example/stir-fry"]]
