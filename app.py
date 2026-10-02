import os, re, io, time, html, requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# --- ENV & CONFIG ---
ADMIN_ID = os.getenv("ADMIN_ID", "")
FISH_AUDIO_API_KEY = (os.getenv("FISH_AUDIO_API_KEY") or "").strip()
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
UPSTASH_REDIS_REST_URL = (os.getenv("UPSTASH_REDIS_REST_URL") or "").strip()
UPSTASH_REDIS_REST_TOKEN = (os.getenv("UPSTASH_REDIS_REST_TOKEN") or "").strip()
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

DEFAULT_VOICES = {
    "voice_adrian": "Adrian (Deep Male)",
    "voice_selene": "Selene (Female Storyteller)",
    "voice_ethan": "Ethan (Professional Male)",
    "voice_sarah": "Sarah (Energetic Female)"
}

# --- REDIS HELPERS ---
def redis_cmd(cmd: list):
    if not UPSTASH_REDIS_REST_URL or not UPSTASH_REDIS_REST_TOKEN: return None
    try:
        r = requests.post(UPSTASH_REDIS_REST_URL, json=cmd, headers={"Authorization": f"Bearer {UPSTASH_REDIS_REST_TOKEN}"}, timeout=5)
        return r.json().get("result") if r.status_code == 200 else None
    except Exception as e:
        print(f"Redis error: {e}"); return None

def get_db(u_id, key, default=None):
    res = redis_cmd(["GET", f"user:{u_id}:{key}"])
    return str(res) if res is not None else default

def set_db(u_id, key, val): redis_cmd(["SET", f"user:{u_id}:{key}", str(val)])

def get_active_voice_id(u_id: int):
    selected = get_db(u_id, "selected_voice", "CLONED")
    if selected == "CLONED":
        cloned = get_db(u_id, "model_id")
        if cloned: return str(cloned)
    return None

def get_user_credits(u_id: int) -> int:
    val = get_db(u_id, "credits")
    if val is None:
        set_db(u_id, "credits", 3); set_db(u_id, "last_reset", int(time.time()))
        return 3
    last_reset = int(get_db(u_id, "last_reset", 0))
    if time.time() - last_reset > 86400:
        curr = max(int(val), 2)
        set_db(u_id, "credits", curr); set_db(u_id, "last_reset", int(time.time()))
        return curr
    return int(val)

def deduct_credit(u_id: int):
    c = get_user_credits(u_id)
    if c > 0: set_db(u_id, "credits", c - 1)

def add_credits(u_id: int, amt: int): set_db(u_id, "credits", get_user_credits(u_id) + amt)

# --- TEXT & FISH AUDIO ---
def prepare_text(text: str) -> str:
    text = re.sub(r'—|-|--', ', ', text)
    text = re.sub(r'\.{2,}', '.', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return re.sub(r'[^\w\s.,!?\'"]', '', text)

def split_text(text: str, max_chars: int = 250) -> list:
    sentences = re.split(r'(?<=[.!?])\s+', text)
    chunks, curr = [], ""
    for s in sentences:
        if len(curr) + len(s) <= max_chars: curr += (" " if curr else "") + s
        else:
            if curr: chunks.append(curr)
            curr = s
    if curr: chunks.append(curr)
    return chunks or [text]

def clone_voice(file_bytes: bytes, title: str, mime: str = "audio/ogg") -> str:
    fn = "voice.ogg" if "ogg" in mime or "opus" in mime else "voice.mp3"
    res = requests.post("https://api.fish.audio/model", 
                        headers={"Authorization": f"Bearer {FISH_AUDIO_API_KEY}"},
                        data={'type': 'tts', 'title': title, 'train_mode': 'fast'},
                        files={'voices': (fn, file_bytes, mime)}, timeout=45)
    if res.status_code not in [200, 201]: raise Exception(res.text)
    d = res.json()
    return d.get("_id") or d.get("id")

def generate_voice(text: str, ref_id: str = None) -> bytes:
    out = io.BytesIO()
    headers = {"Authorization": f"Bearer {FISH_AUDIO_API_KEY}", "Content-Type": "application/json"}
    for chunk in split_text(text):
        payload = {"text": chunk, "format": "mp3", "speed": 0.9}
        if ref_id: payload["reference_id"] = ref_id
        res = requests.post("https://api.fish.audio/v1/tts", json=payload, headers=headers, timeout=45)
        if res.status_code != 200: raise Exception(res.text)
        out.write(res.content)
    return out.getvalue()

# --- TELEGRAM API HELPERS ---
def tg_req(method: str, payload: dict = None, files: dict = None, data: dict = None):
    url = f"{TELEGRAM_API_URL}/{method}"
    if files: return requests.post(url, data=data, files=files, timeout=40).json()
    return requests.post(url, json=payload or {}, timeout=10).json()

def send_msg(c_id, text, reply_markup=None):
    return tg_req("sendMessage", {"chat_id": c_id, "text": text, "parse_mode": "HTML", "reply_markup": reply_markup})

def edit_msg(c_id, m_id, text, reply_markup=None):
    return tg_req("editMessageText", {"chat_id": c_id, "message_id": m_id, "text": text, "parse_mode": "HTML", "reply_markup": reply_markup})

def delete_msg(c_id, m_id):
    return tg_req("deleteMessage", {"chat_id": c_id, "message_id": m_id})

def send_chat_action(c_id, action="record_voice"):
    return tg_req("sendChatAction", {"chat_id": c_id, "action": action})

def send_audio(c_id, audio_bytes):
    files = {'audio': ("StoryVoice.mp3", io.BytesIO(audio_bytes), 'audio/mpeg')}
    tg_req("sendAudio", files=files, data={'chat_id': c_id, 'title': 'StoryVoice Audio', 'performer': 'StoryVoice AI'})

def download_file(file_id: str):
    res = tg_req("getFile", {"file_id": file_id})
    path = res["result"]["file_path"]
    mime = "audio/ogg" if path.endswith((".ogg", ".opus")) else "audio/mpeg"
    content = requests.get(f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{path}", timeout=30).content
    return content, mime

def send_invoice(c_id, stars):
    creds = max(int(stars * 0.475), 1)
    payload = {
        "chat_id": c_id, "title": f"StoryVoice: {creds} Credits",
        "description": f"Generates ~{creds * 300:,} words in HD voiceover.",
        "payload": f"custom_credits_{creds}", "currency": "XTR",
        "prices": [{"label": f"{creds} Premium Credits", "amount": stars}]
    }
    tg_req("sendInvoice", payload)

# --- KEYBOARDS ---
def get_main_kb():
    return {"keyboard": [[{"text": "🎙 Clone My Voice"}, {"text": "🎧 Voice Settings"}],
                         [{"text": "💳 Buy Credits"}, {"text": "📊 My Status"}],
                         [{"text": "📝 Storytelling Tips"}]], "resize_keyboard": True}

def get_buy_kb():
    return {"inline_keyboard": [[{"text": "⭐ 40 Stars (19 Credits)", "callback_data": "buy_40"}],
                                [{"text": "⭐ 100 Stars (57 Credits)", "callback_data": "buy_100"}],
                                [{"text": "⭐ 250 Stars (152 Credits)", "callback_data": "buy_250"}],
                                [{"text": "✏ Enter Custom Amount", "callback_data": "buy_custom"}]]}

def get_voice_kb(u_id: int):
    curr = get_db(u_id, "selected_voice", "CLONED")
    btn = []
    if get_db(u_id, "model_id"):
        btn.append([{"text": f"{'✅ ' if curr == 'CLONED' else '🎙 '}My Custom Cloned Voice", "callback_data": "voice_set_CLONED"}])
    for k, v in DEFAULT_VOICES.items():
        btn.append([{"text": f"{'✅ ' if curr == k else ''}🎙 {v}", "callback_data": f"voice_set_{k}"}])
    return {"inline_keyboard": btn}

# --- ROUTER ---
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json(force=True, silent=True)
    if not data: return jsonify({"status": "ignored"}), 200

    if "pre_checkout_query" in data:
        tg_req("answerPreCheckoutQuery", {"pre_checkout_query_id": data["pre_checkout_query"]["id"], "ok": True})
        return jsonify({"status": "ok"}), 200

    c_id, u_id, raw_text = None, None, ""

    if "callback_query" in data:
        cb = data["callback_query"]
        c_id, u_id, cb_data = cb["message"]["chat"]["id"], cb["from"]["id"], cb.get("data", "")
        m_id = cb["message"]["message_id"]
        tg_req("answerCallbackQuery", {"callback_query_id": cb["id"]})

        if cb_data.startswith("buy_"):
            tier = cb_data.replace("buy_", "")
            delete_msg(c_id, m_id)
            if tier == "custom":
                set_db(u_id, "state", "AWAITING_CUSTOM_STARS")
                send_msg(c_id, "<b>Custom Payment</b>\n\nReply with Telegram Stars to spend (Minimum: <b>40 Stars</b>).")
            else:
                send_invoice(c_id, int(tier))
            return jsonify({"status": "ok"}), 200

        elif cb_data.startswith("voice_set_"):
            selected = cb_data.replace("voice_set_", "")
            set_db(u_id, "selected_voice", selected)
            edit_msg(c_id, m_id, "🎙 <b>Active Voice Updated!</b>\n\nAll future scripts will use this profile:", reply_markup=get_voice_kb(u_id))
            return jsonify({"status": "ok"}), 200

        return jsonify({"status": "ok"}), 200

    elif "message" in data:
        msg = data["message"]
        c_id, u_id = msg["chat"]["id"], msg["from"]["id"]

        if "successful_payment" in msg:
            p = msg["successful_payment"]["invoice_payload"]
            added = int(p.replace("custom_credits_", "")) if p.startswith("custom_credits_") else {"pack_19":19, "pack_57":57, "pack_152":152}.get(p, 19)
            add_credits(u_id, added)
            send_msg(c_id, f"🎉 Payment Received! <b>{added} Premium Credits</b> added.", reply_markup=get_main_kb())
            return jsonify({"status": "ok"}), 200

        if "voice" in msg or "audio" in msg:
            f_obj = msg.get("voice") or msg.get("audio")
            status_msg = send_msg(c_id, "⏳ <b>Processing your voice sample...</b>\nPlease wait standard processing time.")
            try:
                b, mime = download_file(f_obj["file_id"])
                m_id = clone_voice(b, title=f"user_{u_id}_voice", mime_type=mime)
                set_db(u_id, "model_id", m_id)
                set_db(u_id, "selected_voice", "CLONED")
                set_db(u_id, "state", "IDLE")
                if status_msg and "result" in status_msg:
                    delete_msg(c_id, status_msg["result"]["message_id"])
                send_msg(c_id, "🎉 <b>Your custom voice profile is active & saved permanently!</b>", reply_markup=get_main_kb())
            except Exception as e:
                print(f"Clone error: {e}")
                if status_msg and "result" in status_msg:
                    delete_msg(c_id, status_msg["result"]["message_id"])
                send_msg(c_id, "❌ <b>Failed to clone voice sample.</b> Record 15-30s in a quiet room.")
            return jsonify({"status": "ok"}), 200

        raw_text = msg.get("text", "").strip()

    if not c_id or not u_id or not raw_text: return jsonify({"status": "ok"}), 200

    is_admin = str(u_id) == str(ADMIN_ID)
    clean_btn = re.sub(r'[^\w\s]', '', raw_text).strip().lower()

    if get_db(u_id, "state") == "AWAITING_CUSTOM_STARS":
        if raw_text.isdigit() and int(raw_text) >= 40:
            set_db(u_id, "state", "IDLE")
            send_invoice(c_id, int(raw_text))
        else:
            send_msg(c_id, "⚠️ Invalid input. Enter 40 or higher:")
        return jsonify({"status": "ok"}), 200

    if raw_text in ["/start", "i Help", "Help"] or clean_btn in ["start", "help"]:
        get_user_credits(u_id)
        welcome = (
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
        send_msg(c_id, welcome, reply_markup=get_main_kb())
        return jsonify({"status": "ok"}), 200

    if "clone my voice" in clean_btn:
        set_db(u_id, "state", "AWAITING_VOICE")
        send_msg(c_id, "🎙 <b>Send a clear 15-30 second voice note now.</b>")
        return jsonify({"status": "ok"}), 200

    if "voice settings" in clean_btn:
        send_msg(c_id, "🎧 <b>Select your voice engine:</b>", reply_markup=get_voice_kb(u_id))
        return jsonify({"status": "ok"}), 200

    if "buy credits" in clean_btn or raw_text == "/buy":
        send_msg(c_id, "💳 <b>Select a package:</b>", reply_markup=get_buy_kb())
        return jsonify({"status": "ok"}), 200

    if "my status" in clean_btn:
        c, m, v = get_user_credits(u_id), get_db(u_id, "model_id"), get_db(u_id, "selected_voice", "CLONED")
        st = f"👑 <b>{'Admin' if is_admin else 'User'} Status</b>\n\n🎙 Cloned Profile: {'✅ Saved' if m else '❌ Missing'}\n🎧 Engine: <code>{v.upper()}</code>\n🪙 Credits: {'Unlimited' if is_admin else f'{c} Remaining'}"
        send_msg(c_id, st, reply_markup=get_main_kb())
        return jsonify({"status": "ok"}), 200

    if "storytelling tips" in clean_btn:
        send_msg(c_id, "💡 <b>Tips:</b> Use commas for natural pauses, keep hooks sharp in the first 3 seconds.", reply_markup=get_main_kb())
        return jsonify({"status": "ok"}), 200

    # TTS Generation
    words = raw_text.split()
    if len(words) > 300:
        send_msg(c_id, f"⚠️ <b>Script too long!</b> Your text contains {len(words)} words. The maximum limit is 300 words per generation.")
        return jsonify({"status": "ok"}), 200

    if not is_admin and get_user_credits(u_id) <= 0:
        send_msg(c_id, "❌ <b>Out of credits.</b> Buy more using Telegram Stars.", reply_markup=get_buy_kb())
        return jsonify({"status": "ok"}), 200

    # Send processing message & action state
    send_chat_action(c_id, "record_voice")
    proc_msg = send_msg(c_id, "⚡ <b>Generating audio...</b> Please wait a few seconds.")

    try:
        vid = get_active_voice_id(u_id)
        audio = generate_voice(prepare_text(raw_text), ref_id=vid)
        send_audio(c_id, audio)
        if not is_admin: deduct_credit(u_id)
    except Exception as e:
        print(f"TTS Error: {e}")
        send_msg(c_id, "❌ <b>Voice generation failed.</b> Try again later.")
    finally:
        if proc_msg and "result" in proc_msg:
            delete_msg(c_id, proc_msg["result"]["message_id"])

    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
    
