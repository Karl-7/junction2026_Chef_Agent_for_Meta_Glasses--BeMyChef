"""Browser adapter: the glasses page (ui/glasses.html) is the device, reached over a message channel.

The page draws the 600x600 display and speaks; its webcam stands in for the glasses camera. The
orchestrator runs in its own thread and blocks on this adapter; the server feeds page messages
in through `receive` and gives `send` to deliver messages to the page.

Server -> page: listening | capture{frame_id} | say{text} | recipe{recipe} | hide_recipe
                | step{number, total, text, ingredients} | highlight{frame_id, box, label, ttl}
                | clear_highlight
Page -> server: utterance{utterance_id, text} | frame{frame_id, jpeg (base64) or null}
"""
from __future__ import annotations

import base64
import queue
from typing import Callable

FRAME_TIMEOUT_S = 5.0
RECIPE_FIELDS = ("title", "url", "image", "ingredients", "instructions")    # what the display shows


class WebDevice:
    """Implements `cooking.device.Device`."""

    def __init__(self, send: Callable[[dict], None], frame_timeout: float = FRAME_TIMEOUT_S):
        self._send = send
        self._frame_timeout = frame_timeout
        self._utterances: queue.Queue = queue.Queue()
        self._frames: queue.Queue = queue.Queue()
        self._frame_count = 0

    def receive(self, message: dict) -> None:
        if message.get("type") == "utterance":
            self._utterances.put({"utterance_id": message.get("utterance_id"), "text": message["text"]})
        elif message.get("type") == "frame":
            self._frames.put(message)

    def close(self) -> None:
        self._utterances.put(None)               # ends Session.run

    def next_utterance(self):
        self._send({"type": "listening"})
        return self._utterances.get()

    def capture_frame(self):
        self._frame_count += 1
        frame_id = f"f{self._frame_count}"
        self._send({"type": "capture", "frame_id": frame_id})
        jpeg = None
        try:
            while (reply := self._frames.get(timeout=self._frame_timeout))["frame_id"] != frame_id:
                pass                             # a late reply to an earlier request
            if reply.get("jpeg"):
                jpeg = base64.b64decode(reply["jpeg"])
        except queue.Empty:                      # page gone or camera stuck: an empty frame, not a hang
            pass
        return {"frame_id": frame_id, "jpeg": jpeg}

    def say(self, text):
        self._send({"type": "say", "text": text})

    def show_recipe(self, recipe):
        self._send({"type": "recipe", "recipe": {k: recipe.get(k) for k in RECIPE_FIELDS}})

    def hide_recipe(self):
        self._send({"type": "hide_recipe"})

    def show_step(self, step):
        self._send({"type": "step", **step})

    def show_highlight(self, frame_id, box, label, ttl):
        self._send({"type": "highlight", "frame_id": frame_id, "box": box, "label": label, "ttl": ttl})

    def clear_highlight(self):
        self._send({"type": "clear_highlight"})
