#!/bin/sh
# Container entry point: the glasses server, which also answers the Telegram bot's messages when it has
# a token and a public address (PUBLIC_URL, or Fly.io's own): Telegram posts them to it.
# Without a public address the bot polls Telegram instead, as a second process. When either one stops,
# the other is stopped too and the container exits, so the restart policy brings both back together.
cd "$(dirname "$0")/.." || exit 1

python -m scripts.serve_glasses --port "${PORT:-8765}" &
pids=$!

if [ -z "$TELEGRAM_BOT_TOKEN" ]; then
  echo "TELEGRAM_BOT_TOKEN is not set: the Telegram bot is not started." >&2
elif [ -z "$PUBLIC_URL" ] && [ -z "$FLY_APP_NAME" ]; then
  python Telegram_bot.py &
  pids="$pids $!"
fi

trap 'kill $pids 2>/dev/null' TERM INT

# Poll: plain sh has no `wait -n`.
while :; do
  for pid in $pids; do
    if ! kill -0 "$pid" 2>/dev/null; then
      wait "$pid"; code=$?
      kill $pids 2>/dev/null
      exit "$code"
    fi
  done
  sleep 2
done
