import os
import re
import io
import time
import html
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# --- ENVIRONMENT CONFIGURATION ---
ADMIN_ID = os.getenv("ADMIN_ID", "")
FISH_AUDIO_API_KEY = (os.getenv("FISH_AUDIO_API_KEY") or "").strip()
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()

UPSTASH_REDIS_REST_URL = (os.getenv("UPSTASH_REDIS_REST_URL") or "").strip()
UPSTASH_REDIS_REST_TOKEN = (os.getenv("UPSTASH_REDIS_REST_TOKEN") or "").strip()

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# --- DEFAULT FISH AUDIO VOICE IDs ---
DEFAULT_VOICES = {
    "voice_adrian": "7f902a0a29f848f08f02f893d5b0a701",  # Deep Male Narrator
    "voice_selene": "8a123b0a29f848f08f02f893d5b0a702",  # Smooth Female Storyteller
    "voice_ethan": "9b234c0a29f848f08f02f893d5b0a703",   # Professional Male Explainer
    "voice_sarah": "0c345d0a29f848f08f02f893d5b0a704"    # Energetic Female
}


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
    except Exception as e:
        print(f"Upstash Redis REST Error: {e}")
    return None

def get_user_model(user_id: int) -> str:
    res = redis_command(["GET", f"user:{user_id}:model_id"])
    return str(res) if res else None

def set_user_model(user_id: int, model_id: str):
    redis_command(["SET", f"user:{user_id}:model_id", str(model_id)])

def get_user_selected_voice(user_id: int) -> str:
    res = redis_command(["GET", f"user:{user_id}:selected_voice"])
    return str(res) if res else "CLONED"

def set_user_selected_voice(user_id: int, voice_key: str):
    redis_command(["SET", f"user:{user_id}:selected_voice", str(voice_key)])

def get_active_voice_id(user_id: int) -> str:
    """Returns either the user's custom cloned ID or a selected system default voice ID."""
    selected = get_user_selected_voice(user_id)
    if selected in DEFAULT_VOICES:
        return DEFAULT_VOICES[selected]
    
    cloned_id = get_user_model(user_id)
    if cloned_id:
        return str(cloned_id)
        
    return DEFAULT_VOICES["voice_adrian"]

def get_user_state(user_id: int) -> str:
    res = redis_command(["GET", f"user:{user_id}:state"])
    return str(res) if res else "IDLE"

def set_user_state(user_id: int, state: str):
    redis_command(["SET", f"user:{user_id}:state", str(state)])


# --- CREDIT & BALANCE MANAGEMENT ---
def get_user_credits(user_id: int) -> int:
    val = redis_command(["GET", f"user:{user_id}:credits"])
    if val is None:
        redis_command(["SET", f"user:{user_id}:credits", 3])
        redis_command(["SET", f"user:{user_id}:last_reset", int(time.time())])
        return 3
    
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


# --- TEXT PREPROCESSING & CHUNKING ---
def prepare_text_for_tts(text: str) -> str:
    if not text:
        return ""
    
    # 1. Replace dashes and hyphens with clean commas for smooth natural pauses
    text = re.sub(r'—|-|--', ', ', text)
    
    # 2. Collapse double dots/ellipses into single full stops
    text = re.sub(r'\.{2,}', '.', text)
    
    # 3. Clean up excess whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    
    # 4. Filter unsupported characters while preserving words and punctuation
    text = re.sub(r'[^\w\s.,!?\'"]', '', text)
    
    return text


def split_text_into_chunks(text: str, max_chars: int = 250) -> list:
    """Splits long paragraphs into clean sentence chunks so long voice scripts don't glitch."""
    sentences = re.split(r'(?<=[.!?])\s+', text)
    chunks = []
    current_chunk = ""

    for sentence in sentences:
        if len(current_chunk) + len(sentence) <= max_chars:
            current_chunk += (" " if current_chunk else "") + sentence
        else:
            if current_chunk:
                chunks.append(current_chunk)
            current_chunk = sentence

    if current_chunk:
        chunks.append(current_chunk)
        
    return chunks if chunks else [text]


# --- FISH AUDIO API INTEGRATION ---
def clone_voice_fish_audio(file_bytes: bytes, title: str, mime_type: str = "audio/ogg") -> str:
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
        raise Exception(f"Fish Audio Error ({res.status_code}): {res.text}")
        
    res_data = res.json()
    return res_data.get("_id") or res_data.get("id")


def generate_single_tts_chunk(text: str, reference_id: str = None) -> bytes:
    url = "https://api.fish.audio/v1/tts"
    headers = {
        "Authorization": f"Bearer {FISH_AUDIO_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "text": text,
        "format": "mp3",
        "speed": 0.9
    }
    if reference_id:
        payload["reference_id"] = reference_id

    res = requests.post(url, json=payload, headers=headers, timeout=45)
    if res.status_code != 200:
        raise Exception(f"Fish Audio TTS Error ({res.status_code}): {res.text}")
    return res.content


def generate_voice_fish_audio(text: str, reference_id: str = None) -> bytes:
    """Generates clean chunked audio and joins byte outputs for seamless playback."""
    chunks = split_text_into_chunks(text)
    combined_audio = io.BytesIO()

    for chunk in chunks:
        audio_bytes = generate_single_tts_chunk(chunk, reference_id=reference_id)
        combined_audio.write(audio_bytes)

    return combined_audio.getvalue()


# --- TELEGRAM API HELPERS ---
def send_message(chat_id: int, text: str, reply_markup: dict = None, parse_mode: str = "HTML"):
    url = f"{TELEGRAM_API_URL}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
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
    requests.post(url, data=data, files=files, timeout=40)

def download_telegram_file(file_id: str):
    res = requests.get(f"{TELEGRAM_API_URL}/getFile?file_id={file_id}", timeout=10).json()
    file_path = res["result"]["file_path"]
    mime_type = "audio/ogg" if file_path.endswith(".ogg") or file_path.endswith(".opus") else "audio/mpeg"
    content = requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}", timeout=30).content
    return content, mime_type

def answer_callback_query(callback_query_id: str, text: str = None):
    url = f"{TELEGRAM_API_URL}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    requests.post(url, json=payload, timeout=5)

def send_stars_invoice(chat_id: int, star_amount: int):
    credits_amount = int(star_amount * 0.475)
    if credits_amount < 1:
        credits_amount = 1

    url = f"{TELEGRAM_API_URL}/sendInvoice"
    payload = {
        "chat_id": chat_id,
        "title": f"StoryVoice: {credits_amount} Credits",
        "description": f"Generates ~{credits_amount * 300:,} words in high-definition voiceover.",
        "payload": f"custom_credits_{credits_amount}",
        "currency": "XTR",
        "prices": [{"label": f"{credits_amount} Premium Credits", "amount": star_amount}]
    }
    requests.post(url, json=payload, timeout=10)

def answer_pre_checkout_query(pre_checkout_query_id: str, ok: bool = True):
    url = f"{TELEGRAM_API_URL}/answerPreCheckoutQuery"
    requests.post(url, json={"pre_checkout_query_id": pre_checkout_query_id, "ok": ok}, timeout=5)


# --- MAIN KEYBOARDS & INLINE MENUS ---
def get_main_keyboard():
    return {
        "keyboard": [
            [{"text": "🎙 Clone My Voice"}, {"text": "🎧 Voice Settings"}],
            [{"text": "💳 Buy Credits"}, {"text": "📊 My Status"}],
            [{"text": "📝 Storytelling Tips"}]
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

def get_voice_settings_keyboard(user_id: int):
    current = get_user_selected_voice(user_id)
    cloned_id = get_user_model(user_id)
    
    buttons = []
    
    if cloned_id:
        cloned_label = "✅ My Custom Cloned Voice" if current == "CLONED" else "🎙 My Custom Cloned Voice"
        buttons.append([{"text": cloned_label, "callback_data": "voice_set_CLONED"}])

    buttons.extend([
        [{"text": f"{'✅ ' if current == 'voice_adrian' else ''}🎙 Adrian (Deep Male Narrator)", "callback_data": "voice_set_voice_adrian"}],
        [{"text": f"{'✅ ' if current == 'voice_selene' else ''}🎙 Selene (Female Storyteller)", "callback_data": "voice_set_voice_selene"}],
        [{"text": f"{'✅ ' if current == 'voice_ethan' else ''}🎙 Ethan (Professional Male)", "callback_data": "voice_set_voice_ethan"}],
        [{"text": f"{'✅ ' if current == 'voice_sarah' else ''}🎙 Sarah (Energetic Female)", "callback_data": "voice_set_voice_sarah"}]
    ])
    
    return {"inline_keyboard": buttons}


# --- WEBHOOK ROUTER ---
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"status": "ignored"}), 200

    if "pre_checkout_query" in data:
        answer_pre_checkout_query(data["pre_checkout_query"]["id"], ok=True)
        return jsonify({"status": "ok"}), 200

    raw_text = ""
    message = None
    chat_id = None
    user_id = None
    
    if "callback_query" in data:
        cb = data["callback_query"]
        chat_id = cb["message"]["chat"]["id"]
        user_id = cb["from"]["id"]
        cb_data = cb.get("data", "")

        if cb_data.startswith("buy_"):
            answer_callback_query(cb["id"])
            tier = cb_data.replace("buy_", "")
            if tier == "custom":
                set_user_state(user_id, "AWAITING_CUSTOM_STARS")
                send_message(
                    chat_id, 
                    "<b>Custom Payment</b>\n\nPlease reply with the number of Telegram Stars you would like to spend (Minimum: <b>40 Stars</b>)."
                )
            else:
                send_stars_invoice(chat_id, int(tier))
            return jsonify({"status": "ok"}), 200

        elif cb_data.startswith("voice_set_"):
            selected_key = cb_data.replace("voice_set_", "")
            set_user_selected_voice(user_id, selected_key)
            answer_callback_query(cb["id"], text="Voice updated successfully!")
            
            send_message(
                chat_id,
                "🎙 <b>Active Voice Updated!</b>\n\nAll your future script voiceovers will be generated using this active profile:",
                reply_markup=get_voice_settings_keyboard(user_id)
            )
            return jsonify({"status": "ok"}), 200

        raw_text = cb_data

    elif "message" in data:
        message = data["message"]
        chat_id = message["chat"]["id"]
        user_id = message["from"]["id"]

        if "successful_payment" in message:
            payload = message["successful_payment"]["invoice_payload"]
            
            if payload.startswith("custom_credits_"):
                added = int(payload.replace("custom_credits_", ""))
            else:
                credits_map = {"pack_19": 19, "pack_57": 57, "pack_152": 152}
                added = credits_map.get(payload, 19)

            add_credits(user_id, added)
            send_message(chat_id, f"🎉 Payment Received! <b>{added} Premium Credits</b> added to your balance.", reply_markup=get_main_keyboard())
            return jsonify({"status": "ok"}), 200

        if "voice" in message or "audio" in message:
            file_obj = message.get("voice") or message.get("audio")
            send_message(chat_id, "⏳ <b>Processing your voice sample...</b>\nPlease wait a moment while we clone your voice.")
            try:
                audio_bytes, mime_type = download_telegram_file(file_obj["file_id"])
                model_id = clone_voice_fish_audio(audio_bytes, title=f"user_{user_id}_voice", mime_type=mime_type)
                
                set_user_model(user_id, model_id)
                set_user_selected_voice(user_id, "CLONED")
                set_user_state(user_id, "IDLE")
                
                success_msg = (
                    "🎉 <b>Your custom voice profile is active & saved permanently!</b>\n\n"
                    "Send any text script (up to 300 words) and I will bring it to life in your exact voice!"
                )
                send_message(chat_id, success_msg, reply_markup=get_main_keyboard())
            except Exception as e:
                print(f"Voice cloning error: {e}")
                send_message(chat_id, "❌ <b>Failed to clone voice sample.</b> Please make sure your recording is <b>at least 15-20 seconds long</b> in a quiet room.")
            return jsonify({"status": "ok"}), 200

        raw_text = message.get("text", "").strip()
    else:
        return jsonify({"status": "ignored"}), 200

    if not chat_id or not user_id:
        return jsonify({"status": "ignored"}), 200

    is_admin = str(user_id) == str(ADMIN_ID)

    if not raw_text:
        return jsonify({"status": "ok"}), 200

    clean_btn_text = re.sub(r'[^\w\s]', '', raw_text).strip().lower()

    if get_user_state(user_id) == "AWAITING_CUSTOM_STARS":
        if raw_text.isdigit():
            star_val = int(raw_text)
            if star_val < 40:
                send_message(chat_id, "⚠️ Minimum amount is <b>40 Stars</b>. Please enter 40 or higher:")
                return jsonify({"status": "ok"}), 200
            
            set_user_state(user_id, "IDLE")
            send_stars_invoice(chat_id, star_val)
            return jsonify({"status": "ok"}), 200
        else:
            send_message(chat_id, "⚠️ Invalid input. Please enter a valid number (e.g. 150):")
            return jsonify({"status": "ok"}), 200

    if raw_text in ["/start", "i Help", "Help"] or clean_btn_text in ["start", "help"]:
        get_user_credits(user_id)
        
        welcome_text = (
            "🎙 <b>Turn Written Ideas Into Studio-Grade Voiceovers—In Seconds.</b>\n\n"
            "Clone your own voice with 99% accuracy or deploy studio AI voice actors for your Telegram channels, TikToks, and podcasts.\n\n"
            "⚡ <b>What You Get Today:</b>\n"
            "• <b>3 Free Studio Generations</b> ready on your account.\n"
            "• <b>Instant Voice Cloning:</b> Send a 15s voice note to unlock your digital twin.\n"
            "• <b>Multi-Voice Studio:</b> Switch between your cloned voice and standard AI narrators seamlessly.\n"
            "• <b>Zero Rendering Delay:</b> Render full 300-word audio scripts delivered in seconds.\n\n"
            "🎁 <i>Daily Balance Refill:</i> 2 Free generations refreshed every 24 hours.\n\n"
            "👇 <b>Tap '🎙 Clone My Voice' below or pick a preset voice in '🎧 Voice Settings':</b>"
        )
        send_message(chat_id, welcome_text, reply_markup=get_main_keyboard())
        return jsonify({"status": "ok"}), 200

    if "clone my voice" in clean_btn_text:
        set_user_state(user_id, "AWAITING_VOICE")
        instructions = (
            "🎙 <b>Voice Cloning Instructions:</b>\n\n"
            "• <i>Environment:</i> Quiet room with zero background noise.\n"
            "• <i>Duration:</i> Record a clear <b>15 to 30 second</b> voice note.\n"
            "• <i>Tip:</i> Speak naturally at a steady, clear pace.\n\n"
            "👇 <b>Press the microphone button below to send your voice note now:</b>"
        )
        send_message(chat_id, instructions)
        return jsonify({"status": "ok"}), 200

    if "voice settings" in clean_btn_text:
        send_message(
            chat_id,
            "🎧 <b>Voice Studio Selection:</b>\n\nChoose whether you want to generate scripts using your <b>custom cloned voice</b> or select one of our default AI studio actors:",
            reply_markup=get_voice_settings_keyboard(user_id)
        )
        return jsonify({"status": "ok"}), 200

    if "buy credits" in clean_btn_text or raw_text == "/buy":
        buy_text = "💳 <b>Select a Credit Package or Enter Custom Stars:</b>\n\nChoose an option below:"
        send_message(chat_id, buy_text, reply_markup=get_buy_inline_keyboard())
        return jsonify({"status": "ok"}), 200

    if "my status" in clean_btn_text:
        user_credits = get_user_credits(user_id)
        user_model = get_user_model(user_id)
        active_voice = get_user_selected_voice(user_id)
        
        admin_flag = "Admin Account Status" if is_admin else "User Status"
        cloned_flag = "✅ Active & Saved" if user_model else "❌ Not Cloned Yet"
        balance_flag = "Unlimited" if is_admin else f"{user_credits} Credits Remaining"
        tier_flag = "System Administrator" if is_admin else "Standard Tier"
        
        status_text = (
            f"👑 <b>{admin_flag}</b>\n\n"
            f"🎙 Cloned Voice Profile: {cloned_flag}\n"
            f"🎧 Active Voice Engine: <code>{html.escape(active_voice.upper())}</code>\n"
            f"🪙 Credits Balance: {balance_flag}\n"
            f"⚡ Access Tier: {tier_flag}"
        )
        send_message(chat_id, status_text, reply_markup=get_main_keyboard())
        return jsonify({"status": "ok"}), 200

    if "storytelling tips" in clean_btn_text:
        tips_text = (
            "💡 <b>Storytelling & Social Media Tips:</b>\n\n"
            "1. <i>Hooks:</i> Keep the first 3 seconds exciting!\n"
            "2. <i>Punctuation:</i> Use commas and periods to guide natural pauses.\n"
            "3. <i>Visuals:</i> Pair your generated audio with book quotes or aesthetic video loops!"
        )
        send_message(chat_id, tips_text, reply_markup=get_main_keyboard())
        return jsonify({"status": "ok"}), 200

    # TTS Generation Router
    words = raw_text.split()
    if len(words) > 300:
        send_message(chat_id, f"⚠ <b>Script too long!</b> Your
