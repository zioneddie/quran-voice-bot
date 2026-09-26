import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
FISH_AUDIO_API_KEY = os.environ.get("FISH_AUDIO_API_KEY")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Memory storage to keep track of user custom voice IDs (per chat)
USER_VOICES = {}

def send_message(chat_id, text, show_keyboard=False):
    """Sends a text message to Telegram, optionally including main menu buttons."""
    url = f"{TELEGRAM_API_URL}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }

    if show_keyboard:
        payload["reply_markup"] = {
            "keyboard": [
                [{"text": "📖 Recite Verse"}, {"text": "🎙️ Clone My Voice"}],
                [{"text": "ℹ️ Help"}]
            ],
            "resize_keyboard": True
        }

    requests.post(url, json=payload)

def send_audio(chat_id, audio_bytes):
    """Sends an audio voice note back to the Telegram user."""
    url = f"{TELEGRAM_API_URL}/sendVoice"
    files = {"voice": ("recitation.mp3", audio_bytes, "audio/mpeg")}
    payload = {"chat_id": chat_id}
    requests.post(url, data=payload, files=files)

def create_fish_voice_model(voice_bytes, chat_id):
    """Uploads recorded voice audio to Fish Audio to create a voice model."""
    url = "https://api.fish.audio/v1/model"
    headers = {
        "Authorization": f"Bearer {FISH_AUDIO_API_KEY}"
    }
    data = {
        "title": f"User_{chat_id}_Voice",
        "type": "tts",
        "train_mode": "fast"
    }
    files = {
        "voices": ("recording.ogg", voice_bytes, "audio/ogg")
    }
    
    res = requests.post(url, headers=headers, data=data, files=files)
    if res.status_code in [200, 201]:
        return res.json().get("_id") or res.json().get("id")
    return None

@app.route("/", methods=["GET"])
def home():
    return "Quran Voice Bot is running!", 200

@app.route("/set_webhook", methods=["GET"])
def set_webhook():
    webhook_url = f"https://{request.host}/webhook"
    url = f"{TELEGRAM_API_URL}/setWebhook?url={webhook_url}"
    res = requests.get(url).json()
    if res.get("ok"):
        return "Webhook setup successfully!", 200
    return f"Webhook setup failed: {res}", 400

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    
    if not data or "message" not in data:
        return jsonify({"status": "ignored"}), 200

    message = data["message"]
    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()

    # 1. Handle Voice Messages (In-Telegram Voice Cloning)
    if "voice" in message:
        send_message(chat_id, "🎙️ Processing your voice recording and creating your custom AI voice model... Please wait a moment.")
        
        # Get voice file path from Telegram
        file_id = message["voice"]["file_id"]
        file_info = requests.get(f"{TELEGRAM_API_URL}/getFile?file_id={file_id}").json()
        
        if file_info.get("ok"):
            file_path = file_info["result"]["file_path"]
            voice_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
            voice_bytes = requests.get(voice_url).content

            # Create model on Fish Audio
            model_id = create_fish_voice_model(voice_bytes, chat_id)
            if model_id:
                USER_VOICES[chat_id] = model_id
                send_message(
                    chat_id, 
                    "✅ **Voice successfully cloned!**\n\n"
                    "All future Quran recitations will now be generated in your cloned voice! "
                    "Send a verse like `1:1` or `2:255` to test it.", 
                    show_keyboard=True
                )
            else:
                send_message(
                    chat_id, 
                    "⚠️ Voice cloning failed on Fish Audio. Make sure your account has active credits and try recording a clearer 15-30 second audio clip.",
                    show_keyboard=True
                )
        return jsonify({"status": "ok"}), 200

    # 2. Handle /start command or Main Menu navigation
    if text in ["/start", "main menu"]:
        welcome_msg = (
            "Assalamu Alaikum! 🌙\n\n"
            "Welcome to the **Quran Recitation & Voice Cloning Bot**.\n\n"
            "• **To clone your voice:** Hold the microphone icon in Telegram and record a clear 15–30 second voice message.\n"
            "• **To get recitation:** Send a verse number in `Surah:Ayah` format (e.g., `1:1` or `2:255`)."
        )
        send_message(chat_id, welcome_msg, show_keyboard=True)
        return jsonify({"status": "ok"}), 200

    # 3. Handle Button Menu Clicks
    if text == "📖 Recite Verse":
        send_message(
            chat_id, 
            "Type and send any Surah and Ayah number like this:\n\n"
            "`1:1` (Surah Al-Fatiha, Ayah 1)\n"
            "`2:255` (Ayat al-Kursi)"
        )
        return jsonify({"status": "ok"}), 200

    if text == "🎙️ Clone My Voice":
        current_voice = "Custom Cloned Voice" if chat_id in USER_VOICES else "Default Standard Voice"
        msg = (
            f"**Current Active Voice:** `{current_voice}`\n\n"
            "🎙️ **How to clone your voice:**\n"
            "Simply hold the microphone button below and send me a **15–30 second voice recording** reading any text clearly.\n\n"
            "*(Send `default` anytime to reset back to the standard voice).* "
        )
        send_message(chat_id, msg)
        return jsonify({"status": "ok"}), 200

    if text == "ℹ️ Help":
        help_msg = (
            "📌 **Bot Commands & Usage:**\n\n"
            "• **Get Recitation:** Send `Surah:Ayah` (e.g. `112:1`).\n"
            "• **Clone Voice:** Record a voice note directly in Telegram!\n"
            "• **Reset Voice:** Send `default` to return to the standard reciter."
        )
        send_message(chat_id, help_msg, show_keyboard=True)
        return jsonify({"status": "ok"}), 200

    # 4. Handle Voice Reset
    if text.lower() == "default":
        USER_VOICES.pop(chat_id, None)
        send_message(chat_id, "✅ Voice reset back to default standard model.", show_keyboard=True)
        return jsonify({"status": "ok"}), 200

    # 5. Handle Surah:Ayah Verse Generation Requests
    if ":" in text:
        try:
            surah, ayah = text.split(":")
            surah = surah.strip()
            ayah = ayah.strip()

            send_message(chat_id, f"⏳ Fetching verse {surah}:{ayah} and generating voice recitation...")

            # Fetch verse text from Al Qur'an Cloud API
            quran_res = requests.get(f"https://api.alquran.cloud/v1/ayah/{surah}:{ayah}/quran-uthmani").json()
            if quran_res.get("code") != 200:
                send_message(chat_id, f"⚠️ Couldn't find verse {surah}:{ayah}. Check the numbers and try again.")
                return jsonify({"status": "not_found"}), 200

            ayah_text = quran_res["data"]["text"]

            # Convert text to speech using Fish Audio API
            fish_headers = {
                "Authorization": f"Bearer {FISH_AUDIO_API_KEY}",
                "Content-Type": "application/json"
            }
            
            fish_payload = {
                "text": ayah_text,
                "format": "mp3"
            }

            # Use custom voice model ID if user recorded one
            if chat_id in USER_VOICES:
                fish_payload["reference_id"] = USER_VOICES[chat_id]

            fish_res = requests.post("https://api.fish.audio/v1/tts", json=fish_payload, headers=fish_headers)
            
            if fish_res.status_code != 200:
                send_message(
                    chat_id, 
                    f"⚠️ Voice generation failed (Fish Audio Status Code: {fish_res.status_code}). "
                    "Please verify that your developer Fish Audio account has active credits."
                )
                return jsonify({"status": "fish_audio_error"}), 200

            # Send audio back to user
            send_audio(chat_id, fish_res.content)

        except Exception as e:
            send_message(chat_id, f"⚠️ An error occurred: {str(e)}")
            return jsonify({"status": "error"}), 200

    else:
        send_message(chat_id, "Please select an option from the menu or send a verse like `1:1` or `2:255`.", show_keyboard=True)

    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
    
