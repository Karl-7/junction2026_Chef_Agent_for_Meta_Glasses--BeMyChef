"""The scoring must be able to both pass and fail: oracle full marks, do-nothing zero, and each
kind of mistake costs the check that names it."""
import pytest

from cooking.orchestrator import Session
from cooking.replay import ReplayKitchen
from kitchen.kitchen import Kitchen
from kitchen.scoring import highlighted_object, score
from policies.scripted import Scripted


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def play(scenario, brain, clock=None):
    """Run a brain through the orchestrator; return the score."""
    clock = clock or Clock()
    kitchen = Kitchen(scenario, clock)
    replay = ReplayKitchen(kitchen.call)
    Session(replay, replay, clock).run(brain)
    return score(scenario, kitchen.events)


def raw(scenario, script):
    """Call the kitchen directly, with no orchestrator in between: script is [(seconds, tool, args)]."""
    clock = Clock()
    kitchen = Kitchen(scenario, clock)
    for seconds, tool, args in script:
        clock.now += seconds
        kitchen.call(tool, **args)
    return score(scenario, kitchen.events)


def failed(result):
    return sorted((c["utterance"], c["name"]) for c in result["checks"] if not c["passed"])


ORACLE_SCRIPT = [   # what the oracle does, as raw kitchen calls
    (0, "next_utterance", {}), (0, "capture_frame", {}), (0, "say", {"text": "Let me search."}),
    (0, "search_recipes", {"dish": "beef stir-fry"}), (0, "show_recipe", {"recipe_id": "r1"}),
    (0, "say", {"text": "OK?"}),
    (0, "next_utterance", {}), (0, "say", {"text": "Searching again."}),
    (0, "show_recipe", {"recipe_id": "r2"}), (0, "say", {"text": "OK?"}),
    (0, "next_utterance", {}), (0, "show_recipe", {"recipe_id": "r3"}), (0, "say", {"text": "OK?"}),
    (0, "next_utterance", {}), (0, "load_recipe", {"recipe_id": "r3"}), (0, "hide_recipe", {}),
    (0, "say", {"text": "OK."}),
    (0, "next_utterance", {}),
    (0, "next_utterance", {}), (0, "capture_frame", {}), (0, "say", {"text": "Wait, then soy sauce."}),
    (0, "next_utterance", {}), (0, "capture_frame", {}),
    (0, "highlight", {"box": [600, 100, 760, 260]}), (0, "say", {"text": "This one."}),
    (0, "next_utterance", {}),
]


def replace(script, index, *steps):
    return script[:index] + list(steps) + script[index + 1:]


def test_oracle_passes_every_check(scenario):
    result = play(scenario, Scripted(scenario))
    assert failed(result) == [] and result["reward"] == 1.0
    assert result["checks_total"] == 14
    assert [s["utterance"] for s in result["skipped"]] == ["u6"]     # spoken content: not judged by code
    assert result["to_judge"][0]["said"] == [scenario["utterances"][5]["reference_answer"]]


def test_doing_nothing_scores_zero(scenario):
    result = score(scenario, [])
    assert result["reward"] == 0.0 and result["checks_total"] == 14
    assert {c["detail"] for c in result["checks"]} == {"utterance not reached"}


def test_early_stop_loses_exactly_the_cooking_help(scenario):
    result = play(scenario, Scripted(scenario, stop_after_accept=True))
    assert failed(result) == [("u6", "flow/frame_before_answer"), ("u6", "response_time"),
                              ("u7", "flow/frame_before_answer"), ("u7", "highlight/correct_object"),
                              ("u7", "response_time")]
    assert result["reward"] == pytest.approx(9 / 14)


def test_raw_oracle_script_passes(scenario):
    assert failed(raw(scenario, ORACLE_SCRIPT)) == []


def test_same_recipe_after_no_fails(scenario):
    script = replace(ORACLE_SCRIPT, 8, (0, "show_recipe", {"recipe_id": "r1"}))
    assert ("u2", "flow/different_recipe_after_no") in failed(raw(scenario, script))


def test_same_page_under_another_id_after_no_fails(scenario):
    script = replace(ORACLE_SCRIPT, 8, (0, "show_recipe", {"recipe_id": "r1-ad"}))
    assert ("u2", "flow/different_recipe_after_no") in failed(raw(scenario, script))


def test_hide_before_load_fails(scenario):
    script = replace(ORACLE_SCRIPT, 14, (0, "hide_recipe", {}), (0, "load_recipe", {"recipe_id": "r3"}))
    script = replace(script, 16)
    assert failed(raw(scenario, script)) == [("u4", "flow/load_before_hide")]


def test_loading_a_recipe_that_was_not_on_screen_fails(scenario):
    script = replace(ORACLE_SCRIPT, 14, (0, "load_recipe", {"recipe_id": "r2"}))
    assert failed(raw(scenario, script)) == [("u4", "flow/load_before_hide")]


def test_answer_without_a_frame_fails(scenario):
    script = replace(ORACLE_SCRIPT, 19)
    assert failed(raw(scenario, script)) == [("u6", "flow/frame_before_answer")]


def test_talking_when_not_called_fails(scenario):
    script = replace(ORACLE_SCRIPT, 17, (0, "next_utterance", {}), (0, "say", {"text": "Here is the salt."}))
    assert failed(raw(scenario, script)) == [("u5", "flow/silent_when_not_called")]


def test_wrong_bowl_fails(scenario):
    script = replace(ORACLE_SCRIPT, 23, (0, "highlight", {"box": [600, 300, 760, 460]}))
    assert failed(raw(scenario, script)) == [("u7", "highlight/correct_object")]


def test_slow_first_word_fails_and_is_reported(scenario):
    script = replace(ORACLE_SCRIPT, 20, (3.5, "say", {"text": "Wait, then soy sauce."}))
    result = raw(scenario, script)
    assert failed(result) == [("u6", "response_time")]
    assert {"utterance": "u6", "seconds": 3.5} in result["latencies"]


def test_highlight_goes_to_the_object_it_overlaps_most(scenario):
    objects = scenario["utterances"][6]["objects"]
    assert highlighted_object([590, 90, 770, 320], objects) == "soy sauce bowl"   # loose box, spills over
    assert highlighted_object([0, 0, 100, 100], objects) is None                  # touches nothing
    assert highlighted_object([600, 100, 760, 460], objects) is None              # covers both bowls equally
