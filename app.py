import os
import requests
from flask import Flask, request
import replicate

app = Flask(__name__)

# --- Environment Variables (Only BOT_TOKEN & REPLICATE_API_TOKEN required) ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Temporary memory storage for voice samples
user_voices = {}

# --- Helper Functions ---
def send_message(chat_id: int, text: str):
    requests.post(f"{TELEGRAM_API}/sendMessage", json={"chat_id": chat_id, "text": text})

def send_voice(chat_id: int, voice_url: str):
    requests.post(f"{TELEGRAM_API}/sendVoice", json={"chat_id": chat_id, "voice": voice_url})

def get_telegram_file_url(file_id: str) -> str:
    res = requests.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}).json()
    file_path = res.get("result", {}).get("file_path")
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"

def fetch_quran_text(surah: int, ayah: int) -> str:
    url = f"http://api.alquran.cloud/v1/ayah/{surah}:{ayah}/quran-uthmani"
    res = requests.get(url).json()
    if res.get("code") == 200:
        return res["data"]["text"]
    return None

def generate_voice_clone(text: str, speaker_audio_url: str) -> str:
    # Trailing punctuation creates natural breathing pauses
    formatted_text = f"{text}."
    output = replicate.run(
        "lucataco/xtts-v2:682050901e3895e3489b03f0b2f5b82e21e64903e62f025e1d528b9f5d134d1b",
        input={
            "text": formatted_text,
            "speaker": speaker_audio_url,
            "language": "ar",
            "speed": 0.82  # Slower pace for reciter cadence
        }
    )
    return output

# --- Webhook Handler ---
@app.route("/", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data or "message" not in data:
        return "OK", 200

    msg = data["message"]
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    text = msg.get("text", "").strip()

    # Handle /start Command
    if text == "/start":
        start_message = (
            "Assalamu Alaikum! Welcome to the TilawaAiBot🌙\n\n"
            "Experience your voice applied to authentic Qur'an recitations with proper Tajweed rules.\n\n"
            "⚠️ Disclaimer: This is an AI audio visualization and educational tool. "
            "It is meant for listening and learning purposes only and does not replace actual physical Qur'an recitation.\n\n"
            "How to get started:\n"
            "• Send a clear 10–15 second voice recording.\n"
            "• Select the verse you want to hear (e.g., 1:1 or 2:255).\n"
            "• Receive your custom audio clip!\n\n"
            "Upload your voice sample anytime below to begin."
        )
        send_message(chat_id, start_message)
        return "OK", 200

    # Save voice note URL in temporary memory
    if "voice" in msg:
        file_id = msg["voice"]["file_id"]
        voice_url = get_telegram_file_url(file_id)
        user_voices[user_id] = voice_url
        send_message(chat_id, "Voice saved! Send a verse like `1:1` to hear it recited.")
        return "OK", 200

    # Handle verse request (e.g. 1:1 or 2:255)
    if ":" in text:
        try:
            surah, ayah = map(int, text.split(":"))
            user_voice_url = user_voices.get(user_id)

            if not user_voice_url:
                send_message(chat_id, "Please upload a voice note first so I can clone your voice!")
                return "OK", 200

            send_message(chat_id, f"Generating recitation for {surah}:{ayah}...")
            quran_text = fetch_quran_text(surah, ayah)

            if not quran_text:
                send_message(chat_id, "Verse not found. Please check Surah and Ayah numbers.")
                return "OK", 200

            audio_url = generate_voice_clone(quran_text, user_voice_url)
            send_voice(chat_id, audio_url)

        except Exception as e:
            send_message(chat_id, f"Error generating audio: {str(e)}")

        return "OK", 200

    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
    
