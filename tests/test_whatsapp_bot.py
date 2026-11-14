"""The bot sends the recipe as separate messages, photo first, with download, Gemini and Twilio stubbed."""
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("twilio")


@pytest.fixture
def bot(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    import whatsapp_bot
    return whatsapp_bot


def recipe(bot, **overrides):
    fields = dict(is_cooking_video=True, dish_name="Garlic butter salmon", prep_time="15 mins",
                  cook_time="20 mins", servings="2", ingredients=["2 salmon fillets"],
                  steps=["Season the salmon."], final_dish_timestamp_seconds=3.0)
    return bot.RecipeOutput(**{**fields, **overrides})


def run(bot, monkeypatch, result):
    sent = []
    monkeypatch.setattr(bot, "download_tiktok_video", lambda url, path: None)
    monkeypatch.setattr(bot, "analyze_video_with_gemini", lambda path: result)
    monkeypatch.setattr(bot, "extract_frame", lambda *args: True)
    monkeypatch.setattr(bot, "PUBLIC_BASE_URL", "https://bot.example")
    monkeypatch.setattr(bot, "send_whatsapp_messages",
                        lambda frm, to, messages, media_url=None: sent.append((messages, media_url)))
    bot.process_tiktok_request("whatsapp:+1", "whatsapp:+2", "https://www.tiktok.com/@a/video/1")
    return sent


def test_recipe_goes_out_as_three_messages_with_the_photo(bot, monkeypatch):
    [(messages, media_url)] = run(bot, monkeypatch, recipe(bot))
    assert [m.splitlines()[0] for m in messages] == ["*Garlic butter salmon*", "*Ingredients*", "*Method*"]
    assert media_url.startswith("https://bot.example/static/dish_")


def test_a_video_that_is_not_a_recipe_gets_one_plain_answer(bot, monkeypatch):
    assert run(bot, monkeypatch, recipe(bot, is_cooking_video=False)) == [([bot.NOT_A_RECIPE], None)]
