import os
import re
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# --- CONFIGURATION ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
REPLICATE_API_TOKEN = os.getenv("REPLICATE_API_TOKEN")
# Default speaker reference audio URL (replace with your fallback voice sample if needed)
DEFAULT_SPEAKER_WAV = os.getenv("DEFAULT_SPEAKER_WAV", "https://replicate.delivery/pbxt/Jt75wUx3f2xP3/sample.wav")

TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


# --- TEXT PREPROCESSING FOR NATURAL VOICE ---
def prepare_text_for_tts(text: str) -> str:
    """
    Cleans and adds expressive punctuation/pauses 
    to make XTTS-v2 sound less robotic.
    """
    if not text:
        return ""
    
    # 1. Clean extra spaces
    text = re.sub(r'\s+', ' ', text.strip())
    
    # 2. Add dramatic pauses after greetings
    text = re.sub(r'^(hello|hi|hey|welcome|listen)([\s!,.?])', r'\1... ', text, flags=re.IGNORECASE)
    
    # 3. Add micro-pauses before common connecting words
    text = re.sub(r',\s+(but|however|so|and|then)\b', r'... \1', text, flags=re.IGNORECASE)
    
    # 4. Convert dashes into pauses
    text = re.sub(r'\s*--\s*|\s*—\s*', '... ', text)
    
    # 5. Ensure sentence ends with punctuation so pitch resets correctly
    if text and text[-1] not in '.!?':
        text += '.'
        
    return text


# --- REPLICATE API CALL ---
def generate_voice(text: str, speaker_wav: str) -> str:
    """
    Calls Replicate XTTS-v2 API and returns the generated audio URL.
    """
    url = "https://api.replicate.com/v1/predictions"
    headers = {
        "Authorization": f"Bearer {REPLICATE_API_TOKEN}",
        "Content-Type": "application/json"
    }
    
    # Payload for XTTS-v2
    payload = {
        "version": "6841db4583d3d83f72109f13a49648e1a144130509865c977431e780c10b7a42",  # lucataco/xtts-v2
        "input": {
            "text": text,
            "speaker": speaker_wav,
            "language": "en"
        }
    }
    
    # Post request to start prediction
    response = requests.post(url, json=payload, headers=headers)
    response_data = response.json()
    
    if "id" not in response_data:
        raise Exception(f"Replicate API Error: {response_data}")
        
    prediction_id = response_data["id"]
    get_url = f"https://api.replicate.com/v1/predictions/{prediction_id}"
    
    # Poll until execution finishes
    import time
    while True:
        poll_res = requests.get(get_url, headers=headers).json()
        status = poll_res.get("status")
        
        if status == "succeeded":
            # Returns the audio file URL
            return poll_res.get("output")
        elif status == "failed":
            raise Exception("Voice generation failed on Replicate.")
            
        time.sleep(1)


# --- TELEGRAM HELPER FUNCTIONS ---
def send_audio(chat_id: int, audio_url: str):
    """Sends voice/audio message back to Telegram user."""
    url = f"{TELEGRAM_API_URL}/sendAudio"
    payload = {
        "chat_id": chat_id,
        "audio": audio_url
    }
    requests.post(url, json=payload)

def send_message(chat_id: int, text: str):
    """Sends text message back to Telegram user."""
    url = f"{TELEGRAM_API_URL}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text
    }
    requests.post(url, json=payload)


# --- MAIN WEBHOOK ENDPOINT ---
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    
    if not data or "message" not in data:
        return jsonify({"status": "ignored"}), 200

    message = data["message"]
    chat_id = message["chat"]["id"]
    raw_text = message.get("text", "")

    # Handle /start command
    if raw_text == "/start":
        send_message(chat_id, "Welcome! Send me any text and I will generate natural AI voice audio for you.")
        return jsonify({"status": "ok"}), 200

    if not raw_text:
        send_message(chat_id, "Please send a text message.")
        return jsonify({"status": "ok"}), 200

    try:
        # 1. Process text to add pauses/punctuation for natural flow
        enhanced_text = prepare_text_for_tts(raw_text)

        # 2. Call Replicate API to generate audio
        audio_url = generate_voice(enhanced_text, DEFAULT_SPEAKER_WAV)

        # 3. Send audio back to Telegram
        send_audio(chat_id, audio_url)

    except Exception as e:
        print(f"Error processing message: {e}")
        send_message(chat_id, "Sorry, something went wrong while generating the voice.")

    return jsonify({"status": "ok"}), 200


@app.route('/', methods=['GET'])
def home():
    return "Bot is running perfectly!", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
    
