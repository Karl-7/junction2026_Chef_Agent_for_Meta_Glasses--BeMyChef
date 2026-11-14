"""The browser adapter and the server behind the glasses page, driven by a stand-in for the page."""
import base64
import threading

import pytest
from starlette.testclient import TestClient

from cooking.web import WebDevice
from scripts.serve_glasses import create_app, scripted

JPEG = b"\xff\xd8 not really a picture \xff\xd9"


def test_frame_comes_back_from_the_page_under_the_requested_id():
    sent = []
    device = WebDevice(sent.append)
    device.receive({"type": "frame", "frame_id": "f0", "jpeg": None})          # stale reply: ignored
    device.receive({"type": "frame", "frame_id": "f1", "jpeg": base64.b64encode(JPEG).decode()})
    assert device.capture_frame() == {"frame_id": "f1", "jpeg": JPEG}
    assert sent == [{"type": "capture", "frame_id": "f1"}]


def test_silent_page_gives_an_empty_frame_instead_of_hanging():
    device = WebDevice(lambda message: None, frame_timeout=0.05)
    assert device.capture_frame() == {"frame_id": "f1", "jpeg": None}
    assert device.capture_frame()["frame_id"] == "f2"


def test_closing_the_page_ends_the_wait_for_speech():
    device = WebDevice(lambda message: None)
    got = []
    waiter = threading.Thread(target=lambda: got.append(device.next_utterance()))
    waiter.start()
    device.close()
    waiter.join(2)
    assert got == [None]


class Page:
    """What ui/glasses.html does on the wire: answers capture requests, collects everything else."""

    def __init__(self, ws):
        self.ws = ws
        self.script = ws.receive_json()["script"]
        self.until_listening()

    def until_listening(self):
        seen = []
        while (m := self.ws.receive_json())["type"] != "listening":
            seen.append(m)
            if m["type"] == "capture":
                self.ws.send_json({"type": "frame", "frame_id": m["frame_id"],
                                   "jpeg": base64.b64encode(JPEG).decode()})
        return seen

    def speak(self, i):
        self.ws.send_json({"type": "utterance", **self.script[i]})
        return self.until_listening()


@pytest.fixture
def page(scenario):
    with TestClient(create_app(scripted(scenario))).websocket_connect("/ws") as ws:
        yield Page(ws)


def test_page_is_served(scenario):
    client = TestClient(create_app(scripted(scenario)))
    assert "Cooking Assistant" in client.get("/").text
    assert client.get("/icon.png").content.startswith(b"\x89PNG")


def test_script_sent_to_the_page_carries_no_annotations(page, scenario):
    assert page.script == [{"utterance_id": u["id"], "text": u["text"]} for u in scenario["utterances"]]


def test_whole_scripted_session_through_the_page(page):
    types = lambda messages: [m["type"] for m in messages]

    start = page.speak(0)
    assert types(start) == ["capture", "say", "recipe", "say"]
    assert start[1]["text"] == "Let me search."
    assert start[2]["recipe"] == {"title": "Quick beef stir-fry", "url": "https://recipes.example/beef-stir-fry",
                                  "image": None, "ingredients": ["beef", "soy sauce", "garlic", "spring onion"]}

    assert [m["recipe"]["title"] for m in page.speak(1) if m["type"] == "recipe"] == ["Beef and broccoli"]
    assert [m["recipe"]["title"] for m in page.speak(2) if m["type"] == "recipe"] == ["Home-style beef stir-fry"]
    assert types(page.speak(3)) == ["hide_recipe", "say"]
    assert page.speak(4) == []                                   # "pass me the salt": not for the assistant

    advice = page.speak(5)
    assert types(advice) == ["capture", "say"] and "soy sauce" in advice[1]["text"]

    which = page.speak(6)
    assert types(which) == ["capture", "highlight", "say"]
    assert which[1] == {"type": "highlight", "frame_id": which[0]["frame_id"], "box": [600, 100, 760, 260],
                        "label": "soy sauce bowl", "ttl": 5.0}
    assert which[2]["text"] == "This one."
