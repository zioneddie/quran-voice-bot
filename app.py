import os
import re
import io
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# --- CONFIGURATION FROM RENDER ENVIRONMENT ---
ADMIN_ID = os.getenv("ADMIN_ID", "")
FISH_AUDIO_API_KEY = os.getenv("FISH_AUDIO_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


# --- UPSTASH REDIS HELPERS (REST API) ---
def redis_command(command: list):
    """Executes Redis commands via Upstash REST API."""
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return None
    
    headers = {"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"}
    try:
        response = requests.post(UPSTASH_REDIS_REST_URL, json=command, headers=headers)
        res_data = response.json()
        return res_data.get("result")
    except Exception as e:
        print(f"Upstash Redis Error: {e}")
        return None

def get_user_model(user_id: int) -> str:
    return redis_command(["GET", f"user:{user_id}:model_id"])

def set_user_model(user_id: int, model_id: str):
    redis_command(["SET", f"user:{user_id}:model_id", model_id])


# --- ENHANCED TEXT PREPROCESSING FOR NATURAL FISH AUDIO EXPRESSION ---
def prepare_text_for_tts(text: str) -> str:
    """
    Formats raw text scripts to force human expression, pitch variation,
    and dramatic pauses specifically for Fish Audio synthesis.
    """
    if not text:
        return ""

    # 1. Standardize whitespace
    text = re.sub(r'\s+', ' ', text.strip())

    # 2. Add realistic breath/thought pauses after openings/greetings
    text = re.sub(
        r'^(hello|hi|hey|welcome|listen|look|so|well)([\s!,.?])',
        r'\1... ',
        text,
        flags=re.IGNORECASE
    )

    # 3. Force micro-pauses before connecting words to create cadence
    text = re.sub(
        r'[,;]?\s+(but|however|so|because|although|and|then|instead|meanwhile)\b',
        r', ... \1',
        text,
        flags=re.IGNORECASE
    )

    # 4. Turn hyphens and dashes into clear dramatic pauses
    text = re.sub(r'\s*--\s*|\s*—\s*|\s*-\s*', ' ... ', text)

    # 5. Insert steady pause points between multi-sentence statements
    text = re.sub(r'([.!?])\s*', r'\1 ... ', text)

    # 6. Ensure full-stop ending for accurate pitch decay at speech completion
    if text and text[-1] not in '.!?':
        text += '.'

    # Clean up redundant dots
    text = re.sub(r'\.{4,}', '...', text)

    return text


# --- FISH AUDIO TTS API INTEGRATION ---
def generate_voice_fish_audio(text: str, reference_id: str = None) -> bytes:
    """
    Sends script to Fish Audio's API and returns standard audio bytes (MP3).
    """
    url = "https://api.fish.audio/v1/tts"
    
    headers = {
        "Authorization": f"Bearer {FISH_AUDIO_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "text": text,
        "format": "mp3"
    }
    
    # Pass cloned voice reference ID if stored in Redis for user
    if reference_id:
        payload["reference_id"] = reference_id

    response = requests.post(url, json=payload, headers=headers)
    
    if response.status_code != 200:
        raise Exception(f"Fish Audio API Error ({response.status_code}): {response.text}")
        
    return response.content


# --- TELEGRAM API HELPERS ---
def send_audio_bytes(chat_id: int, audio_bytes: bytes, filename: str = "voice.mp3"):
    """Posts binary audio directly to the user chat."""
    url = f"{TELEGRAM_API_URL}/sendAudio"
    files = {'audio': (filename, io.BytesIO(audio_bytes), 'audio/mpeg')}
    data = {'chat_id': chat_id}
    requests.post(url, data=data, files=files)

def send_message(chat_id: int, text: str):
    """Posts plain text messages to the user chat."""
    url = f"{TELEGRAM_API_URL}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    requests.post(url, json=payload)


# --- MAIN TELEGRAM WEBHOOK HANDLER ---
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    
    if not data or "message" not in data:
        return jsonify({"status": "ignored"}), 200

    message = data["message"]
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    raw_text = message.get("text", "").strip()

    if not raw_text:
        return jsonify({"status": "ok"}), 200

    # 1. Menu Buttons & Commands Handler
    if raw_text == "/start":
        send_message(chat_id, "Welcome! Send me any text script and I will read it in a natural voice for you.")
        return jsonify({"status": "ok"}), 200

    if raw_text in ["💳 Buy Credits", "Buy Credits"]:
        send_message(chat_id, "💳 Buy Credits menu coming soon! Telegram Stars payment integration in progress.")
        return jsonify({"status": "ok"}), 200

    if raw_text in ["📊 My Status", "My Status"]:
        is_admin = str(user_id) == str(ADMIN_ID)
        user_model = get_user_model(user_id)
        
        status_text = (
            f"👑 {'Admin Account Status' if is_admin else 'User Status'}\n\n"
            f"🎙 Voice Model: {'✅ Set' if user_model else '❌ Not Cloned Yet'}\n"
            f"⚡ Access Level: {'Unlimited Words' if is_admin else 'Standard Free Tier'}\n"
            f"🛡 Role: {'System Administrator' if is_admin else 'Standard User'}"
        )
        send_message(chat_id, status_text)
        return jsonify({"status": "ok"}), 200

    if raw_text in ["📝 Storytelling Tips", "Storytelling Tips"]:
        tips_text = (
            "💡 Storytelling & Social Media Tips:\n\n"
            "1. Hooks: Keep the first 3 seconds exciting!\n"
            "2. Punctuation: Use commas and periods to guide the AI's natural pauses.\n"
            "3. Visuals: Pair your generated audio with book quotes or aesthetic video loops!"
        )
        send_message(chat_id, tips_text)
        return jsonify({"status": "ok"}), 200

    # 2. TTS Voice Generation Handler
    try:
        user_voice_id = get_user_model(user_id)
        
        # Preprocess input text to insert expressive pause markers
        enhanced_text = prepare_text_for_tts(raw_text)
        
        # Generate and transmit audio
        audio_data = generate_voice_fish_audio(enhanced_text, reference_id=user_voice_id)
        send_audio_bytes(chat_id, audio_data)

    except Exception as e:
        print(f"Error processing voice generation: {e}")
        send_message(chat_id, "Sorry, something went wrong while generating the voice.")

    return jsonify({"status": "ok"}), 200


@app.route('/', methods=['GET'])
def home():
    return "Bot is running perfectly!", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
    
