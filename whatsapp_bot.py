import os
import re
import time
import uuid
import tempfile
import subprocess
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

app = FastAPI(title="Chef_Gordon WhatsApp Bot")
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
    chef_gordon_note: str = Field(
        description="A witty, passionate 1-2 sentence remark in the style of Chef Gordon Ramsay about the dish."
    )
    final_dish_timestamp_seconds: float = Field(
        description="The exact timestamp (in seconds) in the video that gives the clearest, most appetizing shot of the finished plated dish."
    )


# --- 辅助函数 ---
def split_message(text: str, max_len: int = 1550) -> List[str]:
    """避免单条消息超过 Twilio WhatsApp 的 1600 字符限制"""
    if len(text) <= max_len:
        return [text]

    chunks: List[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > max_len:
            if current:
                chunks.append(current.rstrip())
                current = ""
            # 万一单行就超长，直接硬切
            while len(line) > max_len:
                chunks.append(line[:max_len])
                line = line[max_len:]
        current += line
    if current:
        chunks.append(current.rstrip())
    return chunks


def send_whatsapp_message(
    from_number: str,
    to_number: str,
    body: str,
    media_url: Optional[str] = None,
) -> None:
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        raise RuntimeError("Missing TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN in .env")

    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    chunks = split_message(body)

    for idx, chunk in enumerate(chunks):
        data = {
            "From": from_number,
            "To": to_number,
            "Body": chunk,
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


def format_whatsapp_recipe(recipe: RecipeOutput) -> str:
    ingredients_text = "\n".join(f"• {item}" for item in recipe.ingredients)
    steps_text = "\n".join(f"{i}. {step}" for i, step in enumerate(recipe.steps, 1))

    return (
        f"👨‍🍳 *Chef_Gordon's Kitchen* 👨‍🍳\n\n"
        f"🍽️ *{recipe.dish_name}*\n"
        f"⏱️ Prep: {recipe.prep_time} | Cook: {recipe.cook_time} | Servings: {recipe.servings}\n\n"
        f"💬 _\"{recipe.chef_gordon_note}\"_\n\n"
        f"🛒 *Ingredients:*\n{ingredients_text}\n\n"
        f"🔥 *Instructions:*\n{steps_text}"
    )


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
            
            # 2. Gemini 分析视频
            recipe = analyze_video_with_gemini(video_path)
            # 在 analyze_video_with_gemini 后面加：
            print(f"[BG] 2. Gemini 分析完成: is_cooking={recipe.is_cooking_video}", flush=True)
            if not recipe.is_cooking_video:
                send_whatsapp_message(
                    bot_number,
                    sender,
                    "Bloody hell! That doesn't look like a cooking video. Send me a real recipe!",
                )
                return

            # 3. 提取成品图
            if PUBLIC_BASE_URL and extract_frame(
                video_path, recipe.final_dish_timestamp_seconds, image_path
            ):
                image_url = f"{PUBLIC_BASE_URL}/static/{image_filename}"

            # 4. 发回菜谱 + 成品图
            message_body = format_whatsapp_recipe(recipe)
            send_whatsapp_message(
                bot_number,
                sender,
                message_body,
                media_url=image_url,
            )

        except Exception as e:
            print(f"Error processing video: {e}")
            try:
                send_whatsapp_message(
                    bot_number,
                    sender,
                    "Sorry, Chef_Gordon burned the pan trying to read that TikTok link. Make sure the video is public and try again!",
                )
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
        twiml.message(
            "👨‍🍳 Welcome to *Chef_Gordon*!\n\n"
            "Share a TikTok cooking video link with me, and I'll break it down into "
            "a structured recipe with a photo of the finished dish."
        )
        return Response(content=str(twiml), media_type="application/xml")

    tiktok_url = match.group(0)
    background_tasks.add_task(process_tiktok_request, From, To, tiktok_url)

    twiml.message(
        "👨‍🍳 *Chef_Gordon* is watching the video now... Give me a moment, don't rush perfection!"
    )
    return Response(content=str(twiml), media_type="application/xml")