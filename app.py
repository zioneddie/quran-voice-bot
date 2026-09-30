import os
import re
import io
import time
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# --- ENVIRONMENT CONFIGURATION ---
ADMIN_ID = os.getenv("ADMIN_ID", "")
FISH_AUDIO_API_KEY = (os.getenv("FISH_AUDIO_API_KEY") or "").strip()
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()

# Sanitize Upstash Redis environment variables
UPSTASH_REDIS_REST_URL = (os.getenv("UPSTASH_REDIS_REST_URL") or "").strip()
UPSTASH_REDIS_REST_TOKEN = (os.getenv("UPSTASH_REDIS_REST_TOKEN") or "").strip()

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


# --- UPSTASH REDIS HELPERS ---
def redis_command(command: list):
    """Executes Redis commands over Upstash HTTP REST API."""
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN:
        return None
    headers = {"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"}
    try:
        res = requests.post(UPSTASH_REDIS_REST_URL, json=command, headers=headers, timeout=5)
        if res.status_code == 200:
            return res.json().get("result")
        else:
            print(f"Upstash Redis REST Non-200 Response ({res.status_code}): {res.text}")
    except Exception as e:
        print(f"Upstash Redis REST Error: {e}")
    return None

def get_user_model(user_id: int) -> str:
    res = redis_command(["GET", f"user:{user_id}:model_id"])
    return str(res) if res else None

def set_user_model(user_id: int, model_id: str):
    """Saves user voice model ID permanently until manually re-cloned."""
    redis_command(["SET", f"user:{user_id}:model_id", str(model_id)])

def get_user_state(user_id: int) -> str:
    res = redis_command(["GET", f"user:{user_id}:state"])
    return str(res) if res else "IDLE"

def set_user_state(user_id: int, state: str):
    redis_command(["SET", f"user:{user_id}:state", str(state)])


# --- CREDIT & DAILY BALANCE LOGIC ---
def get_user_credits(user_id: int) -> int:
    val = redis_command(["GET", f"user:{user_id}:credits"])
    
    # Initialize new user profile (3 Free Welcome Credits)
    if val is None:
        redis_command(["SET", f"user:{user_id}:credits", 3])
        redis_command(["SET", f"user:{user_id}:last_reset", int(time.time())])
        return 3
    
    # 24-Hour Daily Refill Check
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


# --- TEXT PREPROCESSING FOR NATURAL SYNTHESIS ---
def prepare_text_for_tts(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'\s+', ' ', text.strip())
    text = re.sub(r'^(hello|hi|hey|welcome|listen|look|so|well)([\s!,.?])', r'\1... ', text, flags=re.IGNORECASE)
    text = re.sub(r'[,;]?\s+(but|however|so|because|although|and|then|instead|meanwhile)\b', r', ... \1', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*--\s*|\s*—\s*|\s*-\s*', ' ... ', text)
    text = re.sub(r'([.!?])\s*', r'\1 ... ', text)
    if text and text[-1] not in '.!?':
        text += '.'
    return re.sub(r'\.{4,}', '...', text)


# --- FISH AUDIO API INTEGRATION ---
def clone_voice_fish_audio(file_bytes: bytes, title: str, mime_type: str = "audio/ogg") -> str:
    """Sends recorded user voice note to Fish Audio for instant voice cloning with required API fields."""
    url = "https://api.fish.audio/model"
    headers = {"Authorization": f"Bearer {FISH_AUDIO_API_KEY}"}
    
    filename = "voice.ogg" if "ogg" in mime_type or "opus" in mime_type else "voice.mp3"
    files = {'voices': (filename, file_bytes, mime_type)}
    
    data = {
        'type': 'tts',
        'title': title,
        'train_mode': 'fast'
    }
    
    res = requests.post(url, headers=headers, data=data, files=files, timeout=45)
    if res.status_code not in [200, 201]:
        print(f"Fish Audio Error Response: {res.text}")
        raise Exception(f"Fish Audio Error ({res.status_code}): {res.text}")
        
    res_data = res.json()
    return res_data.get("_id") or res_data.get("id")

def generate_voice_fish_audio(text: str, reference_id: str = None) -> bytes:
    url = "https://api.fish.audio/v1/tts"
    headers = {
        "Authorization": f"Bearer {FISH_AUDIO_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {"text": text, "format": "mp3"}
    if reference_id:
        payload["reference_id"] = reference_id

    res = requests.post(url, json=payload, headers=headers, timeout=45)
    if res.status_code != 200:
        raise Exception(f"Fish Audio TTS Error ({res.status_code}): {res.text}")
    return res.content


# --- TELEGRAM API HELPERS ---
def send_message(chat_id: int, text: str, reply_markup: dict = None):
    url = f"{TELEGRAM_API_URL}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    res = requests.post(url, json=payload, timeout=10)
    return res.json()

def send_audio_bytes(chat_id: int, audio_bytes: bytes, filename: str = "StoryVoice_AI_Audio.mp3", title: str = "StoryVoice Audio"):
    url = f"{TELEGRAM_API_URL}/sendAudio"
    files = {'audio': (filename, io.BytesIO(audio_bytes), 'audio/mpeg')}
    data = {
        'chat_id': chat_id,
        'title': title,
        'performer': 'StoryVoice AI'
    }
    requests.post(url, data=data, files=files, timeout=30)

def download_telegram_file(file_id: str):
    res = requests.get(f"{TELEGRAM_API_URL}/getFile?file_id={file_id}", timeout=10).json()
    file_path = res["result"]["file_path"]
    mime_type = "audio/ogg" if file_path.endswith(".ogg") or file_path.endswith(".opus") else "audio/mpeg"
    content = requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}", timeout=30).content
    return content, mime_type

def answer_callback_query(callback_query_id: str):
    url = f"{TELEGRAM_API_URL}/answerCallbackQuery"
    requests.post(url, json={"callback_query_id": callback_query_id}, timeout=5)


# --- TELEGRAM STARS INVOICE & CUSTOM PAYMENT SYSTEM ---
def send_stars_invoice(chat_id: int, star_amount: int):
    """Generates a dynamic Telegram Stars invoice based on any star count (Min: 40 Stars)."""
    credits_amount = int(star_amount * 0.475)
    if credits_amount < 1:
        credits_amount = 1

    url = f"{TELEGRAM_API_URL}/sendInvoice"
    payload = {
        "chat_id": chat_id,
        "title": f"StoryVoice: {credits_amount} Credits",
        "description": f"Generates ~{credits_amount * 300:,} words in your cloned voice.",
        "payload": f"custom_credits_{credits_amount}",
        "currency": "XTR",
        "prices": [{"label": f"{credits_amount} Premium Credits", "amount": star_amount}]
    }
    requests.post(url, json=payload, timeout=10)

def answer_pre_checkout_query(pre_checkout_query_id: str, ok: bool = True):
    url = f"{TELEGRAM_API_URL}/answerPreCheckoutQuery"
    requests.post(url, json={"pre_checkout_query_id": pre_checkout_query_id, "ok": ok}, timeout=5)


# --- MAIN KEYBOARD & INLINE UI ---
def get_main_keyboard():
    return {
        "keyboard": [
            [{"text": "🎙 Clone My Voice"}, {"text": "💳 Buy Credits"}],
            [{"text": "📊 My Status"}, {"text": "📝 Storytelling Tips"}]
        ],
        "resize_keyboard": True
    }

def get_buy_inline_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "⭐ 40 Stars (19 Credits)", "callback_data": "buy_40"}],
            [{"text": "⭐ 100 Stars (57 Credits)", "callback_data": "buy_100"}],
            [{"text": "⭐ 250 Stars (152 Credits)", "callback_data": "buy_250"}],
            [{"text": "✏️ Enter Custom Amount", "callback_data": "buy_custom"}]
        ]
    }


# --- MAIN WEBHOOK ROUTER ---
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"status": "ignored"}), 200

    # 1. Handle Telegram Stars Pre-Checkout Verification
    if "pre_checkout_query" in data:
        answer_pre_checkout_query(data["pre_checkout_query"]["id"], ok=True)
        return jsonify({"status": "ok"}), 200

    raw_text = ""
    message = None
    
    # 2. Extract Callback Query (Inline Keyboard Buttons)
    if "callback_query" in data:
        cb = data["callback_query"]
        answer_callback_query(cb["id"])
        chat_id = cb["message"]["chat"]["id"]
        user_id = cb["from"]["id"]
        cb_data = cb.get("data", "")

        if cb_data.startswith("buy_"):
            tier = cb_data.replace("buy_", "")
            if tier == "custom":
                set_user_state(user_id, "AWAITING_CUSTOM_STARS")
                send_message(
                    chat_id, 
                    "💬 *Custom Payment*\n\nPlease reply with the number of Telegram Stars you would like to spend (Minimum: **40 Stars**)."
                )
            else:
                send_stars_invoice(chat_id, int(tier))
            return jsonify({"status": "ok"}), 200

        raw_text = cb_data

    # 3. Extract Direct Messages
    elif "message" in data:
        message = data["message"]
        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]

        # Successful Telegram Stars Payment
        if "successful_payment" in message:
            payload = message["successful_payment"]["invoice_payload"]
            
            if payload.startswith("custom_credits_"):
                added = int(payload.replace("custom_credits_", ""))
            else:
                credits_map = {"pack_19": 19, "pack_57": 57, "pack_152": 152}
                added = credits_map.get(payload, 19)

            add_credits(user_id, added)
            send_message(chat_id, f"🎉 Payment Received! *{added} Premium Credits* added to your balance.", reply_markup=get_main_keyboard())
            return jsonify({"status": "ok"}), 200

        # Voice/Audio Note Processing for Voice Cloning
        if "voice" in message or "audio" in message:
            file_obj = message.get("voice") or message.get("audio")
            send_message(chat_id, "⏳ *Processing your voice sample...*\nPlease wait a moment while we clone your voice.")
            try:
                audio_bytes, mime_type = download_telegram_file(file_obj["file_id"])
                model_id = clone_voice_fish_audio(audio_bytes, title=f"user_{user_id}_voice", mime_type=mime_type)
                
                # Save persistent model ID to Upstash Redis (Saved forever until user re-clones)
                set_user_model(user_id, model_id)
                set_user_state(user_id, "IDLE")
                
                success_msg = (
                    "🎉 *Your voice profile is active & saved permanently!*\n\n"
                    "Send any text script (up to 300 words) and I will bring it to life in your exact voice!"
                )
                send_message(chat_id, success_msg, reply_markup=get_main_keyboard())
            except Exception as e:
                print(f"Voice cloning error: {e}")
                send_message(chat_id, "❌ *Failed to clone voice sample.* Please make sure your recording is **at least 15-20 seconds long** in a quiet room.")
            return jsonify({"status": "ok"}), 200

        raw_text = message.get("text", "").strip()
    else:
        return jsonify({"status": "ignored"}), 200

    is_admin = str(user_id) == str(ADMIN_ID)

    if not raw_text:
        return jsonify({"status": "ok"}), 200

    clean_btn_text = re.sub(r'[^\w\s]', '', raw_text).strip().lower()

    # Handle Custom Stars Input State
    if get_user_state(user_id) == "AWAITING_CUSTOM_STARS":
        if raw_text.isdigit():
            star_val = int(raw_text)
            if star_val < 40:
                send_message(chat_id, "⚠️ Minimum amount is **40 Stars**. Please enter 40 or higher:")
                return jsonify({"status": "ok"}), 200
            
            set_user_state(user_id, "IDLE")
            send_stars_invoice(chat_id, star_val)
            return jsonify({"status": "ok"}), 200
        else:
            send_message(chat_id, "⚠️ Invalid input. Please enter a valid number (e.g. 150):")
            return jsonify({"status": "ok"}), 200

    # 4. Standard Navigation Router
    if raw_text in ["/start", "i Help", "Help"] or clean_btn_text in ["start", "help"]:
        get_user_credits(user_id)
        welcome_text = (
            "✨ *Welcome to StoryVoice AI!* 🎙️\n\n"
            "Ever wondered what your stories or posts sound like spoken in your *own* cloned voice? Now you can hear it instantly!\n\n"
            "🎁 *Welcome Gift:* You have **3 Free Credits** ready to use!\n"
            "• **Daily Refill:** 2 Free credits refreshed every 24 hours.\n"
            "• **Script Limit:** Up to 300 words per voice clip.\n"
            "• **Permanent Storage:** Your voice stays saved permanently until you re-clone it.\n\n"
            "👇 Tap *'🎙 Clone My Voice'* below to get started!"
        )
        send_message(chat_id, welcome_text, reply_markup=get_main_keyboard())
        return jsonify({"status": "ok"}), 200

    if "clone my voice" in clean_btn_text:
        set_user_state(user_id, "AWAITING_VOICE")
        instructions = (
            "🎙 *Voice Cloning Instructions:*\n\n"
            "• *Environment:* Quiet room with zero noise or music.\n"
            "• *Duration:* Record a clear **15 to 30 second** voice note.\n"
            "• *Tip:* Speak naturally at a steady, clear pace.\n\n"
            "👇 *Press the microphone button below to send your voice note now:*"
        )
        send_message(chat_id, instructions)
        return jsonify({"status": "ok"}), 200

    if "buy credits" in clean_btn_text or raw_text == "/buy":
        buy_text = "💳 *Select a Credit Package or Enter Custom Stars:*\n\nChoose an option below:"
        send_message(chat_id, buy_text, reply_markup=get_buy_inline_keyboard())
        return jsonify({"status": "ok"}), 200

    if "my status" in clean_btn_text:
        user_credits = get_user_credits(user_id)
        user_model = get_user_model(user_id)
        status_text = (
            f"👑 *{'Admin Account Status' if is_admin else 'User Status'}*\n\n"
            f"🎙 Voice Model: {'✅ Saved & Active Permanently' if user_model else '❌ Not Cloned Yet'}\n"
            f"🪙 Credits Balance: {'Unlimited' if is_admin else f'{user_credits} Credits Remaining'}\n"
            f"⚡ Access Tier: {'System Administrator' if is_admin else 'Standard Tier'}"
        )
        send_message(chat_id, status_text, reply_markup=get_main_keyboard())
        return jsonify({"status": "ok"}), 200

    if "storytelling tips" in clean_btn_text:
        tips_text = (
            "💡 *Storytelling & Social Media Tips:*\n\n"
            "1. *Hooks:* Keep the first 3 seconds exciting!\n"
            "2. *Punctuation:* Use commas and periods to guide natural pauses.\n"
            "3. *Visuals:* Pair your generated audio with book quotes or aesthetic video loops!"
        )
        send_message(chat_id, tips_text, reply_markup=get_main_keyboard())
        return jsonify({"status": "ok"}), 200

    # 5. TTS Voice Audio Generation
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
                "Tap *'💳 Buy Credits'* to buy extra generations with Telegram Stars, or wait for your daily refill.",
                reply_markup=get_main_keyboard()
            )
            return jsonify({"status": "ok"}), 200

    # Instant status update to inform user generation is in progress
    send_message(chat_id, "⏳ *Generating your audio clip...* Please give me a few seconds.")

    try:
        user_voice_id = get_user_model(user_id)
        enhanced_text = prepare_text_for_tts(raw_text)
        audio_data = generate_voice_fish_audio(enhanced_text, reference_id=user_voice_id)
        
        if not is_admin:
            deduct_credit(user_id)
            
        send_audio_bytes(chat_id, audio_data)

    except Exception as e:
        print(f"TTS error: {e}")
        send_message(chat_id, "Sorry, something went wrong while generating your voice audio.")

    return jsonify({"status": "ok"}), 200


@app.route('/', methods=['GET'])
def home():
    return "Bot is running perfectly!", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
