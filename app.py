import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
FISH_AUDIO_API_KEY = os.environ.get("FISH_AUDIO_API_KEY")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Memory storage to keep track of user custom voice IDs (per chat)
USER_VOICES = {}
USER_STATES = {}  # Tracks if user is currently setting a voice ID

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
                [{"text": "📖 Recite Verse"}, {"text": "🎙️ Set Custom Voice"}],
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

    chat_id = data["message"]["chat"]["id"]
    text = data["message"].get("text", "").strip()

    # 1. Handle /start command or Main Menu navigation
    if text in ["/start", "main menu"]:
        USER_STATES[chat_id] = None
        welcome_msg = (
            "Assalamu Alaikum! 🌙\n\n"
            "Welcome to the **Quran Recitation & Voice Cloning Bot**.\n\n"
            "Select an option below or directly send a verse number in `Surah:Ayah` format (e.g., `1:1` or `2:255`)."
        )
        send_message(chat_id, welcome_msg, show_keyboard=True)
        return jsonify({"status": "ok"}), 200

    # 2. Handle Button Menu Clicks
    if text == "📖 Recite Verse":
        send_message(
            chat_id, 
            "Type and send any Surah and Ayah number like this:\n\n"
            "`1:1` (Surah Al-Fatiha, Ayah 1)\n"
            "`2:255` (Ayat al-Kursi)"
        )
        return jsonify({"status": "ok"}), 200

    if text == "🎙️ Set Custom Voice":
        USER_STATES[chat_id] = "awaiting_voice_id"
        current_voice = USER_VOICES.get(chat_id, "Default Voice")
        msg = (
            f"**Current Voice Setting:** `{current_voice}`\n\n"
            "To use your cloned voice or a custom model from Fish Audio:\n"
            "1. Go to [Fish Audio](https://fish.audio) and copy your Model/Voice ID.\n"
            "2. Send the **Voice Model ID** as a reply to this message.\n\n"
            "*(Send `default` anytime to reset to the standard voice).* "
        )
        send_message(chat_id, msg)
        return jsonify({"status": "ok"}), 200

    if text == "ℹ️ Help":
        help_msg = (
            "📌 **Bot Commands & Usage:**\n\n"
            "• **Get Recitation:** Send `Surah:Ayah` (e.g. `112:1`).\n"
            "• **Voice Cloning:** Tap `🎙️ Set Custom Voice` to use your cloned Fish Audio voice.\n"
            "• **Format:** Arabic Uthmani text rendered directly into spoken audio."
        )
        send_message(chat_id, help_msg, show_keyboard=True)
        return jsonify({"status": "ok"}), 200

    # 3. Handle Voice ID Input State
    if USER_STATES.get(chat_id) == "awaiting_voice_id":
        USER_STATES[chat_id] = None
        if text.lower() == "default":
            USER_VOICES.pop(chat_id, None)
            send_message(chat_id, "✅ Voice reset to default standard model.", show_keyboard=True)
        else:
            USER_VOICES[chat_id] = text
            send_message(chat_id, f"✅ Voice Model set to: `{text}`!\nFuture recitations will use this voice model.", show_keyboard=True)
        return jsonify({"status": "ok"}), 200

    # 4. Handle Surah:Ayah Verse Generation Requests
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

            # Attach custom reference/voice ID if user set one
            if chat_id in USER_VOICES:
                fish_payload["reference_id"] = USER_VOICES[chat_id]

            fish_res = requests.post("https://api.fish.audio/v1/tts", json=fish_payload, headers=fish_headers)
            
            if fish_res.status_code != 200:
                send_message(
                    chat_id, 
                    f"⚠️ Voice generation failed (Fish Audio Status Code: {fish_res.status_code}). "
                    "If you set a custom Voice ID, verify that it is valid."
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
    
