import os
import re
import io
import time
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


# --- UPSTASH REDIS HELPERS (HTTP REST) ---
def redis_command(command: list):
    """Executes Redis commands safely over HTTP REST."""
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return None
    headers = {"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"}
    try:
        res = requests.post(UPSTASH_REDIS_REST_URL, json=command, headers=headers)
        return res.json().get("result")
    except Exception as e:
        print(f"Upstash Redis Error: {e}")
        return None

def get_user_model(user_id: int) -> str:
    return redis_command(["GET", f"user:{user_id}:model_id"])

def set_user_model(user_id: int, model_id: str):
    redis_command(["SET", f"user:{user_id}:model_id", model_id])

def get_user_state(user_id: int) -> str:
    return redis_command(["GET", f"user:{user_id}:state"])

def set_user_state(user_id: int, state: str):
    redis_command(["SET", f"user:{user_id}:state", state])


# --- CREDIT & DAILY TRACKING SYSTEM ---
def get_user_credits(user_id: int) -> int:
    """Gets balance, granting 3 welcome credits on first join and 2 daily refills."""
    val = redis_command(["GET", f"user:{user_id}:credits"])
    
    # 1. New User Setup (3 Free Welcome Credits)
    if val is None:
        redis_command(["SET", f"user:{user_id}:credits", 3])
        redis_command(["SET", f"user:{user_id}:last_reset", int(time.time())])
        return 3
    
    # 2. Daily Refill Check (Every 24 Hours)
    last_reset = redis_command(["GET", f"user:{user_id}:last_reset"]) or 0
    if time.time() - int(last_reset) > 86400:
        current_credits = int(val)
        if current_credits < 2:
            redis_command(["SET", f"user:{user_id}:credits", 2])
            current_credits = 2
        redis_command(["SET", f"user:{user_id}:last_reset", int(time.time())])
        return current_credits

    return int(val)

def deduct_credit(user_id: int):
    credits = get_user_credits(user_id)
    if credits > 0:
        redis_command(["SET", f"user:{user_id}:credits", credits - 1])

def add_credits(user_id: int, amount: int):
    credits = get_user_credits(user_id)
    redis_command(["SET", f"user:{user_id}:credits", credits + amount])


# --- TEXT PREPROCESSING FOR HUMAN-LIKE EXPRESSION & PAUSES ---
def prepare_text_for_tts(text: str) -> str:
    if not text:
        return ""

    text = re.sub(r'\s+', ' ', text.strip())

    # Add natural breath pauses after greetings
    text = re.sub(
        r'^(hello|hi|hey|welcome|listen|look|so|well)([\s!,.?])',
        r'\1... ',
        text,
        flags=re.IGNORECASE
    )

    # Insert micro-pauses before connecting words
    text = re.sub(
        r'[,;]?\s+(but|however|so|because|although|and|then|instead|meanwhile)\b',
        r', ... \1',
        text,
        flags=re.IGNORECASE
    )

    # Convert hyphens/dashes to dramatic pauses
    text = re.sub(r'\s*--\s*|\s*—\s*|\s*-\s*', ' ... ', text)

    # Insert steady pause points between multi-sentence statements
    text = re.sub(r'([.!?])\s*', r'\1 ... ', text)

    if text and text[-1] not in '.!?':
        text += '.'

    return re.sub(r'\.{4,}', '...', text)


# --- FISH AUDIO API INTEGRATION ---
def clone_voice_fish_audio(file_bytes: bytes, title: str) -> str:
    """Uploads voice sample to Fish Audio and returns a persistent model ID."""
    url = "https://api.fish.audio/model"
    headers = {"Authorization": f"Bearer {FISH_AUDIO_API_KEY}"}
    files = {'voices': ('sample.mp3', file_bytes, 'audio/mpeg')}
    data = {'type': 'tts', 'title': title}
    
    res = requests.post(url, headers=headers, data=data, files=files)
    if res.status_code not in [200, 201]:
        raise Exception(f"Fish Audio Model Error ({res.status_code}): {res.text}")
        
    res_data = res.json()
    return res_data.get("_id") or res_data.get("id")

def generate_voice_fish_audio(text: str, reference_id: str = None) -> bytes:
    """Generates audio via Fish Audio using cloned voice model if available."""
    url = "https://api.fish.audio/v1/tts"
    headers = {
        "Authorization": f"Bearer {FISH_AUDIO_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {"text": text, "format": "mp3"}
    if reference_id:
        payload["reference_id"] = reference_id

    res = requests.post(url, json=payload, headers=headers)
    if res.status_code != 200:
        raise Exception(f"Fish Audio TTS Error ({res.status_code}): {res.text}")
        
    return res.content


# --- TELEGRAM API HELPERS ---
def send_message(chat_id: int, text: str):
    url = f"{TELEGRAM_API_URL}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})

def send_audio_bytes(chat_id: int, audio_bytes: bytes, filename: str = "voice.mp3"):
    url = f"{TELEGRAM_API_URL}/sendAudio"
    files = {'audio': (filename, io.BytesIO(audio_bytes), 'audio/mpeg')}
    requests.post(url, data={'chat_id': chat_id}, files=files)

def download_telegram_file(file_id: str) -> bytes:
    res = requests.get(f"{TELEGRAM_API_URL}/getFile?file_id={file_id}").json()
    file_path = res["result"]["file_path"]
    return requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}").content


# --- TELEGRAM STARS INVOICES (-5% GENERATIONS ADJUSTED) ---
def send_stars_invoice(chat_id: int, tier: str = "40"):
    tiers = {
        # 5% reduced generation packs to protect API profit margins
        "40": {"title": "19 Credits", "stars": 40, "desc": "Generates ~5,700 words in your voice.", "payload": "pack_19"},
        "100": {"title": "57 Credits", "stars": 100, "desc": "Generates ~17,100 words in your voice.", "payload": "pack_57"},
        "250": {"title": "152 Credits", "stars": 250, "desc": "Generates ~45,600 words in your voice.", "payload": "pack_152"},
    }
    selected = tiers.get(tier, tiers["40"])
    
    url = f"{TELEGRAM_API_URL}/sendInvoice"
    payload = {
        "chat_id": chat_id,
        "title": f"StoryVoice: {selected['title']}",
        "description": selected['desc'],
        "payload": selected['payload'],
        "currency": "XTR",  # Telegram Stars currency tag
        "prices": [{"label": selected['title'], "amount": selected['stars']}]
    }
    requests.post(url, json=payload)

def answer_pre_checkout_query(pre_checkout_query_id: str, ok: bool = True):
    url = f"{TELEGRAM_API_URL}/answerPreCheckoutQuery"
    requests.post(url, json={"pre_checkout_query_id": pre_checkout_query_id, "ok": ok})


# --- MAIN TELEGRAM WEBHOOK ROUTER ---
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    if not data:
        return jsonify({"status": "ignored"}), 200

    # 1. Telegram Stars Pre-Checkout Verification
    if "pre_checkout_query" in data:
        answer_pre_checkout_query(data["pre_checkout_query"]["id"], ok=True)
        return jsonify({"status": "ok"}), 200

    if "message" not in data:
        return jsonify({"status": "ignored"}), 200

    message = data["message"]
    chat_id = message["chat"]["id"]
    user_id = message["from"]["id"]
    is_admin = str(user_id) == str(ADMIN_ID)

    # 2. Process Successful Telegram Stars Payments
    if "successful_payment" in message:
        payload = message["successful_payment"]["invoice_payload"]
        credits_map = {"pack_19": 19, "pack_57": 57, "pack_152": 152}
        added = credits_map.get(payload, 19)
        
        add_credits(user_id, added)
        send_message(chat_id, f"🎉 Payment Received! *{added} Premium Credits* have been added to your balance.")
        return jsonify({"status": "ok"}), 200

    # 3. Handle Voice / Audio Clips for Voice Cloning
    if "voice" in message or "audio" in message:
        file_obj = message.get("voice") or message.get("audio")
        send_message(chat_id, "⏳ *Processing your voice sample...*\nPlease wait a moment.")
        
        try:
            audio_bytes = download_telegram_file(file_obj["file_id"])
            model_id = clone_voice_fish_audio(audio_bytes, title=f"user_{user_id}_voice")
            
            # Save cloned voice model to Upstash Redis
            set_user_model(user_id, model_id)
            set_user_state(user_id, "IDLE")
            
            success_msg = (
                "🎉 *Voice cloned successfully!*\n\n"
                "Your voice profile is permanently saved to your account. "
                "Send me any text script (up to 300 words) and I will read it in your exact voice!"
            )
            send_message(chat_id, success_msg)
        except Exception as e:
            print(f"Voice cloning error: {e}")
            send_message(chat_id, "❌ *Failed to clone voice sample.* Please send a clear 15-30s recording in a quiet room.")
            
        return jsonify({"status": "ok"}), 200

    # 4. Handle Text Input (Commands & Menu Buttons)
    raw_text = message.get("text", "").strip()
    if not raw_text:
        return jsonify({"status": "ok"}), 200

    if raw_text in ["/start", "i Help", "Help"]:
        get_user_credits(user_id)  # Initialize user credits in Redis
        welcome_text = (
            "Welcome to *StoryVoice AI*! 🎙️\n\n"
            "🎁 *Welcome Bonus:* You have 3 Free Credits!\n"
            "• Daily Refill: 2 Free credits refreshed every 24 hours.\n"
            "• Script Limit: Up to 300 words per voice generation.\n\n"
            "Tap *'🎙 Clone My Voice'* below to record your voice!"
        )
        send_message(chat_id, welcome_text)
        return jsonify({"status": "ok"}), 200

    if raw_text in ["🎙 Clone My Voice", "Clone My Voice"]:
        set_user_state(user_id, "AWAITING_VOICE")
        instructions = (
            "🎙 *Voice Cloning Instructions:*\n\n"
            "• *Environment:* Quiet background with zero noise or music.\n"
            "• *Duration:* Record a clear 15 to 30 second voice note.\n"
            "• *Tip:* Speak naturally at a steady, clear pace.\n\n"
            "👇 *Press the microphone button below to send your voice note now:*"
        )
        send_message(chat_id, instructions)
        return jsonify({"status": "ok"}), 200

    if raw_text in ["💳 Buy Credits", "Buy Credits", "/buy"]:
        send_stars_invoice(chat_id, "40")
        return jsonify({"status": "ok"}), 200

    if raw_text in ["📊 My Status", "My Status"]:
        user_credits = get_user_credits(user_id)
        user_model = get_user_model(user_id)
        status_text = (
            f"👑 *{'Admin Account Status' if is_admin else 'User Status'}*\n\n"
            f"🎙 Voice Model: {'✅ Saved & Active' if user_model else '❌ Not Cloned Yet'}\n"
            f"🪙 Credits Balance: {'Unlimited' if is_admin else f'{user_credits} Credits Remaining'}\n"
            f"⚡ Access Tier: {'System Administrator' if is_admin else 'Standard Tier'}"
        )
        send_message(chat_id, status_text)
        return jsonify({"status": "ok"}), 200

    if raw_text in ["📝 Storytelling Tips", "Storytelling Tips"]:
        tips_text = (
            "💡 *Storytelling & Social Media Tips:*\n\n"
            "1. *Hooks:* Keep the first 3 seconds exciting!\n"
            "2. *Punctuation:* Use commas and periods to guide natural pauses.\n"
            "3. *Visuals:* Pair your generated audio with book quotes or aesthetic video loops!"
        )
        send_message(chat_id, tips_text)
        return jsonify({"status": "ok"}), 200

    # 5. Process Script for TTS Generation
    words = raw_text.split()
    if len(words) > 300:
        send_message(chat_id, f"⚠️ *Script too long!* Your text contains {len(words)} words. The maximum limit is 300 words per generation.")
        return jsonify({"status": "ok"}), 200

    if not is_admin:
        user_credits = get_user_credits(user_id)
        if user_credits <= 0:
            send_message(
                chat_id,
                "❌ *You are out of credits!*\n\n"
                "Tap *'💳 Buy Credits'* to buy extra generations with Telegram Stars, or wait for your daily refill."
            )
            return jsonify({"status": "ok"}), 200

    try:
        user_voice_id = get_user_model(user_id)
        enhanced_text = prepare_text_for_tts(raw_text)
        audio_data = generate_voice_fish_audio(enhanced_text, reference_id=user_voice_id)
        
        # Deduct credit after successful API generation
        if not is_admin:
            deduct_credit(user_id)
            
        send_audio_bytes(chat_id, audio_data)

    except Exception as e:
        print(f"TTS error: {e}")
        send_message(chat_id, "Sorry, something went wrong while generating the voice.")

    return jsonify({"status": "ok"}), 200


@app.route('/', methods=['GET'])
def home():
    return "Bot is running perfectly!", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
    
