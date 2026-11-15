# The glasses server and the Telegram bot in one image. They run in one container because the bot
# writes the recipes it reads to saved_recipes/ and the server offers them from the same folder.
#
#   docker build -t cooking .
#   docker run -d --name cooking --restart unless-stopped --env-file .env \
#     -p 8765:8765 -v cooking-saved:/app/saved_recipes cooking
#
# Keys come from the environment (GEMINI_API_KEY; optional TELEGRAM_BOT_TOKEN);
# none are copied into the image.
FROM python:3.13-slim

# ffmpeg: the bot cuts the videos it downloads.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x scripts/start.sh

ENV PYTHONUNBUFFERED=1
EXPOSE 8765
VOLUME /app/saved_recipes
CMD ["scripts/start.sh"]
