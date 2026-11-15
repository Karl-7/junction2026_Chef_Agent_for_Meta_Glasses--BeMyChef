"""The bot sends the recipe as separate messages, photo first, with download, Gemini and Telegram stubbed."""
import pytest

pytest.importorskip("httpx")
pytest.importorskip("yt_dlp")
pytest.importorskip("google.genai")


@pytest.fixture
def bot(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    import Telegram_bot
    from cooking.saved import SavedRecipes
    monkeypatch.setattr(Telegram_bot, "saved_recipes", SavedRecipes(tmp_path))
    return Telegram_bot


def recipe(bot, **overrides):
    fields = dict(is_cooking_video=True, dish_name="Garlic butter salmon", prep_time="15 mins",
                  cook_time="20 mins", servings="2", ingredients=["2 salmon fillets"],
                  steps=["Season the salmon."], final_dish_timestamp_seconds=3.0)
    return bot.RecipeOutput(**{**fields, **overrides})


def run(bot, monkeypatch, result):
    """What reaches Telegram, in order: ("text", html or plain text) and ("photo", caption)."""
    sent = []
    monkeypatch.setattr(bot, "download_tiktok_video", lambda url, path: None)
    monkeypatch.setattr(bot, "analyze_video_with_gemini", lambda path: result)
    monkeypatch.setattr(bot, "extract_frame", lambda *args: True)
    monkeypatch.setattr(bot, "send_chat_action", lambda *args: None)
    monkeypatch.setattr(bot, "send_telegram_text", lambda chat_id, message: sent.append(("text", message)))
    monkeypatch.setattr(bot, "send_telegram_photo",
                        lambda chat_id, path, caption="": sent.append(("photo", caption)))
    bot.handle_tiktok_message(1, "https://www.tiktok.com/@a/video/1")
    return sent


def test_recipe_goes_out_as_three_messages_with_the_photo(bot, monkeypatch):
    working, *messages = run(bot, monkeypatch, recipe(bot))
    assert working == ("text", bot.WORKING)
    assert [(kind, text.splitlines()[0]) for kind, text in messages] == [
        ("photo", "<b>Garlic butter salmon</b>"), ("text", "*Ingredients*"), ("text", "*Method*")]


def test_a_video_that_is_not_a_recipe_gets_one_plain_answer(bot, monkeypatch):
    assert run(bot, monkeypatch, recipe(bot, is_cooking_video=False)) == [
        ("text", bot.WORKING), ("text", bot.NOT_A_RECIPE)]
    assert bot.saved_recipes.all() == []


def test_the_recipe_is_saved_for_the_glasses(bot, monkeypatch):
    run(bot, monkeypatch, recipe(bot))
    (saved,) = bot.saved_recipes.search(["salmon"], [])
    assert (saved["title"], saved["url"], saved["instructions"]) == (
        "Garlic butter salmon", "https://www.tiktok.com/@a/video/1", ["Season the salmon."])


def test_headings_become_bold_and_text_is_escaped(bot):
    assert bot.to_html("*Fish & chips*\n- 2 * 3 <big> potatoes") == "<b>Fish &amp; chips</b>\n- 2 * 3 &lt;big&gt; potatoes"


def test_telegram_posts_its_updates_to_the_server(bot, monkeypatch):
    from starlette.testclient import TestClient
    from scripts.serve_glasses import create_app
    handled = []
    monkeypatch.setattr(bot.executor, "submit", lambda work, *args: handled.append((work.__name__, *args)))
    client = TestClient(create_app(lambda device: None, bot=bot))
    link = {"message": {"chat": {"id": 7}, "text": "look https://www.tiktok.com/@a/video/1"}}
    secret = {"X-Telegram-Bot-Api-Secret-Token": bot.webhook_secret()}

    assert client.post("/telegram", json=link).status_code == 403            # not from Telegram
    assert handled == []
    assert client.post("/telegram", json=link, headers=secret).status_code == 200
    assert client.post("/telegram", json={"message": {"chat": {"id": 7}, "text": "hi"}}, headers=secret).status_code == 200
    assert handled == [("handle_tiktok_message", 7, "https://www.tiktok.com/@a/video/1"),
                       ("send_telegram_text", 7, bot.WELCOME)]


def test_the_public_address_is_fly_s_unless_one_is_given(bot, monkeypatch):
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    monkeypatch.delenv("FLY_APP_NAME", raising=False)
    assert bot.public_url() is None                                          # a laptop: the bot polls
    monkeypatch.setenv("FLY_APP_NAME", "bemychef")
    assert bot.public_url() == "https://bemychef.fly.dev"
    monkeypatch.setenv("PUBLIC_URL", "https://cook.example/")
    assert bot.public_url() == "https://cook.example"
