"""Recipes saved from videos, and finding one by the ingredients the user wants to use up."""
import pytest

from cooking.saved import SavedRecipes

CHICKEN = dict(title="Crispy chicken rice", url="https://www.tiktok.com/@a/video/1",
               ingredients=["2 chicken thighs", "1 cup rice", "1 tbsp olive oil"], instructions=["Fry.", "Serve."])
PEAS = dict(title="Pea soup", url="https://www.tiktok.com/@a/video/2",
            ingredients=["2 cups frozen peas", "1 onion"], instructions=["Boil.", "Blend."])
TOFU = dict(title="麻婆豆腐", url="https://www.tiktok.com/@a/video/3",
            ingredients=["豆腐 400克", "牛肉末 100克"], instructions=["豆腐切块。", "下锅。"])


@pytest.fixture
def saved(tmp_path):
    store = SavedRecipes(tmp_path)
    for recipe in (CHICKEN, PEAS, TOFU):
        store.save(**recipe)
    return store


def titles(candidates):
    return [c["title"] for c in candidates]


@pytest.mark.parametrize("ingredients, found", [
    (["frozen chicken"], ["Crispy chicken rice"]),          # "frozen" describes it, "chicken" names it
    (["peas"], ["Pea soup"]),
    (["豆腐"], ["麻婆豆腐"]),
    (["salmon"], []),
    (["onion", "peas", "rice"], ["Pea soup", "Crispy chicken rice"]),     # the one using more of them first
])
def test_recipes_are_found_by_the_ingredients_they_use(saved, ingredients, found):
    assert titles(saved.search(ingredients, [])) == found


def test_excluded_recipes_are_not_returned(saved):
    (first,) = saved.search(["chicken"], [])
    assert saved.search(["chicken"], [first["recipe_id"]]) == []


def test_the_same_video_is_saved_once(saved):
    saved.save(**{**CHICKEN, "title": "Crispy chicken rice, again"})
    assert sorted(r["title"] for r in saved.all()) == ["Crispy chicken rice, again", "Pea soup", "麻婆豆腐"]


def test_a_saved_recipe_reads_like_a_recipe_page(saved):
    (cand,) = saved.search(["chicken"], [])
    assert saved.fetch(cand["recipe_id"]).splitlines() == [
        "Crispy chicken rice", "", "Ingredients:", "2 chicken thighs", "1 cup rice", "1 tbsp olive oil", "",
        "Steps:", "1. Fry.", "2. Serve."]


def test_the_dish_picture_is_kept_and_only_saved_pictures_are_served(saved, tmp_path):
    picture = tmp_path / "frame.jpg"
    picture.write_bytes(b"jpeg")
    recipe = saved.save(**CHICKEN, photo=picture)
    (cand,) = saved.search(["chicken"], [])
    assert cand["image"] == f"saved/{recipe['photo']}"
    assert saved.photo(recipe["photo"]).read_bytes() == b"jpeg"
    assert saved.photo("frame.jpg") is None and saved.photo("../.env") is None


def test_an_empty_store_has_nothing(tmp_path):
    assert SavedRecipes(tmp_path / "none").search(["chicken"], []) == []
