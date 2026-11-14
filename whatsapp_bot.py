import os
import re
import time
import uuid
import tempfile
from pathlib import Path
from typing import List, Optional

import httpx
import yt_dlp
from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks, Form, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from twilio.twiml.messaging_response import MessagingResponse
from google import genai
from google.genai import types

from cooking.chat import FAILED, NOT_A_RECIPE, WELCOME, WORKING, recipe_messages
from cooking.media import open_video

load_dotenv()

# --- 配置 ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

# 可用模型列表：优先用第一个，失败（如额度耗尽）自动切换下一个
CANDIDATE_MODELS = [
    "gemini-4-hydrogen",
    # 如果你在 AI Studio 查到了其他可用备用模型，可以加在下面，例如：
    # "gemini-3.8-flash-lite",
]

STATIC_DIR = Path("static")
STATIC_DIR.mkdir(exist_ok=True)

app = FastAPI(title="Cooking assistant WhatsApp bot")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

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
    final_dish_timestamp_seconds: float = Field(
        description="The exact timestamp (in seconds) in the video that gives the clearest, most appetizing shot of the finished plated dish."
    )


# --- 辅助函数 ---
SECONDS_BETWEEN_MESSAGES = 1.0   # Twilio does not guarantee order; a pause keeps the messages in sequence


def send_whatsapp_messages(
    from_number: str,
    to_number: str,
    messages: List[str],
    media_url: Optional[str] = None,
) -> None:
    """Send `messages` in order; the photo, if any, goes with the first."""
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        raise RuntimeError("Missing TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN in .env")

    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"

    for idx, message in enumerate(messages):
        if idx:
            time.sleep(SECONDS_BETWEEN_MESSAGES)
        data = {
            "From": from_number,
            "To": to_number,
            "Body": message,
        }
        if idx == 0 and media_url:
            data["MediaUrl"] = media_url

        print(f"[Twilio SEND] From={from_number} To={to_number} Media={data.get('MediaUrl')}", flush=True)
        resp = httpx.post(
            url,
            data=data,
            auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            timeout=30.0,
        )
        print(f"[Twilio RESP] status={resp.status_code} body={resp.text}", flush=True)
        if resp.status_code >= 400:
            raise RuntimeError(f"Twilio API error {resp.status_code}: {resp.text}")
        
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
    print("[Gemini] 开始上传视频文件...", flush=True)
    uploaded_file = gemini_client.files.upload(file=video_path)
    print(f"[Gemini] 上传完成: {uploaded_file.name}, state={uploaded_file.state}", flush=True)

    try:
        while str(uploaded_file.state).endswith("PROCESSING"):
            print("[Gemini] 视频转码中 (PROCESSING)，等待 2 秒...", flush=True)
            time.sleep(2)
            uploaded_file = gemini_client.files.get(name=uploaded_file.name)

        if str(uploaded_file.state).endswith("FAILED"):
            raise RuntimeError("Gemini failed to process the uploaded video.")

        print(f"[Gemini] 视频就绪 (state={uploaded_file.state})，开始调用模型...", flush=True)

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
                print(f"[Gemini] 正在尝试模型: {model_name}...", flush=True)
                response = gemini_client.models.generate_content(
                    model=model_name,
                    contents=[uploaded_file, prompt],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=RecipeOutput,
                        temperature=0.3,
                    ),
                )
                print(f"[Gemini] 模型 {model_name} 调用成功!", flush=True)
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


def process_tiktok_request(sender: str, bot_number: str, tiktok_url: str) -> None:
    image_filename = f"dish_{uuid.uuid4().hex}.jpg"
    image_path = str(STATIC_DIR / image_filename)
    image_url: Optional[str] = None

    with tempfile.TemporaryDirectory() as tmpdir:
        video_path = os.path.join(tmpdir, "tiktok_video.mp4")

        try:
            # 1. 下载视频
            download_tiktok_video(tiktok_url, video_path)
            # 在 download_tiktok_video 后面加：
            print("[BG] 1. 下载完成，开始上传给 Gemini...", flush=True)
            
            # 2. Gemini 分析视频 (Cloudinary shrinks the video first and takes the dish photo; local fallback)
            with open_video(video_path) as video:
                recipe = analyze_video_with_gemini(video.for_model(tmpdir))
                print(f"[BG] 2. Gemini 分析完成: is_cooking={recipe.is_cooking_video}", flush=True)
                if not recipe.is_cooking_video:
                    send_whatsapp_messages(bot_number, sender, [NOT_A_RECIPE])
                    return

                # 3. 提取成品图
                if PUBLIC_BASE_URL and video.frame(recipe.final_dish_timestamp_seconds, image_path):
                    image_url = f"{PUBLIC_BASE_URL}/static/{image_filename}"

            # 4. 发回菜谱 + 成品图: three messages, the photo with the first
            messages = recipe_messages(
                recipe.dish_name, recipe.prep_time, recipe.cook_time, recipe.servings,
                recipe.ingredients, recipe.steps,
            )
            send_whatsapp_messages(bot_number, sender, messages, media_url=image_url)

        except Exception as e:
            print(f"Error processing video: {e}")
            try:
                send_whatsapp_messages(bot_number, sender, [FAILED])
            except Exception as send_err:
                print(f"Failed to send error notification to WhatsApp: {send_err}")


# --- WhatsApp Webhook ---
@app.post("/webhook")
async def whatsapp_webhook(
    background_tasks: BackgroundTasks,
    Body: str = Form(""),
    From: str = Form(...),
    To: str = Form(...),
):
    print(f"[WEBHOOK] From={From} | To={To} | Body={repr(Body)}", flush=True)
    twiml = MessagingResponse()
    match = TIKTOK_URL_REGEX.search(Body)

    if not match:
        twiml.message(WELCOME)
        return Response(content=str(twiml), media_type="application/xml")

    tiktok_url = match.group(0)
    background_tasks.add_task(process_tiktok_request, From, To, tiktok_url)

    twiml.message(WORKING)
    return Response(content=str(twiml), media_type="application/xml")