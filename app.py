import os
import datetime
import requests
from flask import Flask, request
import telebot
from telebot import types

# --- ENVIRONMENT VARIABLES & CONFIGURATION ---
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("BOT_TOKEN")
FISH_AUDIO_API_KEY = os.environ.get("FISH_AUDIO_API_KEY")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

MAX_WORDS = 300
INITIAL_TRIALS = 3
DAILY_ALLOWANCE = 2

# Initialize Telebot with HTML parse mode
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

# Simple in-memory state storage
user_db = {}


def get_user_data(chat_id: int) -> dict:
    today = datetime.date.today().isoformat()
    if chat_id not in user_db:
        user_db[chat_id] = {
            "voice_id": None,
            "welcome_trials": INITIAL_TRIALS,
            "daily_trials": DAILY_ALLOWANCE,
            "last_active_date": today
        }
        return user_db[chat_id]
    
    user = user_db[chat_id]
    if user["last_active_date"] != today:
        user["daily_trials"] = DAILY_ALLOWANCE
        user["last_active_date"] = today
        
    return user


def validate_user_request(chat_id: int, text: str):
    words = text.strip().split()
    word_count = len(words)
    
    if word_count > MAX_WORDS:
        return False, None, (
            f"⚠️ <b>Script too long!</b>\n\n"
            f"Regular users can convert up to <b>{MAX_WORDS} words</b> per request.\n"
            f"Your text contains <b>{word_count} words</b>."
        )

    user = get_user_data(chat_id)
    if user["welcome_trials"] > 0:
        return True, "welcome", None
    elif user["daily_trials"] > 0:
        return True, "daily", None
    else:
        return False, None, (
            "⚠️ <b>Daily limit reached!</b>\n\n"
            "You have used all your free generations for today. "
            "Your 2 free daily trials reset tomorrow!"
        )


def deduct_user_trial(chat_id: int, trial_type: str):
    user = user_db[chat_id]
    if trial_type == "welcome":
        user["welcome_trials"] -= 1
    elif trial_type == "daily":
        user["daily_trials"] -= 1


# --- FISH AUDIO API INTEGRATION ---

def send_admin_alert(message_text: str):
    if ADMIN_ID != 0:
        try:
            bot.send_message(ADMIN_ID, message_text)
        except Exception as e:
            print(f"--> Failed to send admin alert: {e}", flush=True)


def create_fish_voice_model(audio_bytes: bytes, filename: str = "voice.mp3") -> str:
    url = "https://api.fish.audio/model"
    headers = {"Authorization": f"Bearer {FISH_AUDIO_API_KEY}"}
    data = {
        "type": "tts",
        "title": "User Cloned Voice",
        "train_mode": "fast",
        "visibility": "private"
    }
    files = {"voices": (filename, audio_bytes, "audio/mpeg")}

    try:
        response = requests.post(url, headers=headers, data=data, files=files, timeout=30)
        if response.status_code in [200, 201]:
            return response.json().get("id")
        elif response.status_code in [401, 402]:
            send_admin_alert("🚨 <b>CRITICAL ALERT:</b> Fish Audio account is OUT OF CREDITS or API Key is invalid!")
            return None
        else:
            print(f"--> Model creation failed: {response.status_code} - {response.text}", flush=True)
            return None
    except Exception as e:
        print(f"--> Error calling Fish Audio Model API: {e}", flush=True)
        return None


def generate_speech_fish(text: str, reference_id: str, chat_id: int) -> bytes:
    url = "https://api.fish.audio/v1/tts"
    headers = {
        "Authorization": f"Bearer {FISH_AUDIO_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "text": text,
        "reference_id": reference_id,
        "format": "mp3"
    }

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=60)

        if response.status_code in [401, 402]:
            send_admin_alert("🚨 <b>CRITICAL ALERT:</b> Fish Audio balance is empty!")
            bot.send_message(
                chat_id, 
                "⚠️ Service is temporarily undergoing quick maintenance. Please try again in a few minutes!"
            )
            return None

        if response.status_code == 200:
            return response.content

        bot.send_message(chat_id, "⚠️ Service is temporarily busy. Please try again in a few minutes!")
        return None

    except Exception as e:
        print(f"--> Error calling Fish Audio TTS API: {e}", flush=True)
        bot.send_message(chat_id, "⚠️ Service is temporarily busy. Please try again in a few minutes!")
        return None


# --- KEYBOARDS & UI ---

def get_main_keyboard():
    markup = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    btn_clone = types.KeyboardButton("🎙️ Clone My Voice")
    btn_buy = types.KeyboardButton("💳 Buy Credits")
    btn_status = types.KeyboardButton("📊 My Status")
    btn_tips = types.KeyboardButton("📝 Storytelling Tips")
    btn_help = types.KeyboardButton("ℹ️ Help")
    
    markup.add(btn_clone, btn_buy)
    markup.add(btn_status, btn_tips)
    markup.add(btn_help)
    return markup


# --- TELEGRAM BOT HANDLERS WITH DEBUG LOGGING ---

@bot.message_handler(commands=['start'])
def handle_start(message):
    print(f"--> Received /start command from chat_id: {message.chat.id}", flush=True)
    get_user_data(message.chat.id)
    welcome_text = (
        "Welcome to <b>StoryVoice AI Studio!</b> 🎙✨\n\n"
        "Your ultimate AI Voice Cloning & Storytelling Assistant.\n\n"
        "⚡ <b>Quick Start:</b>\n"
        "1️⃣ Tap <b>'🎙️ Clone My Voice'</b> and send a clear 10–15s voice note.\n"
        "2️⃣ Send any text script or story (up to <b>300 words</b>).\n"
        "3️⃣ Receive your custom audio narration in seconds!\n\n"
        "🎁 <b>Free Tier:</b> You get <b>3 Welcome Bonus Credits</b> + <b>2 FREE generations</b> every single day!"
    )
    try:
        bot.send_message(message.chat.id, welcome_text, reply_markup=get_main_keyboard())
        print("--> Sent welcome message successfully!", flush=True)
    except Exception as e:
        print(f"--> ERROR sending welcome message: {e}", flush=True)


@bot.message_handler(func=lambda msg: msg.text == "🎙️ Clone My Voice")
def handle_clone_button(message):
    print(f"--> Received 'Clone My Voice' from chat_id: {message.chat.id}", flush=True)
    try:
        bot.send_message(
            message.chat.id,
            "🎙 <b>Send me a clear voice note!</b>\n\n"
            "• Duration: 10 to 15 seconds\n"
            "• Environment: Quiet background\n"
            "• Tip: Speak naturally at a steady pace."
        )
    except Exception as e:
        print(f"--> ERROR in handle_clone_button: {e}", flush=True)


@bot.message_handler(func=lambda msg: msg.text == "📊 My Status")
def handle_status_button(message):
    print(f"--> Received 'My Status' from chat_id: {message.chat.id}", flush=True)
    try:
        user = get_user_data(message.chat.id)
        has_voice = "✅ Saved" if user["voice_id"] else "❌ Not Cloned Yet"
        
        status_text = (
            "📊 <b>Your Account Status</b>\n\n"
            f"🎙️ <b>Voice Model:</b> {has_voice}\n"
            f"🎁 <b>Welcome Credits:</b> {user['welcome_trials']} left\n"
            f"🔄 <b>Daily Free Credits:</b> {user['daily_trials']}/2 left\n"
            f"📝 <b>Max Script Length:</b> {MAX_WORDS} words\n\n"
            "Need more credits or longer scripts? Tap <b>'💳 Buy Credits'</b>!"
        )
        bot.send_message(message.chat.id, status_text)
    except Exception as e:
        print(f"--> ERROR in handle_status_button: {e}", flush=True)


@bot.message_handler(func=lambda msg: msg.text == "💳 Buy Credits")
def handle_buy_button(message):
    try:
        bot.send_message(
            message.chat.id,
            "💳 <b>Upgrade Your Tier</b>\n\n"
            "Get access to unlimited generations, longer scripts, and priority processing.\n\n"
            "• <b>Minimum Tier:</b> 30 Telegram Stars"
        )
    except Exception as e:
        print(f"--> ERROR in handle_buy_button: {e}", flush=True)


@bot.message_handler(func=lambda msg: msg.text == "📝 Storytelling Tips")
def handle_tips_button(message):
    try:
        bot.send_message(
            message.chat.id,
            "💡 <b>Storytelling & Social Media Tips:</b>\n\n"
            "1. <b>Hooks:</b> Keep the first 3 seconds exciting!\n"
            "2. <b>Punctuation:</b> Use commas and periods to guide the AI's natural pauses.\n"
            "3. <b>Visuals:</b> Pair your generated audio with book quotes or aesthetic video loops!"
        )
    except Exception as e:
        print(f"--> ERROR in handle_tips_button: {e}", flush=True)


@bot.message_handler(func=lambda msg: msg.text == "ℹ️️ Help")
def handle_help_button(message):
    try:
        bot.send_message(
            message.chat.id,
            "ℹ️ <b>Need Help?</b>\n\n"
            "1. Click <b>'🎙️ Clone My Voice'</b> and record your voice.\n"
            "2. Type or paste your story text (up to 300 words).\n"
            "3. The bot will automatically generate audio in your cloned voice!"
        )
    except Exception as e:
        print(f"--> ERROR in handle_help_button: {e}", flush=True)


# --- VOICE NOTE HANDLER ---

@bot.message_handler(content_types=['voice', 'audio'])
def handle_voice_input(message):
    chat_id = message.chat.id
    print(f"--> Received voice note from chat_id: {chat_id}", flush=True)
    bot.send_message(chat_id, "⏳ <b>Processing your voice sample...</b> Please wait a moment.")
    
    try:
        file_info = bot.get_file(message.voice.file_id if message.voice else message.audio.file_id)
        downloaded_file = bot.download_file(file_info.file_path)

        reference_id = create_fish_voice_model(downloaded_file)

        if reference_id:
            user = get_user_data(chat_id)
            user["voice_id"] = reference_id
            bot.send_message(
                chat_id,
                "🎉 <b>Voice cloned successfully!</b>\n\n"
                "Now, send me any text script (up to 300 words) and I will read it in your voice!"
            )
        else:
            bot.send_message(chat_id, "❌ Failed to process audio sample. Please try recording again in a quiet environment.")

    except Exception as e:
        print(f"--> Error handling voice file: {e}", flush=True)
        bot.send_message(chat_id, "❌ Error receiving audio. Please try sending it again.")


# --- TEXT SCRIPT HANDLER ---

@bot.message_handler(func=lambda msg: True, content_types=['text'])
def handle_text_generation(message):
    chat_id = message.chat.id
    text = message.text.strip()
    print(f"--> Received text generation request from chat_id: {chat_id}", flush=True)
    
    user = get_user_data(chat_id)

    if not user["voice_id"]:
        bot.send_message(
            chat_id,
            "⚠️ <b>No Voice Model Found!</b>\n\n"
            "Please tap <b>'🎙️ Clone My Voice'</b> and send a 10–15s voice note first!"
        )
        return

    allowed, trial_type, error_msg = validate_user_request(chat_id, text)
    if not allowed:
        bot.send_message(chat_id, error_msg)
        return

    bot.send_message(chat_id, "🗣️ <b>Generating your audio narration...</b>")

    audio_bytes = generate_speech_fish(text, user["voice_id"], chat_id)

    if audio_bytes:
        deduct_user_trial(chat_id, trial_type)
        bot.send_audio(chat_id, audio_bytes, caption="✨ Here is your generated voiceover!")


# --- FLASK WEBHOOK ROUTES ---

@app.route('/', methods=['GET'])
def index():
    return "Bot service is running perfectly!", 200


@app.route('/webhook', methods=['POST'])
def webhook():
    if request.is_json:
        json_string = request.get_data().decode('utf-8')
        print(f"--> Incoming Webhook Payload: {json_string}", flush=True)
        try:
            update = telebot.types.Update.de_json(json_string)
            bot.process_new_updates([update])
            print("--> Update processed successfully!", flush=True)
        except Exception as e:
            print(f"--> ERROR processing update: {e}", flush=True)
        return 'OK', 200
    return 'Invalid', 400


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
            
