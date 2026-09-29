import os
import re
import requests
import threading
from datetime import datetime
from flask import Flask, request
import replicate

app = Flask(__name__)

# --- Environment Variables ---
def get_clean_env(key_name: str) -> str:
    val = os.environ.get(key_name, "")
    if not val:
        return ""
    return val.replace("\r", "").replace("\n", "").strip().strip('"\'')

BOT_TOKEN = get_clean_env("BOT_TOKEN")
ADMIN_ID = get_clean_env("ADMIN_ID")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

UPSTASH_URL = (
    get_clean_env("UPSTASH_REDIS_REST_URL") or 
    get_clean_env("UPSTASH_URL") or 
    get_clean_env("REDIS_URL")
)

UPSTASH_TOKEN = (
    get_clean_env("UPSTASH_REDIS_REST_TOKEN") or 
    get_clean_env("UPSTASH_TOKEN") or 
    get_clean_env("REDIS_TOKEN")
)

# --- Rules & Limits ---
MAX_CHARS_REGULAR = 400      # Max text length for standard users
MAX_CHARS_ADMIN = 2000      # Max text length for Admin
DAILY_FREE_LIMIT = 5        # Free daily voiceover quota

# --- Keyboards ---
MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "🎙️ Clone My Voice"}, {"text": "💳 Buy Credits"}],
        [{"text": "📊 My Status"}, {"text": "📝 Storytelling Tips"}],
        [{"text": "ℹ️ Help"}]
    ],
    "resize_keyboard": True
}

BUY_KEYBOARD = {
    "inline_keyboard": [
        [{"text": "⭐ 10 Credits — 15 Stars", "pay": True}],
        [{"text": "⭐ 45 Credits — 50 Stars", "pay": True}],
        [{"text": "⭐ 100 Credits — 100 Stars", "pay": True}]
    ]
}

# --- Text Sanitization & Anti-Hallucination ---
def sanitize_and_fix_script(text: str) -> str:
    """
    Cleans script to prevent XTTS-v2 model hallucinations and end-of-sentence gibberish.
    Forces ending punctuation so the model cleanly triggers its end-of-sequence token.
    """
    if not text:
        return ""
    text = text.strip()
    
    # Remove emojis and unsupported special characters that confuse the AI tokenizer
    text = re.sub(r'[^\w\s.,!?\'"-]', '', text)
    
    # Collapse multiple spaces or newlines into a single space
    text = re.sub(r'\s+', ' ', text)
    
    # Force proper end punctuation if missing
    if text and not text.endswith(('.', '!', '?')):
        text += '.'
        
    return text

# --- Telegram Helper Functions ---
def send_message(chat_id: int, text: str, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    requests.post(f"{TELEGRAM_API}/sendMessage", json=payload)

def send_invoice(chat_id: int, title: str, description: str, payload_str: str, stars_amount: int):
    """Sends a Telegram Stars invoice natively."""
    invoice_data = {
        "chat_id": chat_id,
        "title": title,
        "description": description,
        "payload": payload_str,
        "provider_token": "",  # Must be empty for Telegram Stars
        "currency": "XTR",
        "prices": [{"label": title, "amount": stars_amount}]
    }
    requests.post(f"{TELEGRAM_API}/sendInvoice", json=invoice_data)

def send_voice(chat_id: int, voice_url: str):
    """Downloads audio from Replicate and uploads raw bytes directly to Telegram."""
    try:
        audio_res = requests.get(voice_url, timeout=25)
        if audio_res.status_code != 200:
            send_message(chat_id, f"⚠️ Failed to retrieve audio from generator (HTTP {audio_res.status_code}).")
            return

        files = {"audio": ("narration.wav", audio_res.content, "audio/wav")}
        res = requests.post(f"{TELEGRAM_API}/sendAudio", data={"chat_id": chat_id}, files=files, timeout=30)
        
        res_json = res.json()
        if not res_json.get("ok"):
            err_desc = res_json.get("description", "Delivery issue")
            send_message(chat_id, f"⚠️ Delivery notice: {err_desc}")
    except Exception as e:
        send_message(chat_id, f"⚠️ Delivery exception: {str(e)}")

def get_telegram_file_url(file_id: str) -> str:
    res = requests.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id}).json()
    file_path = res.get("result", {}).get("file_path")
    if file_path:
        return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    return None

# --- Upstash Redis Helper ---
def redis_cmd(*args):
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        return False, "Missing database environment variables."

    try:
        url = UPSTASH_URL.rstrip('/')
        headers = {"Authorization": f"Bearer {UPSTASH_TOKEN}"}
        res = requests.post(url, headers=headers, json=list(args), timeout=5)
        
        if res.status_code == 200:
            return True, res.json().get("result")
        else:
            return False, f"Upstash HTTP {res.status_code}: {res.text}"
    except Exception as e:
        return False, f"Connection error: {str(e)}"

def get_user_voice(user_id: int) -> str:
    success, val = redis_cmd("GET", f"voice:{user_id}")
    return str(val) if (success and val) else None

def save_user_voice(user_id: int, voice_url: str) -> tuple:
    success, res = redis_cmd("SET", f"voice:{user_id}", voice_url)
    if success and res == "OK":
        return True, "OK"
    return False, str(res)

def get_paid_credits(user_id: int) -> int:
    success, val = redis_cmd("GET", f"paid_credits:{user_id}")
    return int(val) if (success and val) else 0

def add_paid_credits(user_id: int, amount: int):
    redis_cmd("INCRBY", f"paid_credits:{user_id}", amount)

def deduct_paid_credit(user_id: int):
    redis_cmd("DECR", f"paid_credits:{user_id}")

def check_and_increment_quota(user_id: int) -> tuple:
    """Checks free daily quota first, then falls back to paid credits."""
    # Admin bypass
    if ADMIN_ID and str(user_id) == str(ADMIN_ID):
        return True, "Admin (Unlimited)", 0

    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    key = f"usage:{user_id}:{today_str}"
    
    success, current = redis_cmd("GET", key)
    current_count = int(current) if (success and current) else 0

    # 1. Free daily quota available
    if current_count < DAILY_FREE_LIMIT:
        success_incr, new_count = redis_cmd("INCR", key)
        if success_incr and (new_count == 1 or new_count == "1"):
            redis_cmd("EXPIRE", key, 172800)
        return True, f"Free ({new_count}/{DAILY_FREE_LIMIT})", get_paid_credits(user_id)

    # 2. Daily quota exhausted, check paid credits balance
    paid = get_paid_credits(user_id)
    if paid > 0:
        deduct_paid_credit(user_id)
        return True, f"Paid Credit Used ({paid - 1} remaining)", paid - 1

    return False, "Quota Exceeded", 0

# --- Voice Generation ---
def generate_voice_narration(text: str, speaker_audio_url: str) -> str:
    # Clean text to stop end-of-sentence gibberish
    clean_text = sanitize_and_fix_script(text)
    
    output = replicate.run(
        "lucataco/xtts-v2:684bc3855b37866c0c65add2ff39c78f3dea3f4ff103a436465326e0f438d55e",
        input={
            "text": clean_text,
            "speaker": speaker_audio_url,
            "language": "en",
            "speed": 1.0
        }
    )
    if isinstance(output, list):
        return str(output[0])
    return str(output)

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

    try:
        data = request.get_json(silent=True)
        if not data:
            return "OK", 200

        # --- 1. Handle Telegram Stars Pre-Checkout Query ---
        if "pre_checkout_query" in data:
            query_id = data["pre_checkout_query"]["id"]
            requests.post(
                f"{TELEGRAM_API}/answerPreCheckoutQuery",
                json={"pre_checkout_query_id": query_id, "ok": True}
            )
            return "OK", 200

        # --- 2. Handle Messages ---
        if "message" in data:
            msg = data["message"]
            chat_id = msg["chat"]["id"]
            user_id = msg["from"]["id"]
            text = msg.get("text", "").strip()
            is_admin = bool(ADMIN_ID and str(user_id) == str(ADMIN_ID))

            # --- Successful Payment Callback ---
            if "successful_payment" in msg:
                payload = msg["successful_payment"]["invoice_payload"]
                credits_to_add = 0
                if payload == "credits_10":
                    credits_to_add = 10
                elif payload == "credits_45":
                    credits_to_add = 45
                elif payload == "credits_100":
                    credits_to_add = 100

                if credits_to_add > 0:
                    add_paid_credits(user_id, credits_to_add)
                    new_total = get_paid_credits(user_id)
                    send_message(
                        chat_id,
                        f"🎉 Payment Successful!\n\nAdded {credits_to_add} voiceover credits to your balance.\nTotal Paid Credits: {new_total}",
                        reply_markup=MAIN_KEYBOARD
                    )
                return "OK", 200

            # --- Start Command ---
            if text == "/start":
                start_message = (
                    "Welcome to StoryVoice AI Studio! 🎙️✨\n\n"
                    "The first Ugandan AI Voice Cloning Bot on Telegram.\n\n"
                    "⚡ Quick Start:\n"
                    "1️⃣ Tap '🎙️ Clone My Voice' and send a clear 10–15s voice note.\n"
                    "2️⃣ Send any text script or story.\n"
                    "3️⃣ Receive your custom audio narration in seconds!\n\n"
                    "🎁 You get 5 FREE generations every single day!"
                )
                send_message(chat_id, start_message, reply_markup=MAIN_KEYBOARD)
                return "OK", 200

            # --- Buy Credits Menu ---
            if text == "💳 Buy Credits" or text == "/buy":
                send_message(
                    chat_id,
                    "⭐ Choose a Telegram Stars credit pack to top up your balance:",
                    reply_markup=MAIN_KEYBOARD
                )
                send_invoice(chat_id, "10 Voiceover Credits", "Generate 10 custom voiceovers", "credits_10", 15)
                send_invoice(chat_id, "45 Voiceover Credits", "Generate 45 custom voiceovers", "credits_45", 50)
                send_invoice(chat_id, "100 Voiceover Credits", "Generate 100 custom voiceovers", "credits_100", 100)
                return "OK", 200

            # --- Menu Options ---
            if text == "🎙️ Clone My Voice":
                send_message(
                    chat_id,
                    "🎙 Voice Cloning & Updating:\n\n"
                    "1. Find a quiet room with minimal background noise.\n"
                    "2. Record a 10–15 second voice note speaking naturally.\n"
                    "3. Stop the recording immediately after speaking so there's no trailing noise!\n\n"
                    "💡 Sending a new voice note at any time will instantly replace your old cloned voice!",
                    reply_markup=MAIN_KEYBOARD
                )
                return "OK", 200

            if text == "📝 Storytelling Tips":
                send_message(
                    chat_id,
                    "📝 AI Voiceover Tips:\n\n"
                    "• Always end sentences with periods (.) or exclamation marks (!) for clean speech pauses.\n"
                    "• Use commas for natural pauses.\n"
                    "• Keep paragraphs under 400 characters for optimal clarity and speed.",
                    reply_markup=MAIN_KEYBOARD
                )
                return "OK", 200

            if text == "📊 My Status":
                user_voice = get_user_voice(user_id)
                voice_status = "✅ Active Voice Profile Saved" if user_voice else "❌ No Voice Saved Yet"
                
                today_str = datetime.utcnow().strftime("%Y-%m-%d")
                success, used_val = redis_cmd("GET", f"usage:{user_id}:{today_str}")
                current_used = int(used_val) if (success and used_val) else 0
                free_remaining = max(0, DAILY_FREE_LIMIT - current_used)
                paid_balance = get_paid_credits(user_id)

                admin_badge = " 👑 [ADMIN MODE ACTIVE]" if is_admin else ""

                status_msg = (
                    f"📊 Your StoryVoice Profile{admin_badge}:\n\n"
                    f"Voice Status: {voice_status}\n"
                    f"Free Daily Generations: {free_remaining}/{DAILY_FREE_LIMIT} remaining today\n"
                    f"Paid Credits Balance: {paid_balance} credits"
                )
                send_message(chat_id, status_msg, reply_markup=MAIN_KEYBOARD)
                return "OK", 200

            if text == "ℹ️ Help":
                send_message(
                    chat_id,
                    "ℹ️ How to use StoryVoice AI:\n\n"
                    "1. Record a clear 10–15s voice note to save your voice profile.\n"
                    "2. Send any text message to convert to speech.\n"
                    "3. Stop voice notes immediately after speaking to prevent audio glitches.",
                    reply_markup=MAIN_KEYBOARD
                )
                return "OK", 200

            # --- Save/Re-clone Voice Note ---
            voice_obj = msg.get("voice") or msg.get("audio")
            if voice_obj:
                file_id = voice_obj.get("file_id")
                if file_id:
                    voice_url = get_telegram_file_url(file_id)
                    if voice_url:
                        saved, err_details = save_user_voice(user_id, voice_url)
                        if saved:
                            send_message(
                                chat_id,
                                "✅ Voice profile updated permanently! You can now send text messages to generate voiceovers.",
                                reply_markup=MAIN_KEYBOARD
                            )
                        else:
                            send_message(chat_id, f"⚠️ Database Error: {err_details}", reply_markup=MAIN_KEYBOARD)
                    else:
                        send_message(chat_id, "⚠️ Couldn't retrieve audio file from Telegram.", reply_markup=MAIN_KEYBOARD)
                return "OK", 200

            # --- Process Text Script ---
            if text and not text.startswith("/"):
                max_allowed_chars = MAX_CHARS_ADMIN if is_admin else MAX_CHARS_REGULAR
                
                if len(text) > max_allowed_chars:
                    send_message(
                        chat_id,
                        f"⚠️ Script exceeds length limit ({len(text)}/{max_allowed_chars} chars). Please shorten your text.",
                        reply_markup=MAIN_KEYBOARD
                    )
                    return "OK", 200

                user_voice_url = get_user_voice(user_id)
                if not user_voice_url:
                    send_message(
                        chat_id,
                        "Please record and upload a voice note first by tapping '🎙️ Clone My Voice'!",
                        reply_markup=MAIN_KEYBOARD
                    )
                    return "OK", 200

                allowed, status_info, remaining_paid = check_and_increment_quota(user_id)
                if not allowed:
                    send_message(
                        chat_id,
                        f"⚠️ You've reached your free daily limit of {DAILY_FREE_LIMIT} voiceovers!\n\nTap '💳 Buy Credits' to top up using Telegram Stars.",
                        reply_markup=MAIN_KEYBOARD
                    )
                    return "OK", 200

                send_message(chat_id, f"🎙️ Generating voiceover ({status_info})...", reply_markup=MAIN_KEYBOARD)

                threading.Thread(
                    target=process_narration_task,
                    args=(chat_id, text, user_voice_url)
                ).start()

                return "OK", 200

    except Exception as global_err:
        print(f"Global error: {global_err}")

    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
    
