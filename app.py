import os
import requests
from flask import Flask, request
from telegram import Bot, Update

app = Flask(__name__)

TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
FISH_AUDIO_API_KEY = os.getenv("FISH_AUDIO_API_KEY")
WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL")

bot = Bot(token=TELEGRAM_TOKEN)
user_voices = {}

def get_quran_text(surah, ayah):
    url = f"http://api.alquran.cloud/v1/ayah/{surah}:{ayah}/quran-uthmani"
    res = requests.get(url).json()
    if res.get("code") == 200:
        return res["data"]["text"]
    return None

def clone_and_synthesize(user_id, text):
    reference_id = user_voices.get(user_id)
    if not reference_id:
        return None

    url = "https://api.fish.audio/v1/tts"
    headers = {
        "Authorization": f"Bearer {FISH_AUDIO_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "text": text,
        "reference_id": reference_id,
        "format": "mp3"
    }
    
    response = requests.post(url, json=payload, headers=headers)
    if response.status_code == 200:
        return response.content
    return None

@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), bot)
    
    if update.message:
        user_id = update.message.from_user.id
        
        if update.message.voice:
            user_voices[user_id] = "DEFAULT_VOICE_ID"
            update.message.reply_text("Voice received! Send a verse in 'Surah:Ayah' format (e.g. 1:1) to hear recite.")

        elif update.message.text and ":" in update.message.text:
            try:
                surah, ayah = update.message.text.split(":")
                arabic_text = get_quran_text(surah, ayah)
                
                if arabic_text:
                    bot.send_message(chat_id=user_id, text=f"Reciting: {arabic_text}")
                    audio_data = clone_and_synthesize(user_id, arabic_text)
                    
                    if audio_data:
                        bot.send_voice(chat_id=user_id, voice=audio_data)
                    else:
                        bot.send_message(chat_id=user_id, text="Please record and send a 15-second voice note first.")
            except Exception:
                bot.send_message(chat_id=user_id, text="Please send in 'Surah:Ayah' format (e.g. 2:255).")

    return "OK", 200

@app.route("/set_webhook", methods=["GET", "POST"])
def set_webhook():
    s = bot.setWebhook(f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}")
    return "Webhook setup successfully!" if s else "Webhook setup failed."

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
