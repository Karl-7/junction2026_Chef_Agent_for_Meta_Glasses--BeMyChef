to activate:
run 
ngrok http 8000 --url https://getaway-capacity-disown.ngrok-free.dev
uvicorn whatsapp_bot:app --host 0.0.0.0 --port 8000 --reload
.venv/bin/python -m scripts.serve_glasses