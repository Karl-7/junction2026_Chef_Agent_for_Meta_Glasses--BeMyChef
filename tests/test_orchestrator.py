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
    assert tools(kitchen)[-2:] == ["load_recipe", "hide_recipe"] and session.state == COOKING


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
