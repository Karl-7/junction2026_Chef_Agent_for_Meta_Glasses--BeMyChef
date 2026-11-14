import html
import os
import re
import time
import tempfile
import subprocess
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

import httpx
import yt_dlp
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from google import genai
from google.genai import types

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
    chef_gordon_note: str = Field(
        description="A witty, passionate 1-2 sentence remark in the style of Chef Gordon Ramsay about the dish."
    )
    final_dish_timestamp_seconds: float = Field(
        description="The exact timestamp (in seconds) in the video that gives the clearest, most appetizing shot of the finished plated dish."
    )


# --- Telegram API 辅助函数 ---
def split_message(text: str, max_len: int = 3800) -> List[str]:
    if len(text) <= max_len:
        return [text]
    chunks: List[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > max_len:
            if current:
                chunks.append(current.rstrip())
                current = ""
            while len(line) > max_len:
                chunks.append(line[:max_len])
                line = line[max_len:]
        current += line
    if current:
        chunks.append(current.rstrip())
    return chunks


def send_chat_action(chat_id: int, action: str = "typing") -> None:
    try:
        httpx.post(
            f"{TG_API_BASE}/sendChatAction",
            json={"chat_id": chat_id, "action": action},
            timeout=10.0,
        )
    except Exception:
        pass


def send_telegram_text(chat_id: int, text: str, parse_mode: Optional[str] = "HTML") -> None:
    for chunk in split_message(text):
        payload = {"chat_id": chat_id, "text": chunk}
        if parse_mode:
            payload["parse_mode"] = parse_mode

        resp = httpx.post(f"{TG_API_BASE}/sendMessage", json=payload, timeout=30.0)
        # 如果 HTML 格式偶然解析失败，自动降级为纯文本重发，保证不丢消息
        if resp.status_code == 400 and parse_mode:
            payload.pop("parse_mode", None)
            resp = httpx.post(f"{TG_API_BASE}/sendMessage", json=payload, timeout=30.0)
        if resp.status_code >= 400:
            raise RuntimeError(f"Telegram sendMessage error {resp.status_code}: {resp.text}")


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


def extract_frame(video_path: str, timestamp: float, output_image_path: str) -> bool:
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                str(max(0.0, timestamp)),
                "-i",
                video_path,
                "-frames:v",
                "1",
                "-q:v",
                "2",
                output_image_path,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return os.path.exists(output_image_path)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False


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
            "You are Chef_Gordon, an expert culinary AI modeled after Chef Gordon Ramsay. "
            "Watch and listen to this cooking video carefully (including narration, captions, "
            "and visual cues). Summarize it into an accurate, structured recipe. "
            "Keep steps concise and clear. "
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


def format_telegram_recipe(recipe: RecipeOutput) -> str:
    e = html.escape
    ingredients_text = "\n".join(f"• {e(item)}" for item in recipe.ingredients)
    steps_text = "\n".join(f"{i}. {e(step)}" for i, step in enumerate(recipe.steps, 1))

    return (
        f"👨‍🍳 <b>Chef_Gordon's Kitchen</b> 👨‍🍳\n\n"
        f"🍽️ <b>{e(recipe.dish_name)}</b>\n"
        f"⏱️ Prep: {e(recipe.prep_time)} | Cook: {e(recipe.cook_time)} | Servings: {e(recipe.servings)}\n\n"
        f"💬 <i>\"{e(recipe.chef_gordon_note)}\"</i>\n\n"
        f"🛒 <b>Ingredients:</b>\n{ingredients_text}\n\n"
        f"🔥 <b>Instructions:</b>\n{steps_text}"
    )


def handle_tiktok_message(chat_id: int, tiktok_url: str) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        video_path = os.path.join(tmpdir, "tiktok_video.mp4")
        image_path = os.path.join(tmpdir, "dish.jpg")

        try:
            send_telegram_text(
                chat_id,
                "👨‍🍳 <b>Chef_Gordon</b> is watching the video now... Give me a moment, don't rush perfection!",
            )
            send_chat_action(chat_id, "typing")

            download_tiktok_video(tiktok_url, video_path)
            print(f"[TG] 视频下载完成: {tiktok_url}", flush=True)

            recipe = analyze_video_with_gemini(video_path)

            if not recipe.is_cooking_video:
                send_telegram_text(
                    chat_id,
                    "Bloody hell! That doesn't look like a cooking video. Send me a real recipe!",
                )
                return

            if extract_frame(video_path, recipe.final_dish_timestamp_seconds, image_path):
                send_chat_action(chat_id, "upload_photo")
                caption = (
                    f"🍽️ <b>{html.escape(recipe.dish_name)}</b>\n"
                    f"💬 <i>\"{html.escape(recipe.chef_gordon_note)}\"</i>"
                )
                send_telegram_photo(chat_id, image_path, caption=caption)

            send_telegram_text(chat_id, format_telegram_recipe(recipe))
            print("[TG] 菜谱与成品图发送成功!", flush=True)

        except Exception as e:
            print(f"[ERROR] {e}", flush=True)
            try:
                send_telegram_text(
                    chat_id,
                    "Sorry, Chef_Gordon burned the pan trying to read that TikTok link. Make sure the video is public and try again!",
                )
            except Exception as send_err:
                print(f"[SEND ERROR] {send_err}", flush=True)


# --- 主循环（长轮询，无需 ngrok） ---
def main() -> None:
    print("👨‍🍳 Chef_Gordon Telegram Bot 已启动，正在监听消息...", flush=True)
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
                    executor.submit(
                        send_telegram_text,
                        chat_id,
                        "👨‍🍳 Welcome to <b>Chef_Gordon</b>!\n\nShare a TikTok cooking video link with me, and I'll break it down into a structured recipe with a photo of the finished dish.",
                    )

        except KeyboardInterrupt:
            print("\nBot stopped.", flush=True)
            break
        except Exception as e:
            print(f"[POLL LOOP ERROR] {e}", flush=True)
            time.sleep(3)


if __name__ == "__main__":
    main()