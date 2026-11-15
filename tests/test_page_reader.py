"""The Gemini page reader against a stand-in for the model (no network, no spend)."""
from cooking.page_reader import MAX_PAGE_CHARS, GeminiReader, reading, visible_text

PAGE = ('<html><head><style>p { color: red }</style><script>var steps = "not text";</script></head>'
        '<body><!-- ad --><h1>麻婆豆腐</h1><p>豆腐&nbsp;400克</p><p>切块。</p></body></html>')
ANSWER = '{"title": " 麻婆豆腐 ", "ingredients": ["豆腐 400克", ""], "steps": ["切块。", "炒。"]}'


class Model:
    def __init__(self, answer):
        self.answer, self.requests = answer, []

    def generate(self, purpose, body):
        self.requests.append((purpose, body))
        if isinstance(self.answer, Exception):
            raise self.answer
        return {"candidates": [{"content": {"parts": [{"text": self.answer}]}}]}


def test_visible_text_drops_scripts_styles_comments_and_tags():
    assert visible_text(PAGE) == "麻婆豆腐 豆腐 400克 切块。"


def test_answer_is_reduced_to_a_usable_reading():
    assert reading(ANSWER) == {"title": "麻婆豆腐", "ingredients": ["豆腐 400克"], "steps": ["切块。", "炒。"]}
    assert reading("{}") == {"title": "", "ingredients": [], "steps": []}
    assert reading("not json") is None and reading("[1]") is None


def test_one_json_request_with_the_pages_text_cut_to_size():
    model = Model(ANSWER)
    assert GeminiReader(model)(PAGE + "<p>" + "x" * (2 * MAX_PAGE_CHARS) + "</p>")["title"] == "麻婆豆腐"
    ((purpose, body),) = model.requests
    text = body["contents"][0]["parts"][0]["text"]
    assert purpose == "read_page" and "麻婆豆腐 豆腐" in text and "not text" not in text
    assert text.count("x") < MAX_PAGE_CHARS
    assert body["generationConfig"]["responseMimeType"] == "application/json"


def test_an_empty_page_is_not_sent_and_a_model_error_gives_no_reading():
    model = Model(ANSWER)
    assert GeminiReader(model)("<html><script>x</script></html>") is None and model.requests == []
    assert GeminiReader(Model(RuntimeError("Gemini 429")))(PAGE) is None
