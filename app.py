import os
import requests
from flask import Flask, request
import replicate

app = Flask(__name__)

# --- Environment Variables ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Temporary memory storage for voice samples
user_voices = {}

# --- Bottom Menu Keyboard Definition ---
MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "📖 Recite Verse"}, {"text": "🎙️ Clone My Voice"}],
        [{"text": "ℹ️ Help"}]
    ],
    "resize_keyboard": True
}

# --- Helper Functions ---
def send_message(chat_id: int, text: str, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    requests.post(f"{TELEGRAM_API}/sendMessage", json=payload)

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
    formatted_text = f"{text}."
    # Calling "lucataco/xtts-v2" directly automatically uses the latest version
    output = replicate.run(
        "lucataco/xtts-v2",
        input={
            "text": formatted_text,
            "speaker": speaker_audio_url,
            "language": "ar",
            "speed": 0.82
        }
    )
    return output

# --- Webhook Handler ---
@app.route("/", methods=["GET", "POST"])
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return "Bot is running!", 200

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
        send_message(chat_id, start_message, reply_markup=MAIN_KEYBOARD)
        return "OK", 200

    # Handle Menu Button Taps
    if text == "🎙️ Clone My Voice":
        send_message(
            chat_id, 
            "🎙️ How to clone your voice:\n\n"
            "Hold the microphone button below and record a 10–15 second voice message reading any text clearly.",
            reply_markup=MAIN_KEYBOARD
        )
        return "OK", 200

    if text == "📖 Recite Verse":
        send_message(
            chat_id, 
            "📖 To generate a verse, send its Surah and Ayah numbers separated by a colon.\n\n"
            "Examples:\n"
            "• `1:1` (Surah Al-Fatiha, Verse 1)\n"
            "• `2:255` (Ayat al-Kursi)",
            reply_markup=MAIN_KEYBOARD
        )
        return "OK", 200

    if text == "ℹ️ Help":
        send_message(
            chat_id,
            "ℹ️ TilawaAiBot Help:\n\n"
            "1. Record a 10-15s voice note to save your voice.\n"
            "2. Send `Surah:Ayah` (e.g. `112:1`) to generate recitation.\n"
            "3. Send a new voice note anytime to update your voice sample.",
            reply_markup=MAIN_KEYBOARD
        )
        return "OK", 200

    # Save voice note URL
    if "voice" in msg:
        file_id = msg["voice"]["file_id"]
        voice_url = get_telegram_file_url(file_id)
        user_voices[user_id] = voice_url
        send_message(chat_id, "✅ Voice saved successfully! Send a verse like `1:1` or `2:255` to test it.", reply_markup=MAIN_KEYBOARD)
        return "OK", 200

    # Handle verse request (e.g. 1:1 or 2:255)
    if ":" in text:
        try:
            surah, ayah = map(int, text.split(":"))
            user_voice_url = user_voices.get(user_id)

            if not user_voice_url:
                send_message(chat_id, "Please upload a voice note first by tapping '🎙️ Clone My Voice'!", reply_markup=MAIN_KEYBOARD)
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
    
