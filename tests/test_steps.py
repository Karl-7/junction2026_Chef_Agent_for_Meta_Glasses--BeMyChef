"""Step commands and which ingredients a step uses: decided by code, so tested exactly."""
import pytest

from cooking.steps import BACK, NEXT, command, mentions, view


@pytest.mark.parametrize("text, expected", [
    ("Next", NEXT), ("next step.", NEXT), ("Gemini, next", NEXT), ("Go on!", NEXT), ("下一步", NEXT),
    ("Back", BACK), ("go back", BACK), ("Previous step", BACK), ("上一步。", BACK),
])
def test_bare_step_commands(text, expected):
    assert command(text) == expected


@pytest.mark.parametrize("text", [
    "It's next to the salt", "I'll be back in a minute", "What's next?", "Gemini, what do I do next?",
    "Can you pass me the salt?", "",
])
def test_anything_longer_is_not_a_command(text):
    assert command(text) is None


@pytest.mark.parametrize("step, ingredient", [
    ("Sear the sliced beef on high heat.", "300 g beef, thinly sliced"),
    ("Add a spoonful of soy sauce.", "2 tbsp light soy sauce"),
    ("Finish with spring onion.", "2 spring onions, chopped"),
    ("Season with salt.", "1 tsp salt"),
    ("Add the eggs one at a time.", "3 large eggs"),
    ("豆腐切块焯水。", "豆腐 400克"),
])
def test_a_step_names_its_ingredients(step, ingredient):
    assert mentions(step, ingredient)


@pytest.mark.parametrize("step, ingredient", [
    ("Toss with oyster sauce.", "2 tbsp soy sauce"),        # "sauce" alone does not identify it
    ("Add garlic.", "300 g beef, thinly sliced"),
    ("Add the beef and cook until no pink remains.", "1 tbsp cooking wine"),
    ("炒香豆瓣酱。", "豆腐 400克"),
])
def test_a_step_does_not_claim_other_ingredients(step, ingredient):
    assert not mentions(step, ingredient)


def test_view_numbers_from_one_and_lists_only_what_the_step_uses():
    instructions = ["Sear the beef.", "Add garlic and soy sauce."]
    ingredients = ["300 g beef", "2 cloves garlic", "2 tbsp soy sauce"]
    assert view(instructions, ingredients, 1) == {
        "number": 2, "total": 2, "text": "Add garlic and soy sauce.",
        "ingredients": ["2 cloves garlic", "2 tbsp soy sauce"]}
