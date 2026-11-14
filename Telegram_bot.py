import html
import os
import re
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

import httpx
import yt_dlp
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

from cooking.chat import FAILED, NOT_A_RECIPE, WELCOME, WORKING, recipe_messages
from cooking.media import open_video
from cooking.saved import SavedRecipes

load_dotenv()

# --- 配置 ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("请先在 .env 中设置 TELEGRAM_BOT_TOKEN")

TG_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

CANDIDATE_MODELS = [
    "gemini-4-hydrogen",
    "gemini-3.8-flash",
]

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
saved_recipes = SavedRecipes()      # what the glasses offer when the user names ingredients to use up
TIKTOK_URL_REGEX = re.compile(r"https?://(?:www\.|vm\.|vt\.|m\.)?tiktok\.com/[^\s]+")


# --- Gemini 结构化输出格式 ---
class RecipeOutput(BaseModel):
    is_cooking_video: bool = Field(
        description="True if the video shows a recipe or cooking process, False otherwise."
    )
    dish_name: str = Field(description="Name of the dish being prepared.")
    prep_time: str = Field(description="Estimated prep time (e.g., '15 mins').")
    cook_time: str = Field(description="Estimated cooking time (e.g., '25 mins').")
    servings: str = Field(description="Estimated number of servings.")
    ingredients: List[str] = Field(
        description="List of ingredients with quantities inferred from the video/audio/on-screen text."
    )
    steps: List[str] = Field(
        description="Step-by-step cooking instructions in clear, concise order."
    )
    final_dish_timestamp_seconds: float = Field(
        description="The exact timestamp (in seconds) in the video that gives the clearest, most appetizing shot of the finished plated dish."
    )


# --- Telegram API 辅助函数 ---
def to_html(message: str) -> str:
    """A `cooking.chat` message as Telegram HTML: text escaped, a `*heading*` line made bold."""
    return re.sub(r"^\*(.+)\*$", r"<b>\1</b>", html.escape(message), flags=re.M)


def send_chat_action(chat_id: int, action: str = "typing") -> None:
    try:
        httpx.post(
            f"{TG_API_BASE}/sendChatAction",
            json={"chat_id": chat_id, "action": action},
            timeout=10.0,
        )
    except Exception:
        pass


def send_telegram_text(chat_id: int, message: str) -> None:
    payload = {"chat_id": chat_id, "text": to_html(message), "parse_mode": "HTML"}
    resp = httpx.post(f"{TG_API_BASE}/sendMessage", json=payload, timeout=30.0)
    # 如果 HTML 格式偶然解析失败，自动降级为纯文本重发，保证不丢消息
    if resp.status_code == 400:
        resp = httpx.post(f"{TG_API_BASE}/sendMessage", json={"chat_id": chat_id, "text": message}, timeout=30.0)
    if resp.status_code >= 400:
        raise RuntimeError(f"Telegram sendMessage error {resp.status_code}: {resp.text}")


def send_telegram_messages(chat_id: int, messages: List[str], image_path: Optional[str] = None) -> None:
    """Send `messages` in order; the photo, if any, goes with the first."""
    for idx, message in enumerate(messages):
        if idx == 0 and image_path:
            send_chat_action(chat_id, "upload_photo")
            send_telegram_photo(chat_id, image_path, caption=to_html(message))
        else:
            send_telegram_text(chat_id, message)


def send_telegram_photo(chat_id: int, image_path: str, caption: str = "") -> None:
    with open(image_path, "rb") as f:
        resp = httpx.post(
            f"{TG_API_BASE}/sendPhoto",
            data={
                "chat_id": chat_id,
                "caption": caption[:1024],
                "parse_mode": "HTML",
            },
            files={"photo": ("dish.jpg", f, "image/jpeg")},
            timeout=30.0,
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"Telegram sendPhoto error {resp.status_code}: {resp.text}")


# --- 视频下载、抽帧与 Gemini 分析 ---
def download_tiktok_video(url: str, output_path: str) -> None:
    ydl_opts = {
        "format": "mp4/best[ext=mp4]/best",
        "outtmpl": output_path,
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])


def analyze_video_with_gemini(video_path: str) -> RecipeOutput:
    print("[Gemini] 开始上传视频...", flush=True)
    uploaded_file = gemini_client.files.upload(file=video_path)

    try:
        while str(uploaded_file.state).endswith("PROCESSING"):
            time.sleep(2)
            uploaded_file = gemini_client.files.get(name=uploaded_file.name)

        if str(uploaded_file.state).endswith("FAILED"):
            raise RuntimeError("Gemini failed to process the uploaded video.")

        prompt = (
            "Watch and listen to this cooking video carefully, including narration, captions "
            "and what is shown. Write it down as an accurate recipe someone can cook from. "
            "Give ingredients with quantities. Write each step as one short, plain instruction. "
            "Name the dish plainly, in sentence case. "
            "Identify the best timestamp showing the finished dish so we can take a snapshot of it."
        )

        last_error = None
        response = None
        for model_name in CANDIDATE_MODELS:
            try:
                print(f"[Gemini] 尝试模型: {model_name}", flush=True)
                response = gemini_client.models.generate_content(
                    model=model_name,
                    contents=[uploaded_file, prompt],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=RecipeOutput,
                        temperature=0.3,
                    ),
                )
                print(f"[Gemini] 模型 {model_name} 成功!", flush=True)
                break
            except Exception as e:
                print(f"[Gemini] 模型 {model_name} 失败: {e}", flush=True)
                last_error = e

        if response is None:
            raise last_error

        return RecipeOutput.model_validate_json(response.text)
    finally:
        try:
            gemini_client.files.delete(name=uploaded_file.name)
        except Exception:
            pass


def handle_tiktok_message(chat_id: int, tiktok_url: str) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        video_path = os.path.join(tmpdir, "tiktok_video.mp4")
        image_path = os.path.join(tmpdir, "dish.jpg")

        try:
            send_telegram_text(chat_id, WORKING)
            send_chat_action(chat_id, "typing")

            download_tiktok_video(tiktok_url, video_path)
            print(f"[TG] 视频下载完成: {tiktok_url}", flush=True)

            # Cloudinary shrinks the video for Gemini and takes the dish photo (local fallback without it).
            with open_video(video_path) as video:
                recipe = analyze_video_with_gemini(video.for_model(tmpdir))

                if not recipe.is_cooking_video:
                    send_telegram_text(chat_id, NOT_A_RECIPE)
                    return

                has_photo = video.frame(recipe.final_dish_timestamp_seconds, image_path)

            try:
                saved_recipes.save(recipe.dish_name, tiktok_url, recipe.ingredients, recipe.steps, recipe.prep_time,
                                   recipe.cook_time, recipe.servings, photo=image_path if has_photo else None)
            except OSError as e:                # the recipe is still sent
                print(f"[SAVE ERROR] {e}", flush=True)

            # 发回菜谱 + 成品图: three messages, the photo with the first
            messages = recipe_messages(
                recipe.dish_name, recipe.prep_time, recipe.cook_time, recipe.servings,
                recipe.ingredients, recipe.steps,
            )
            send_telegram_messages(chat_id, messages, image_path=image_path if has_photo else None)
            print("[TG] 菜谱与成品图发送成功!", flush=True)

        except Exception as e:
            print(f"[ERROR] {e}", flush=True)
            try:
                send_telegram_text(chat_id, FAILED)
            except Exception as send_err:
                print(f"[SEND ERROR] {send_err}", flush=True)


# --- 主循环（长轮询，无需 ngrok） ---
def main() -> None:
    print("Cooking assistant Telegram bot 已启动，正在监听消息...", flush=True)
    offset = 0
    executor = ThreadPoolExecutor(max_workers=4)

    while True:
        try:
            resp = httpx.get(
                f"{TG_API_BASE}/getUpdates",
                params={"timeout": 30, "offset": offset},
                timeout=40.0,
            )
            data = resp.json()
            if not data.get("ok"):
                print(f"[TG POLL ERROR] {data}", flush=True)
                time.sleep(3)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message") or update.get("edited_message")
                if not msg:
                    continue

                chat_id = msg["chat"]["id"]
                text = msg.get("text") or msg.get("caption") or ""
                print(f"[TG MSG] chat_id={chat_id} text={repr(text)}", flush=True)

                match = TIKTOK_URL_REGEX.search(text)
                if match:
                    executor.submit(handle_tiktok_message, chat_id, match.group(0))
                else:
                    executor.submit(send_telegram_text, chat_id, WELCOME)

        except KeyboardInterrupt:
            print("\nBot stopped.", flush=True)
            break
        except Exception as e:
            print(f"[POLL LOOP ERROR] {e}", flush=True)
            time.sleep(3)


if __name__ == "__main__":
    main()