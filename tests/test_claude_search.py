"""The Claude finder against a stand-in for the API client (no network, no spend)."""
from types import SimpleNamespace

import anthropic
import httpx2

from cooking.claude_search import ClaudeSearch, leads, unavailable

ANSWER = ('Here you go:\n```json\n[{"url": " https://a.example/mapo ", "title": "麻婆豆腐", "ingredients": ["豆腐 400克", ""], '
          '"steps": ["切块。", "炒。"]}, {"title": "no url"}, "junk", {"url": "https://b.example/x", "steps": "not a list"}]\n```')


def response(stop_reason, text=""):
    usage = SimpleNamespace(to_dict=lambda: {"input_tokens": 10, "output_tokens": 5})
    return SimpleNamespace(stop_reason=stop_reason, model="claude-opus-5-5", usage=usage,
                           content=[SimpleNamespace(type="server_tool_use"), SimpleNamespace(type="text", text=text)])


class Api:
    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **request):
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def no_credit():
    http = httpx2.Response(400, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    return anthropic.BadRequestError("Your credit balance is too low", response=http, body=None)


def test_answer_is_reduced_to_usable_leads():
    assert leads(ANSWER) == [
        {"url": "https://a.example/mapo", "title": "麻婆豆腐", "ingredients": ["豆腐 400克"], "steps": ["切块。", "炒。"]},
        {"url": "https://b.example/x", "title": "", "ingredients": [], "steps": []}]
    assert leads("I could not find anything.") == [] and leads("[not json]") == []


def test_one_request_with_the_search_tool_and_the_pages_to_skip():
    api, logged = Api(response("end_turn", ANSWER)), []
    found = ClaudeSearch(api, "claude-opus-5-5", logged.append)("麻婆豆腐", ["https://old.example/1"])
    assert [lead["url"] for lead in found] == ["https://a.example/mapo", "https://b.example/x"]
    (request,) = api.requests
    assert request["tools"] == [{"type": "web_search_20260209", "name": "web_search", "max_uses": 2}]
    assert "麻婆豆腐" in request["messages"][0]["content"] and "https://old.example/1" in request["messages"][0]["content"]
    assert logged[0]["usage"] == {"input_tokens": 10, "output_tokens": 5}


def test_paused_search_is_resumed_with_the_models_own_turn():
    paused = response("pause_turn")
    api = Api(paused, response("end_turn", ANSWER))
    assert len(ClaudeSearch(api, "m")("mapo tofu", [])) == 2
    resumed = api.requests[1]["messages"]
    assert [m["role"] for m in resumed] == ["user", "assistant"] and resumed[1]["content"] is paused.content


def test_refusal_and_api_errors_give_no_leads_instead_of_raising():
    assert ClaudeSearch(Api(response("refusal")), "m")("x", []) == []
    assert ClaudeSearch(Api(no_credit()), "m")("x", []) == []


def test_unavailable_says_why():
    assert unavailable(Api(response("end_turn", "ok")), "m") is None
    assert "credit balance is too low" in unavailable(Api(no_credit()), "m")
