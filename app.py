import os
import datetime
import requests

# --- CONFIGURATION ---
MAX_WORDS = 300
INITIAL_TRIALS = 3
DAILY_ALLOWANCE = 2

# Simulated database dictionary (replace with Upstash Redis / Supabase calls when connected)
user_db = {}

def get_user_data(chat_id: int) -> dict:
    """Fetches user limits and handles daily resets."""
    today = datetime.date.today().isoformat()
    
    # 1. New User Setup (3 Welcome Trials)
    if chat_id not in user_db:
        user_db[chat_id] = {
            "welcome_trials": INITIAL_TRIALS,
            "daily_trials": DAILY_ALLOWANCE,
            "last_active_date": today
        }
        return user_db[chat_id]
    
    user = user_db[chat_id]
    
    # 2. Daily Allowance Reset Logic (Runs on new day)
    if user["last_active_date"] != today:
        user["daily_trials"] = DAILY_ALLOWANCE
        user["last_active_date"] = today
        
    return user


def validate_user_request(chat_id: int, text: str):
    """Checks word limit and remaining generation credits."""
    # --- WORD COUNT CHECK ---
    words = text.strip().split()
    word_count = len(words)
    
    if word_count > MAX_WORDS:
        return False, None, (
            f"⚠️ **Script too long!**\n\n"
            f"Regular users can convert up to **{MAX_WORDS} words** per request.\n"
            f"Your text contains **{word_count} words**."
        )

    # --- TRIAL USAGE CHECK ---
    user = get_user_data(chat_id)
    
    if user["welcome_trials"] > 0:
        return True, "welcome", None
    elif user["daily_trials"] > 0:
        return True, "daily", None
    else:
        return False, None, (
            "⚠️ **Daily limit reached!**\n\n"
            "You've used all your free generations for today. "
            "Your 2 free trials will reset tomorrow, or you can purchase Stars for unlimited access!"
        )


def deduct_user_trial(chat_id: int, trial_type: str):
    """Deducts a trial only after successful voice generation."""
    user = user_db[chat_id]
    if trial_type == "welcome":
        user["welcome_trials"] -= 1
    elif trial_type == "daily":
        user["daily_trials"] -= 1


# --- UPDATED VOICE GENERATION HANDLER ---
def handle_tts_request(chat_id: int, text: str, reference_id: str):
    # 1. Validate limits before calling API
    allowed, trial_type, error_msg = validate_user_request(chat_id, text)
    if not allowed:
        send_telegram_message(chat_id, error_msg)
        return

    # 2. Call Fish Audio API
    audio_bytes = generate_speech_fish(text, reference_id, chat_id)

    # 3. On success, deduct trial and send audio
    if audio_bytes:
        deduct_user_trial(chat_id, trial_type)
        send_telegram_audio(chat_id, audio_bytes)
        
