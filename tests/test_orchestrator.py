"""The rules the orchestrator enforces whatever the brain does."""
import pytest

from cooking.orchestrator import AWAITING, COOKING, Session, canonical_url
from cooking.replay import ReplayKitchen
from kitchen.kitchen import Kitchen


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def kitchen(scenario, clock):
    return Kitchen(scenario, clock)


@pytest.fixture
def session(kitchen, clock):
    replay = ReplayKitchen(kitchen.call)
    return Session(replay, replay, clock)


def tools(kitchen):
    return [e["tool"] for e in kitchen.events]


def to_cooking(session):
    session.call("search_recipes", dish="beef stir-fry")
    session.call("show_recipe", recipe_id="r1")
    session.call("load_recipe", recipe_id="r1")
    session.call("hide_recipe")
    assert session.state == COOKING


def test_same_page_is_one_candidate(session):
    got = session.call("search_recipes", dish="beef stir-fry")["candidates"]
    assert [c["recipe_id"] for c in got] == ["r1"]          # r1-ad is the same page with a tracking tag
    assert canonical_url("https://Recipes.example/beef-stir-fry/?utm_source=search") == \
        canonical_url("https://recipes.example/beef-stir-fry")


def test_second_search_returns_only_new_pages(session):
    session.call("search_recipes", dish="beef stir-fry")
    got = session.call("search_recipes", dish="beef stir-fry")["candidates"]
    assert [c["recipe_id"] for c in got] == ["r2", "r3"]


def test_search_is_acknowledged_before_it_runs(session, kitchen):
    session.call("search_recipes", dish="beef stir-fry")
    assert tools(kitchen) == ["say", "search_recipes"]


def test_acknowledgement_follows_the_users_language(session, kitchen):
    class Search:
        def respond(self, utterance, call):
            call("search_recipes", dish="牛肉")

    session.handle({"utterance_id": "x", "text": "我要做这道菜，你看一下"}, Search())
    assert kitchen.events[0]["args"]["text"] == "我搜一下。"


def test_rejected_recipe_is_never_shown_again(session, kitchen):
    session.call("search_recipes", dish="beef stir-fry")
    session.call("search_recipes", dish="beef stir-fry")
    session.call("show_recipe", recipe_id="r1")
    session.call("show_recipe", recipe_id="r2")
    assert "error" in session.call("show_recipe", recipe_id="r1")
    assert [e["args"]["recipe_id"] for e in kitchen.events if e["tool"] == "show_recipe"] == ["r1", "r2"]


def test_display_closes_only_after_the_recipe_is_loaded(session, kitchen):
    session.call("search_recipes", dish="beef stir-fry")
    session.call("show_recipe", recipe_id="r1")
    assert "error" in session.call("hide_recipe")
    assert session.state == AWAITING and "hide_recipe" not in tools(kitchen)
    session.call("load_recipe", recipe_id="r1")
    assert session.call("hide_recipe") == {"ok": True}
    assert session.state == COOKING and session.recipe["text"].startswith("Quick beef stir-fry")


def test_display_is_closed_for_a_brain_that_loads_and_forgets(session, kitchen):
    class Forgetful:
        def respond(self, utterance, call):
            call("search_recipes", dish="beef stir-fry")
            call("show_recipe", recipe_id="r1")
            call("load_recipe", recipe_id="r1")

    session.handle({"utterance_id": "x", "text": "cook this"}, Forgetful())
    assert tools(kitchen)[-3:] == ["load_recipe", "hide_recipe", "show_step"] and session.state == COOKING


def test_cooking_answer_needs_a_fresh_frame(session, kitchen):
    to_cooking(session)

    class Asked:
        def __init__(self, *steps):
            self.steps, self.results = steps, []

        def respond(self, utterance, call):
            self.results = [call(name, **args) for name, args in self.steps]

    say = ("say", {"text": "Add the soy sauce."})
    box = ("highlight", {"box": [600, 100, 760, 260]})
    first = Asked(say, box, ("capture_frame", {}), say, box)
    session.handle({"utterance_id": "x", "text": "Gemini, what next?"}, first)
    assert ["error" in r for r in first.results] == [True, True, False, False, False]
    second = Asked(say)
    session.handle({"utterance_id": "y", "text": "Gemini, and now?"}, second)
    assert "error" in second.results[0]         # the frame from the earlier question does not count


def test_confirming_the_recipe_can_be_spoken_after_the_display_closes(session, kitchen):
    session.call("search_recipes", dish="beef stir-fry")
    session.call("show_recipe", recipe_id="r1")

    class Confirm:
        def respond(self, utterance, call):
            call("load_recipe", recipe_id="r1")
            call("hide_recipe")
            self.result = call("say", text="OK, let's cook.")

    brain = Confirm()
    session.handle({"utterance_id": "x", "text": "Yes"}, brain)
    assert brain.result == {"ok": True}


def test_only_the_wake_word_gets_an_answer_while_cooking(session):
    to_cooking(session)

    class Counting:
        heard = 0

        def respond(self, utterance, call):
            self.heard += 1

    brain = Counting()
    session.handle({"utterance_id": "a", "text": "Can you pass me the salt?"}, brain)
    session.handle({"utterance_id": "b", "text": "Gemini, what next?"}, brain)
    assert brain.heard == 1


def test_highlight_clears_after_its_ttl_or_is_replaced(session, kitchen, clock):
    to_cooking(session)
    session.call("capture_frame")
    session.call("highlight", box=[600, 100, 760, 260], label="bowl", ttl=4)
    session.call("highlight", box=[600, 300, 760, 460], label="other bowl", ttl=4)
    clock.now = 3.9
    session.tick()
    assert "clear_highlight" not in tools(kitchen)
    clock.now = 4.0
    session.tick()
    session.tick()
    assert tools(kitchen).count("clear_highlight") == 1


def test_malformed_box_is_refused(session):
    to_cooking(session)
    session.call("capture_frame")
    assert "error" in session.call("highlight", box=[760, 100, 600, 260])
    assert "error" in session.call("highlight", box=[0, 0, 1200, 10])


# ---- recipe steps --------------------------------------------------------------------------

def steps_shown(kitchen):
    return [e["args"]["number"] for e in kitchen.events if e["tool"] == "show_step"]


class Recording:
    """A brain that only remembers what it was given."""

    def __init__(self):
        self.utterances = []

    def respond(self, utterance, call):
        self.utterances.append(utterance)


def test_first_step_is_shown_when_cooking_starts(session, kitchen):
    to_cooking(session)
    assert steps_shown(kitchen) == [1] and session.current_step()["total"] == 4


def test_next_and_back_move_the_step_without_the_brain_or_the_wake_word(session, kitchen):
    to_cooking(session)
    brain = Recording()
    for text in ["Next", "next step", "Back"]:
        session.handle({"utterance_id": text, "text": text}, brain)
    assert steps_shown(kitchen) == [1, 2, 3, 2] and brain.utterances == []


def test_steps_stop_at_both_ends(session, kitchen):
    to_cooking(session)
    session.handle({"utterance_id": "b", "text": "back"}, Recording())
    assert steps_shown(kitchen) == [1]                       # already on the first step
    for _ in range(3):
        session.handle({"utterance_id": "n", "text": "next"}, Recording())
    session.handle({"utterance_id": "n", "text": "next"}, Recording())
    assert steps_shown(kitchen) == [1, 2, 3, 4]
    assert kitchen.events[-1]["tool"] == "say" and kitchen.events[-1]["args"]["text"] == "That's the last step."


def test_next_means_nothing_before_cooking(session, kitchen):
    brain = Recording()
    session.handle({"utterance_id": "n", "text": "next"}, brain)
    assert steps_shown(kitchen) == [] and len(brain.utterances) == 1   # the brain decides what it means


def test_a_question_reaches_the_brain_with_the_step_on_screen(session):
    to_cooking(session)
    session.handle({"utterance_id": "n", "text": "next"}, Recording())
    brain = Recording()
    session.handle({"utterance_id": "q", "text": "Gemini, how much garlic?"}, brain)
    assert brain.utterances[0]["step"] == session.current_step() and session.current_step()["number"] == 2


# ---- waking up, and recipes saved from videos ------------------------------------------------

from cooking.orchestrator import ASLEEP, IDLE, OPENING
from cooking.saved import SavedRecipes


class Silent:
    """A brain that must not be reached."""
    def respond(self, utterance, call):
        raise AssertionError(f"the brain was asked about {utterance['text']!r}")


class Heard:
    def __init__(self):
        self.texts = []

    def respond(self, utterance, call):
        self.texts.append(utterance["text"])


@pytest.fixture
def asleep(kitchen, clock, tmp_path):
    replay = ReplayKitchen(kitchen.call)
    session = Session(replay, replay, clock, saved=SavedRecipes(tmp_path), asleep=True)
    session.prompts = []
    replay.show_prompt = lambda: session.prompts.append(True)
    return session


def test_nothing_happens_until_the_assistant_is_called(asleep, kitchen):
    asleep.handle({"utterance_id": "x", "text": "I want to cook beef stir-fry"}, Silent())
    assert (asleep.state, asleep.prompts, kitchen.events) == (ASLEEP, [], [])


@pytest.mark.parametrize("text, lang", [("Gemini, I want to cook.", "en"), ("gemini", "en"), ("Gemini，我想做饭", "zh")])
def test_calling_the_assistant_shows_the_prompt_and_asks_dish_or_ingredients(asleep, kitchen, text, lang):
    asleep.handle({"utterance_id": "x", "text": text}, Silent())
    assert (asleep.state, asleep.prompts) == (IDLE, [True])
    assert [(e["tool"], e["args"]["text"]) for e in kitchen.events] == [("say", OPENING[lang])]


def test_a_call_that_already_says_what_to_cook_goes_to_the_brain(asleep, kitchen):
    brain = Heard()
    asleep.handle({"utterance_id": "x", "text": "Gemini, I want to cook some chicken pasta"}, brain)
    assert (asleep.prompts, brain.texts, tools(kitchen)) == ([True], ["Gemini, I want to cook some chicken pasta"], [])


def test_a_saved_recipe_is_found_by_ingredient_shown_and_loaded(asleep, tmp_path):
    asleep.saved.save("Crispy chicken rice", "https://www.tiktok.com/@a/video/1",
                      ["2 chicken thighs", "1 cup rice"], ["Fry the chicken.", "Serve on rice."])
    asleep.handle({"utterance_id": "x", "text": "Gemini"}, Silent())
    (cand,) = asleep.call("find_saved_recipes", ingredients=["frozen chicken"])["candidates"]
    assert cand["title"] == "Crispy chicken rice"
    assert asleep.call("find_saved_recipes", ingredients=["chicken"])["candidates"] == []     # new candidates only
    assert asleep.call("show_recipe", recipe_id=cand["recipe_id"]) == {"ok": True}
    assert "Fry the chicken." in asleep.call("load_recipe", recipe_id=cand["recipe_id"])["text"]
    asleep.call("hide_recipe")
    assert asleep.state == COOKING and asleep.current_step()["text"] == "Fry the chicken."


def test_without_saved_recipes_the_search_finds_nothing(session):
    assert session.call("find_saved_recipes", ingredients=["chicken"]) == {"candidates": []}
