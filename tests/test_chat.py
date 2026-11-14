"""The WhatsApp messages: three short ones per recipe, plain formatting, times as estimates."""
from cooking.chat import MAX_MESSAGE_CHARS, recipe_messages, split_message, summary

INGREDIENTS = ["2 salmon fillets", "3 tbsp butter", " ", "4 cloves garlic, minced"]
STEPS = ["Pat the salmon dry and season both sides.", "Melt the butter over medium heat."]


def test_a_recipe_is_three_messages_dish_ingredients_method():
    assert recipe_messages("Garlic butter salmon", "15 mins", "20 minutes", "2 servings", INGREDIENTS, STEPS) == [
        "*Garlic butter salmon*\nAbout 15 min prep and 20 min cooking. Serves 2.",
        "*Ingredients*\n- 2 salmon fillets\n- 3 tbsp butter\n- 4 cloves garlic, minced",
        "*Method*\n1. Pat the salmon dry and season both sides.\n2. Melt the butter over medium heat.",
    ]


def test_missing_facts_are_left_out_instead_of_guessed():
    assert summary("Toast", "", "N/A", "unknown") == "*Toast*"
    assert summary("Stew", "", "1 hour", "4-6 people") == "*Stew*\nAbout 1 h cooking. Serves 4-6."
    assert recipe_messages("Toast", "", "", "", [], []) == ["*Toast*"]


def test_messages_carry_no_emoji():
    text = "\n".join(recipe_messages("Salmon", "5 mins", "10 mins", "2", INGREDIENTS, STEPS))
    assert all(ord(ch) < 0x2000 for ch in text)


def test_long_messages_are_split_at_line_breaks():
    steps = [f"Step number {n} with enough words to take up some room in the message." for n in range(60)]
    parts = recipe_messages("Long", "", "", "", [], steps)
    assert len(parts) > 1 and all(len(p) <= MAX_MESSAGE_CHARS for p in parts)
    assert "".join(parts).count("Step number") == 60                 # nothing lost at the seams


def test_a_single_overlong_line_is_cut():
    assert split_message("x" * 25, max_len=10) == ["x" * 10, "x" * 10, "x" * 5]
