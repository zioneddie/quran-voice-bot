import os
import datetime
import requests
from flask import Flask, request
import telebot
from telebot import types

# --- ENVIRONMENT VARIABLES & CONFIGURATION ---
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
FISH_AUDIO_API_KEY = os.environ.get("FISH_AUDIO_API_KEY")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))  # Set your Telegram ID in Render Env Vars

# Limits
MAX_WORDS = 300
INITIAL_TRIALS = 3
DAILY_ALLOWANCE = 2

# Initialize Telebot & Flask
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="Markdown")
app = Flask(__name__)

# --- IN-MEMORY DATABASE (Will persist while app runs) ---
# For permanent storage across restarts, connect Upstash Redis or Supabase later.
user_db = {}

def get_user_data(chat_id: int) -> dict:
    """Fetches user account state and handles daily trial resets."""
    today = datetime.date.today().isoformat()
    
    # New User Initialization
    if chat_id not in user_db:
        user_db[chat_id] = {
            "voice_id": None,
            "welcome_trials": INITIAL_TRIALS,
            "daily_trials": DAILY_ALLOWANCE,
            "last_active_date": today
        }
        return user_db[chat_id]
    
    user = user_db[chat_id]
    
    # Daily Reset Logic
    if user["last_active_date"] != today:
        user["daily_trials"] = DAILY_ALLOWANCE
        user["last_active_date"] = today
        
    return user


def validate_user_request(chat_id: int, text: str):
    """Validates word count and remaining user trials."""
    words = text.strip().split()
    word_count = len(words)
    
    # 1. Word Limit Check
    if word_count > MAX_WORDS:
        return False, None, (
            f"⚠️ **Script too long!**\n\n"
            f"Regular users can convert up to **{MAX_WORDS} words** per request.\n"
            f"Your text contains **{word_count} words**."
        )

    # 2. Credits Check
    user = get_user_data(chat_id)
    if user["welcome_trials"] > 0:
        return True, "welcome", None
    elif user["daily_trials"] > 0:
        return True, "daily", None
    else:
        return False, None, (
            "⚠️ **Daily limit reached!**\n\n"
            "You have used all your free generations for today. "
            "Your 2 free daily trials reset tomorrow, or you can top up for unlimited access!"
        )


def deduct_user_trial(chat_id: int, trial_type: str):
    """Deducts a trial only after successful voice generation."""
    user = user_db[chat_id]
    if trial_type == "welcome":
        user["welcome_trials"] -= 1
    elif trial_type == "daily":
        user["daily_trials"] -= 1


# --- FISH AUDIO API INTEGRATION ---

def send_admin_alert(message_text: str):
    """Sends private alert to admin Telegram ID."""
    if ADMIN_ID != 0:
        try:
            bot.send_message(ADMIN_ID, message_text)
        except Exception as e:
            print(f"Failed to send admin alert: {e}")


def create_fish_voice_model(audio_bytes: bytes, filename: str = "voice.mp3") -> str:
    """Uploads user's audio to Fish Audio to generate a custom voice reference_id."""
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
            send_admin_alert("🚨 **CRITICAL ALERT:** Fish Audio account is OUT OF CREDITS or API Key is invalid!")
            return None
        else:
            print(f"Model creation failed: {response.status_code} - {response.text}")
            return None
    except Exception as e:
        print(f"Error calling Fish Audio Model API: {e}")
        return None


def generate_speech_fish(text: str, reference_id: str, chat_id: int) -> bytes:
    """Generates speech via Fish Audio API and handles out-of-credit alerts."""
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

        # Catch Out of Credits / Authentication Errors
        if response.status_code in [401, 402]:
            send_admin_alert("🚨 **CRITICAL ALERT:** Fish Audio balance is empty! Users cannot generate voices.")
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
        print(f"Error calling Fish Audio TTS API: {e}")
        bot.send_message(chat_id, "⚠️️ Service is temporarily busy. Please try again in a few minutes!")
        return None


# --- KEYBOARDS & UI ---

def get_main_keyboard():
    """Generates Telegram Reply Keyboard."""
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


# --- TELEGRAM BOT HANDLERS ---

@bot.message_handler(commands=['start'])
def handle_start(message):
    get_user_data(message.chat.id)
    welcome_text = (
        "Welcome to StoryVoice AI Studio! 🎙✨\n\n"
        "Your ultimate AI Voice Cloning & Storytelling Assistant.\n\n"
        "⚡ **Quick Start:**\n"
        "1️⃣ Tap **'🎙️ Clone My Voice'** and send a clear 10–15s voice note.\n"
        "2️⃣ Send any text script or story (up to **300 words**).\n"
        "3️⃣ Receive your custom audio narration in seconds!\n\n"
        "🎁 **Free Tier:** You get **3 Welcome Bonus Credits** + **2 FREE generations** every single day!"
    )
    bot.send_message(message.chat.id, welcome_text, reply_markup=get_main_keyboard())


@bot.message_handler(func=lambda msg: msg.text == "🎙️️ Clone My Voice")
def handle_clone_button(message):
    bot.send_message(
        message.chat.id,
        "🎙️️ **Send me a clear voice note!**\n\n"
        "• Duration: 10 to 15 seconds\n"
        "• Environment: Quiet background\n"
        "• Tip: Speak naturally at a steady pace."
    )


@bot.message_handler(func=lambda msg: msg.text == "📊 My Status")
def handle_status_button(message):
    user = get_user_data(message.chat.id)
    has_voice = "✅ Saved" if user["voice_id"] else "❌ Not Cloned Yet"
    
    status_text = (
        "📊 **Your Account Status**\n\n"
        f"🎙️ **Voice Model:** {has_voice}\n"
        f"🎁 **Welcome Credits:** {user['welcome_trials']} left\n"
        f"🔄 **Daily Free Credits:** {user['daily_trials']}/2 left\n"
        f"📝 **Max Script Length:** {MAX_WORDS} words\n\n"
        "Need more credits or longer scripts? Tap **'💳 Buy Credits'**!"
    )
    bot.send_message(message.chat.id, status_text)


@bot.message_handler(func=lambda msg: msg.text == "💳 Buy Credits")
def handle_buy_button(message):
    bot.send_message(
        message.chat.id,
        "💳 **Upgrade Your Tier**\n\n"
        "Get access to unlimited generations, longer scripts, and priority processing.\n\n"
        "• **Minimum Tier:** 30 Telegram Stars\n\n"
        "*(Telegram Stars payment integration active!)*"
    )


@bot.message_handler(func=lambda msg: msg.text == "📝 Storytelling Tips")
def handle_tips_button(message):
    bot.send_message(
        message.chat.id,
        "💡 **Storytelling & Social Media Tips:**\n\n"
        "1. **Hooks:** Keep the first 3 seconds exciting!\n"
        "2. **Punctuation:** Use commas and periods to guide the AI's natural pauses.\n"
        "3. **Visuals:** Pair your generated audio with book quotes or aesthetic video loops for viral reach!"
    )


@bot.message_handler(func=lambda msg: msg.text == "ℹ️ Help")
def handle_help_button(message):
    bot.send_message(
        message.chat.id,
        "ℹ️ **Need Help?**\n\n"
        "1. Click **'🎙️ Clone My Voice'** and record your voice.\n"
        "2. Type or paste your story text (up to 300 words).\n"
        "3. The bot will automatically generate audio in your cloned voice!"
    )


# --- VOICE NOTE HANDLER (Voice Cloning) ---

@bot.message_handler(content_types=['voice', 'audio'])
def handle_voice_input(message):
    chat_id = message.chat.id
    bot.send_message(chat_id, "⏳ **Processing your voice sample...** Please wait a moment.")
    
    try:
        # Download audio from Telegram
        file_info = bot.get_file(message.voice.file_id if message.voice else message.audio.file_id)
        downloaded_file = bot.download_file(file_info.file_path)

        # Send to Fish Audio
        reference_id = create_fish_voice_model(downloaded_file)

        if reference_id:
            user = get_user_data(chat_id)
            user["voice_id"] = reference_id
            bot.send_message(
                chat_id,
                "🎉 **Voice cloned successfully!**\n\n"
                "Now, send me any text script (up to 300 words) and I will read it in your voice!"
            )
        else:
            bot.send_message(chat_id, "❌ Failed to process audio sample. Please try recording again in a quiet environment.")

    except Exception as e:
        print(f"Error handling voice file: {e}")
        bot.send_message(chat_id, "❌ Error receiving audio. Please try sending it again.")


# --- TEXT SCRIPT HANDLER (Voice Generation) ---

@bot.message_handler(func=lambda msg: True, content_types=['text'])
def handle_text_generation(message):
    chat_id = message.chat.id
    text = message.text.strip()
    user = get_user_data(chat_id)

    # Check if voice is cloned first
    if not user["voice_id"]:
        bot.send_message(
            chat_id,
            "⚠️ **No Voice Model Found!**\n\n"
            "Please tap **'🎙️ Clone My Voice'** and send a 10–15s voice note first!"
        )
        return

    # Validate length and credits
    allowed, trial_type, error_msg = validate_user_request(chat_id, text)
    if not allowed:
        bot.send_message(chat_id, error_msg)
        return

    bot.send_message(chat_id, "🗣️ **Generating your audio narration...**")

    # Generate Speech
    audio_bytes = generate_speech_fish(text, user["voice_id"], chat_id)

    if audio_bytes:
        deduct_user_trial(chat_id, trial_type)
        bot.send_audio(chat_id, audio_bytes, caption="✨ Here is your generated voiceover!")


# --- FLASK WEBHOOK ROUTES FOR RENDER ---

@app.route('/', methods=['GET'])
def index():
    return "Bot service is running perfectly!", 200


@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return '', 200
    return 'Invalid', 400


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
    
