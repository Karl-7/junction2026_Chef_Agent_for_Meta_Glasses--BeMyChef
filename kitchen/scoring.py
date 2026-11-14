"""Score one recorded run against the scenario's annotations. Everything here is decided by code.

Each check is a named pass/fail. reward = passed checks / all code-judged checks. Spoken content
cannot be judged by code: those answers are returned under "to_judge" and listed under "skipped".
An utterance the assistant never fetched fails all of its checks.
"""
from __future__ import annotations

RESPONSE_TIME_LIMIT_S = 3.0   # TASK section 7.3: end of the user's utterance to the first spoken word
ANSWERS = ("say", "highlight")


def _area_of_overlap(a, b) -> float:
    h = min(a[2], b[2]) - max(a[0], b[0])
    w = min(a[3], b[3]) - max(a[1], b[1])
    return h * w if h > 0 and w > 0 else 0.0


def highlighted_object(box, objects) -> str | None:
    """The annotated object the box overlaps most; None when it overlaps nothing or ties."""
    areas = sorted(((_area_of_overlap(box, o["box"]), o["name"]) for o in objects), reverse=True)
    if not areas or areas[0][0] == 0 or (len(areas) > 1 and areas[0][0] == areas[1][0]):
        return None
    return areas[0][1]


def score(scenario: dict, events: list[dict]) -> dict:
    page = {r["recipe_id"]: r.get("same_as", r["recipe_id"]) for r in scenario["recipes"]}
    segments: dict[str, list[dict]] = {}     # utterance id -> events up to the next utterance
    heard_at: dict[str, float] = {}
    current = None
    for e in events:
        if e["tool"] == "next_utterance":
            current = e["result"].get("utterance_id")
            if current:
                segments[current], heard_at[current] = [], e["time"]
        elif current:
            segments[current].append(e)

    checks, latencies, to_judge, skipped = [], [], [], []
    shown_pages: set[str] = set()            # pages shown before the utterance being judged
    on_screen: str | None = None

    for u in scenario["utterances"]:
        uid, kind = u["id"], u["kind"]
        reached = uid in segments
        seg = segments.get(uid, [])

        def check(name, passed, detail=""):
            checks.append({"utterance": uid, "name": name, "passed": bool(reached and passed),
                           "detail": detail if reached else "utterance not reached"})

        shows = [page.get(e["args"]["recipe_id"]) for e in seg if e["tool"] == "show_recipe"]
        tools = [e["tool"] for e in seg]

        if kind == "start":
            check("flow/recipe_shown", shows)
        elif kind == "reject":
            check("flow/different_recipe_after_no", shows and not shown_pages & set(shows),
                  f"shown {shows}, earlier {sorted(shown_pages)}")
        elif kind == "accept":
            loads = [i for i, e in enumerate(seg)
                     if e["tool"] == "load_recipe" and page.get(e["args"]["recipe_id"]) == on_screen]
            hides = [i for i, t in enumerate(tools) if t == "hide_recipe"]
            check("flow/load_before_hide", loads and hides and loads[0] < hides[0],
                  f"on screen {on_screen}, calls {tools}")
        elif kind == "help":
            first_answer = next((i for i, t in enumerate(tools) if t in ANSWERS), None)
            check("flow/frame_before_answer",
                  first_answer is not None and "capture_frame" in tools[:first_answer], f"calls {tools}")
            if "target" in u:
                boxes = [e["args"]["box"] for e in seg if e["tool"] == "highlight"]
                got = highlighted_object(boxes[-1], u["objects"]) if boxes else None
                check("highlight/correct_object", got == u["target"], f"highlighted {got}, correct {u['target']}")
            if "facts" in u:
                said = [e["args"]["text"] for e in seg if e["tool"] == "say"]
                skipped.append({"utterance": uid, "name": "spoken/matches_facts", "reason": "needs the LLM judge"})
                to_judge.append({"utterance": uid, "question": u["text"], "said": said, "facts": u["facts"]})
        elif kind == "unaddressed":
            check("flow/silent_when_not_called", not any(t in ANSWERS + ("show_recipe",) for t in tools),
                  f"calls {tools}")

        if kind != "unaddressed":
            first_say = next((e["time"] for e in seg if e["tool"] == "say"), None)
            seconds = None if first_say is None else first_say - heard_at[uid]
            latencies.append({"utterance": uid, "seconds": seconds})
            check("response_time", seconds is not None and seconds <= RESPONSE_TIME_LIMIT_S,
                  "nothing said" if seconds is None else f"{seconds:.3f} s")

        shown_pages |= set(shows)
        if shows:
            on_screen = shows[-1]

    passed = sum(c["passed"] for c in checks)
    return {"reward": passed / len(checks), "passed": passed, "checks_total": len(checks),
            "checks": checks, "skipped": skipped, "latencies": latencies, "to_judge": to_judge}
