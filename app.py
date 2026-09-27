import os
import requests
import threading
from datetime import datetime
from flask import Flask, request
import replicate
from upstash_redis import Redis

app = Flask(__name__)

# --- Environment Variables ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# --- Upstash Redis Client ---
# Automatically reads UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN
redis = Redis.from_env()

# --- Limits & Rules ---
MAX_CHARS = 400       # Maximum text length per request to protect API credits
DAILY_LIMIT = 5       # Max voiceover generations per user per day

# --- Bottom Menu Keyboard ---
MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "🎙️ Clone My Voice"}, {"text": "📝 Storytelling Tips"}],
        [{"text": "📊 My Status"}, {"text": "ℹ️ Help"}]
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

# --- Redis Database Storage Helpers ---
def get_user_voice(user_id: int) -> str:
    try:
        val = redis.get(f"voice:{user_id}")
        return str(val) if val else None
    except Exception:
        return None

def save_user_voice(user_id: int, voice_url: str):
    redis.set(f"voice:{user_id}", voice_url)

def check_and_increment_quota(user_id: int) -> tuple:
    """Tracks daily generation quota in Redis. Returns (allowed: bool, count: int)"""
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    key = f"usage:{user_id}:{today_str}"
    
    current_count = redis.get(key)
    current_count = int(current_count) if current_count else 0

    if current_count >= DAILY_LIMIT:
        return False, current_count

    new_count = redis.incr(key)
    if new_count == 1:
        redis.expire(key, 172800)  # Key expires after 48 hours
    return True, new_count

# --- Voice Generation Call ---
def generate_voice_narration(text: str, speaker_audio_url: str) -> str:
    output = replicate.run(
        "lucataco/xtts-v2:684bc3855b37866c0c65add2ff39c78f3dea3f4ff103a436465326e0f438d55e",
        input={
            "text": text,
            "speaker": speaker_audio_url,
            "language": "en",
            "speed": 1.0
        }
    )
    if isinstance(output, list):
        return str(output[0])
    return str(output)

# --- Background Worker Thread ---
def process_narration_task(chat_id: int, text: str, user_voice_url: str):
    try:
        audio_url = generate_voice_narration(text, user_voice_url)
        send_voice(chat_id, audio_url)
    except Exception as e:
        send_message(chat_id, f"⚠️ Error generating audio narration: {str(e)}", reply_markup=MAIN_KEYBOARD)

# --- Webhook Handler ---
@app.route("/", methods=["GET", "POST"])
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return "StoryVoice AI Studio is online!", 200

    data = request.get_json()
    if not data or "message" not in data:
        return "OK", 200

    msg = data["message"]
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    text = msg.get("text", "").strip()

    # --- Start Command ---
    if text == "/start":
        start_message = (
            "Welcome to StoryVoice AI Studio! 🎙️✨\n\n"
            "Transform scripts, stories, and social content into custom voiceovers using your own cloned voice.\n\n"
            "⚡ Quick Start:\n"
            "1️⃣ Tap '🎙️ Clone My Voice' and send a clear 10–15s voice clip.\n"
            "2️⃣ Send any text script or story (up to 400 characters).\n"
            "3️⃣ Receive your studio voiceover in seconds!\n\n"
            "Your cloned voice is stored permanently in our secure database."
        )
        send_message(chat_id, start_message, reply_markup=MAIN_KEYBOARD)
        return "OK", 200

    # --- Bottom Menu Buttons ---
    if text == "🎙️ Clone My Voice":
        send_message(
            chat_id,
            "🎙️ Voice Cloning Instructions:\n\n"
            "1. Find a quiet room with minimal background noise.\n"
            "2. Hold the microphone icon and record a 10–15 second message speaking clearly.\n"
            "3. Send the voice message here to save your voice clone.",
            reply_markup=MAIN_KEYBOARD
        )
        return "OK", 200

    if text == "📝 Storytelling Tips":
        send_message(
            chat_id,
            "📝 AI Voiceover Tips:\n\n"
            "• Use commas for short natural pauses.\n"
            "• Use periods or exclamation marks for ending dramatic sentences.\n"
            "• Keep paragraphs under 400 characters for optimal natural pacing.",
            reply_markup=MAIN_KEYBOARD
        )
        return "OK", 200

    if text == "📊 My Status":
        voice_saved = bool(get_user_voice(user_id))
        voice_status = "✅ Active Voice Clone Found" if voice_saved else "❌ No Voice Saved Yet"
        
        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        current_used = int(redis.get(f"usage:{user_id}:{today_str}") or 0)
        remaining = max(0, DAILY_LIMIT - current_used)

        status_msg = (
            f"📊 Your StoryVoice Profile:\n\n"
            f"Voice Status: {voice_status}\n"
            f"Daily Quota: {remaining}/{DAILY_LIMIT} generations remaining today"
        )
        send_message(chat_id, status_msg, reply_markup=MAIN_KEYBOARD)
        return "OK", 200

    if text == "ℹ️ Help":
        send_message(
            chat_id,
            "ℹ️ How to use StoryVoice AI:\n\n"
            "1. Record a voice note to save your voice profile permanently.\n"
            "2. Send any text message (up to 400 characters) to convert to speech.\n"
            "3. You can record a new voice note anytime to update your voice profile.",
            reply_markup=MAIN_KEYBOARD
        )
        return "OK", 200

    # --- Save Voice Note Upload ---
    if "voice" in msg:
        file_id = msg["voice"]["file_id"]
        voice_url = get_telegram_file_url(file_id)
        save_user_voice(user_id, voice_url)
        send_message(
            chat_id,
            "✅ Voice profile saved permanently! Send any story or text message now to generate a custom narration.",
            reply_markup=MAIN_KEYBOARD
        )
        return "OK", 200

    # --- Process Plain Text Script ---
    if text and not text.startswith("/"):
        # Check text length limit
        if len(text) > MAX_CHARS:
            send_message(
                chat_id,
                f"⚠️ Message too long ({len(text)} chars). Please keep scripts under {MAX_CHARS} characters for optimal quality.",
                reply_markup=MAIN_KEYBOARD
            )
            return "OK", 200

        # Retrieve stored voice profile
        user_voice_url = get_user_voice(user_id)
        if not user_voice_url:
            send_message(
                chat_id,
                "Please record and upload a voice note first by tapping '🎙️ Clone My Voice'!",
                reply_markup=MAIN_KEYBOARD
            )
            return "OK", 200

        # Check daily quota
        allowed, used_count = check_and_increment_quota(user_id)
        if not allowed:
            send_message(
                chat_id,
                f"⚠️ You've reached your daily limit of {DAILY_LIMIT} voiceovers. Please try again tomorrow!",
                reply_markup=MAIN_KEYBOARD
            )
            return "OK", 200

        send_message(chat_id, f"🎙️ Generating voiceover ({used_count}/{DAILY_LIMIT} used today)...", reply_markup=MAIN_KEYBOARD)

        # Launch generation in background thread
        threading.Thread(
            target=process_narration_task,
            args=(chat_id, text, user_voice_url)
        ).start()

        return "OK", 200

    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
    
