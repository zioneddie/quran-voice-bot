import os
import requests
import replicate
from flask import Flask, request, jsonify

app = Flask(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Memory storage to keep track of user custom voice file URLs
USER_VOICE_URLS = {}

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

@app.route("/", methods=["GET"])
def home():
    return "Quran Voice Bot (Replicate Edition) is running!", 200

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

    # 1. Handle Voice Messages (In-Telegram Voice Cloning via Replicate)
    if "voice" in message:
        send_message(chat_id, "🎙️ Saving your voice recording for AI cloning...")
        
        file_id = message["voice"]["file_id"]
        file_info = requests.get(f"{TELEGRAM_API_URL}/getFile?file_id={file_id}").json()
        
        if file_info.get("ok"):
            file_path = file_info["result"]["file_path"]
            voice_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"

            # Save direct audio URL for Replicate XTTS model
            USER_VOICE_URLS[chat_id] = voice_url
            send_message(
                chat_id, 
                "✅ **Voice saved successfully!**\n\n"
                "All future Quran recitations will now be generated using your cloned voice! "
                "Send a verse like `1:1` or `2:255` to test it.", 
                show_keyboard=True
            )
        else:
            send_message(chat_id, "⚠️ Failed to process voice message. Please try recording again.", show_keyboard=True)
        return jsonify({"status": "ok"}), 200

    # 2. Handle /start command or Main Menu navigation
    if text in ["/start", "main menu"]:
        welcome_msg = (
            "Assalamu Alaikum! 🌙\n\n"
            "Welcome to the **Quran Voice Bot**.\n\n"
            "• **To clone your voice:** Hold the microphone button in Telegram and send a clear 15–30 second voice recording.\n"
            "• **To get recitation:** Send a verse number in `Surah:Ayah` format (e.g., `1:1` or `2:255`)."
        )
        send_message(chat_id, welcome_msg, show_keyboard=True)
        return jsonify({"status": "ok"}), 200

    # 3. Handle Menu Buttons
    if text == "📖 Recite Verse":
        send_message(
            chat_id, 
            "Type and send any Surah and Ayah number like this:\n\n"
            "`1:1` (Surah Al-Fatiha, Ayah 1)\n"
            "`2:255` (Ayat al-Kursi)"
        )
        return jsonify({"status": "ok"}), 200

    if text == "🎙️ Clone My Voice":
        current_voice = "Custom Voice Active" if chat_id in USER_VOICE_URLS else "Standard Voice"
        msg = (
            f"**Current Voice Status:** `{current_voice}`\n\n"
            "🎙️ **How to clone your voice:**\n"
            "Hold the microphone button below and record a **15–30 second voice message** reading any text clearly.\n\n"
            "*(Send `default` anytime to reset back to standard reciter).* "
        )
        send_message(chat_id, msg)
        return jsonify({"status": "ok"}), 200

    if text == "ℹ️ Help":
        help_msg = (
            "📌 **Bot Commands & Usage:**\n\n"
            "• **Get Recitation:** Send `Surah:Ayah` (e.g. `112:1`).\n"
            "• **Clone Voice:** Record a voice note directly in Telegram!\n"
            "• **Reset Voice:** Send `default` to clear your voice clone."
        )
        send_message(chat_id, help_msg, show_keyboard=True)
        return jsonify({"status": "ok"}), 200

    # 4. Handle Voice Reset
    if text.lower() == "default":
        USER_VOICE_URLS.pop(chat_id, None)
        send_message(chat_id, "✅ Voice reset back to standard AI reciter.", show_keyboard=True)
        return jsonify({"status": "ok"}), 200

    # 5. Handle Surah:Ayah Verse Generation Requests
    if ":" in text:
        try:
            surah, ayah = text.split(":")
            surah = surah.strip()
            ayah = ayah.strip()

            send_message(chat_id, f"⏳ Fetching verse {surah}:{ayah} and generating recitation...")

            # Fetch verse text from Al Qur'an Cloud API
            quran_res = requests.get(f"https://api.alquran.cloud/v1/ayah/{surah}:{ayah}/quran-uthmani").json()
            if quran_res.get("code") != 200:
                send_message(chat_id, f"⚠️ Couldn't find verse {surah}:{ayah}. Check the numbers and try again.")
                return jsonify({"status": "not_found"}), 200

            ayah_text = quran_res["data"]["text"]
            speaker_url = USER_VOICE_URLS.get(chat_id)

            # Build Replicate XTTS input payload
            input_data = {
                "text": ayah_text,
                "language": "ar"
            }
            if speaker_url:
                input_data["speaker"] = speaker_url

            # Run XTTS-v2 via Replicate API
            output = replicate.run(
                "lucataco/xtts-v2:684bc3855b37866c0c65add2ff39c78f3dea3f4ff103a436465326e0f438d55e",
                input=input_data
            )

            # Download generated audio file from Replicate output URL
            audio_url = str(output)
            audio_bytes = requests.get(audio_url).content

            send_audio(chat_id, audio_bytes)

        except Exception as e:
            send_message(chat_id, f"⚠️ Voice generation failed: {str(e)}")
            return jsonify({"status": "error"}), 200

    else:
        send_message(chat_id, "Please select an option from the menu or send a verse like `1:1` or `2:255`.", show_keyboard=True)

    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
    
