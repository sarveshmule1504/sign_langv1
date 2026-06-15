import cv2
import mediapipe as mp
import os
import time
import threading
import subprocess
from google.oauth2 import service_account
from google.cloud import texttospeech
from flask import Flask, Response

# --- OLED Setup Libraries (1.3" 128x64 SH1106 over I2C) ---
try:
    from luma.core.interface.serial import i2c
    from luma.core.render import canvas
    from luma.oled.device import sh1106
    OLED_LIB_AVAILABLE = True
except ImportError:
    OLED_LIB_AVAILABLE = False

OLED_I2C_PORT = 1
OLED_I2C_ADDRESSES = (0x3C, 0x3D)
OLED_MAX_CHARS_PER_LINE = 16
OLED_MARGIN_X = 2
OLED_MARGIN_Y = 1
OLED_LINE_HEIGHT = 9

# Global dictionary for maintaining the data to be shown on OLED
oled_data = {
    "hand_found": "NO",
    "curr_gesture": "None",
    "last_message": "Show hand to detect"
}
oled_data_lock = threading.Lock()


def wrap_oled_text(text, max_chars=OLED_MAX_CHARS_PER_LINE, max_lines=3):
    """Wrap text to fit the 128px-wide SH1106 display."""
    words = text.split()
    lines = []
    current = ""

    for word in words:
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= max_chars:
            current = candidate
        elif current:
            lines.append(current)
            current = word[:max_chars]
        else:
            lines.append(word[:max_chars])
            current = ""

        if len(lines) >= max_lines:
            break

    if current and len(lines) < max_lines:
        lines.append(current)

    if len(lines) == max_lines and len(" ".join(words)) > sum(len(line) for line in lines):
        lines[-1] = (lines[-1][: max_chars - 2] + "..") if len(lines[-1]) > 2 else ".."

    return lines


def init_oled_device():
    """Connect to the SH1106 OLED over I2C, trying common addresses 0x3C and 0x3D."""
    last_error = None
    for address in OLED_I2C_ADDRESSES:
        try:
            serial = i2c(port=OLED_I2C_PORT, address=address)
            device = sh1106(serial)
            print(f"OLED display initialized on I2C bus {OLED_I2C_PORT} at 0x{address:02X}")
            return device
        except Exception as exc:
            last_error = exc
    raise last_error


def draw_oled_screen(draw, hand_found, gesture, message):
    """Render a evenly spaced layout that fits the 128x64 SH1106 screen."""
    x = OLED_MARGIN_X
    y = OLED_MARGIN_Y

    draw.text((x, y), "SIGN LANGUAGE", fill="white")
    y += OLED_LINE_HEIGHT

    draw.line((x, y, 127 - OLED_MARGIN_X, y), fill="white")
    y += 2

    draw.text((x, y), f"Hand: {hand_found}", fill="white")
    y += OLED_LINE_HEIGHT

    gesture_line = gesture if len(gesture) <= OLED_MAX_CHARS_PER_LINE else gesture[: OLED_MAX_CHARS_PER_LINE - 2] + ".."
    draw.text((x, y), f"Sign: {gesture_line}", fill="white")
    y += OLED_LINE_HEIGHT

    max_message_lines = max(1, (64 - y) // OLED_LINE_HEIGHT)
    for line in wrap_oled_text(message, max_lines=max_message_lines):
        draw.text((x, y), line, fill="white")
        y += OLED_LINE_HEIGHT


def oled_updater_thread():
    """Handles continuous writing to the physical OLED display."""
    if not OLED_LIB_AVAILABLE:
        print("Note: luma.oled library not installed correctly, skipping OLED hardware thread.")
        return
    
    device = None
    print("Starting OLED daemon thread...")
    
    while True:
        # Try to instantiate device if not ready (e.g. not plugged in yet)
        if device is None:
            try:
                device = init_oled_device()
            except Exception:
                device = None
                time.sleep(5)
                continue

        try:
            with oled_data_lock:
                hand_found = oled_data["hand_found"]
                gesture = oled_data["curr_gesture"]
                message = oled_data["last_message"]

            with canvas(device) as draw:
                draw_oled_screen(draw, hand_found, gesture, message)

        except Exception:
            # If failed write (unplugged, etc), reset and try to reconnect later
            device = None
            
        time.sleep(0.5)

# Helper function to safely update global OLED state from video_loop
def update_oled_values(hand=None, gesture=None, msg=None):
    with oled_data_lock:
        if hand is not None:
            oled_data["hand_found"] = hand
        if gesture is not None:
            oled_data["curr_gesture"] = gesture
        if msg is not None:
            oled_data["last_message"] = msg


# Automatically set Display environment variables for Wayland/X11 compatibility
os.environ["DISPLAY"] = ":0"
os.environ["WAYLAND_DISPLAY"] = "wayland-0"

# Initialize Flask App
flask_app = Flask(__name__)

# Global frames for Flask streaming
latest_frame = None
frame_lock = threading.Lock()

# Initialize Google Cloud Text-to-Speech
creds_path = "/home/raspi/Desktop/asv-tech-894372079dd5 (1).json"
tts_client = None

try:
    if os.path.exists(creds_path):
        credentials = service_account.Credentials.from_service_account_file(creds_path)
        tts_client = texttospeech.TextToSpeechClient(credentials=credentials)
        print("Google Cloud TTS initialized successfully!")
    else:
        print(f"Warning: Service account file not found at {creds_path}. Speech is disabled.")
except Exception as e:
    print(f"Warning: Could not initialize Google Cloud TTS ({e}). Speech is disabled.")

# Sentence mappings for each gesture
sentence_map = {
    "Hello / Yes": "Hello, how can I help you today?",
    "Thumbs Up": "That is absolutely fantastic!",
    "Need Help (Fist)": "Excuse me, I need some assistance, please.",
    "Thank You (Peace)": "Thank you very much!",
    "Pointing / One": "Please take a look in this direction.",
    "Rock On / Spider-Man": "This is totally amazing!",
    "OK Sign": "Understood, everything is fine.",
    "Awesome / Three": "That is wonderful!",
    "Beckon / Two": "Could you please come here for a moment?"
}

# Thread-safety lock and busy flag to prevent overlapping audio
is_speaking = False
speech_lock = threading.Lock()

def speak_text(text):
    """Synthesizes and plays a complete sentence asynchronously."""
    global is_speaking
    if tts_client is None:
        return
    
    with speech_lock:
        if is_speaking:
            return
        is_speaking = True
    
    def run_tts():
        global is_speaking
        try:
            time.sleep(0.5)
            sentence = sentence_map.get(text, text)
            print(f"Speaking: {sentence}")
            
            # Update OLED that message was spoken
            update_oled_values(msg=sentence)
            
            synthesis_input = texttospeech.SynthesisInput(text=sentence)
            voice = texttospeech.VoiceSelectionParams(
                language_code="en-US",
                ssml_gender=texttospeech.SsmlVoiceGender.FEMALE
            )
            audio_config = texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.LINEAR16
            )
            
            response = tts_client.synthesize_speech(
                input=synthesis_input, voice=voice, audio_config=audio_config
            )
            
            wav_path = f"/tmp/sign_output_{int(time.time())}.wav"
            with open(wav_path, "wb") as out:
                out.write(response.audio_content)
            
            try:
                subprocess.run(["paplay", wav_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                subprocess.run(["aplay", wav_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            if os.path.exists(wav_path):
                try:
                    os.remove(wav_path)
                except Exception:
                    pass
            
            time.sleep(1.5)
            
        except Exception as e:
            print(f"Error in TTS playback: {e}")
        finally:
            with speech_lock:
                is_speaking = False

    threading.Thread(target=run_tts, daemon=True).start()


def video_loop():
    """Continuously processes frames from the camera and prepares them for the stream."""
    global latest_frame
    
    # MediaPipe Setup (Sirf 1 haath detect karenge taaki Pi fast chale)
    mp_hands = mp.solutions.hands
    hands = mp_hands.Hands(max_num_hands=1, min_detection_confidence=0.7)
    mp_draw = mp.solutions.drawing_utils

    # Try to find a working camera index automatically
    cap = None
    for index in [0, 1, 2, 4]:
        print(f"Trying to open camera index {index}...")
        temp_cap = cv2.VideoCapture(index, cv2.CAP_V4L2)
        if temp_cap.isOpened():
            success, img = temp_cap.read()
            if success:
                cap = temp_cap
                print(f"Successfully opened camera at index {index}")
                break
            temp_cap.release()
        
        # Try default backend if V4L2 backend didn't work
        temp_cap = cv2.VideoCapture(index)
        if temp_cap.isOpened():
            success, img = temp_cap.read()
            if success:
                cap = temp_cap
                print(f"Successfully opened camera at index {index} (default backend)")
                break
            temp_cap.release()

    if cap is None:
        print("Error: No working camera found on indices 0, 1, 2, or 4!")
        return

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)

    # Gesture accuracy state variables
    last_detected_gesture = "None"
    gesture_start_time = None
    confirmed_gesture = "None"
    last_spoken_gesture = "None"
    STABLE_DURATION = 2.0  # seconds

    current_fingers_open = [False, False, False, False]
    thumb_is_up = False

    while True:
        success, img = cap.read()
        if not success:
            time.sleep(0.1)
            continue

        # OpenCV BGR use karta hai, MediaPipe ko RGB chahiye
        imgRGB = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        results = hands.process(imgRGB)

        if results.multi_hand_landmarks:
            for handLms in results.multi_hand_landmarks:
                mp_draw.draw_landmarks(img, handLms, mp_hands.HAND_CONNECTIONS)

                # Finger Tips ke Y-coordinates nikalna
                thumb_tip = handLms.landmark[4].y
                index_tip = handLms.landmark[8].y
                middle_tip = handLms.landmark[12].y
                ring_tip = handLms.landmark[16].y
                pinky_tip = handLms.landmark[20].y

                # Finger PIP (Middle joints) ke Y-coordinates
                index_pip = handLms.landmark[6].y
                middle_pip = handLms.landmark[10].y
                ring_pip = handLms.landmark[14].y
                pinky_pip = handLms.landmark[18].y

                # LOGIC: Agar Tip ka Y, Pip ke Y se kam hai, toh ungli khuli hai
                current_fingers_open = [
                    index_tip < index_pip,
                    middle_tip < middle_pip,
                    ring_tip < ring_pip,
                    pinky_tip < pinky_pip
                ]
                
                # Thumbs Up logic: Thumb tip must be higher than the MCP joint
                thumb_is_up = thumb_tip < handLms.landmark[2].y

                # Specific Signs Banana (Rules)
                raw_gesture = "Unknown"
                if all(current_fingers_open):
                    raw_gesture = "Hello / Yes"
                elif not any(current_fingers_open) and thumb_is_up:
                    raw_gesture = "Thumbs Up"
                elif not any(current_fingers_open) and not thumb_is_up:
                    raw_gesture = "Need Help (Fist)"
                elif current_fingers_open[0] and current_fingers_open[1] and not current_fingers_open[2] and not current_fingers_open[3]:
                    raw_gesture = "Thank You (Peace)"
                elif current_fingers_open[0] and not current_fingers_open[1] and not current_fingers_open[2] and not current_fingers_open[3]:
                    raw_gesture = "Pointing / One"
                elif current_fingers_open[0] and not current_fingers_open[1] and not current_fingers_open[2] and current_fingers_open[3]:
                    raw_gesture = "Rock On / Spider-Man"
                elif not current_fingers_open[0] and current_fingers_open[1] and current_fingers_open[2] and current_fingers_open[3]:
                    raw_gesture = "OK Sign"
                elif current_fingers_open[0] and current_fingers_open[1] and current_fingers_open[2] and not current_fingers_open[3]:
                    raw_gesture = "Awesome / Three"
                elif not current_fingers_open[0] and not current_fingers_open[1] and current_fingers_open[2] and current_fingers_open[3]:
                    raw_gesture = "Beckon / Two"

                # Check for stability to increase accuracy (2-second hold)
                if raw_gesture == last_detected_gesture and raw_gesture != "Unknown":
                    elapsed_time = time.time() - gesture_start_time
                    if elapsed_time >= STABLE_DURATION:
                        confirmed_gesture = raw_gesture
                        if confirmed_gesture != last_spoken_gesture:
                            detected_message = sentence_map.get(confirmed_gesture, confirmed_gesture)
                            update_oled_values(gesture=confirmed_gesture, msg=detected_message)
                            speak_text(confirmed_gesture)
                            last_spoken_gesture = confirmed_gesture
                else:
                    last_detected_gesture = raw_gesture
                    gesture_start_time = time.time()
                
                # Send updates to OLED state
                update_oled_values(hand="YES", gesture=raw_gesture)
        else:
            # Reset tracking if no hand is on screen
            last_detected_gesture = "None"
            gesture_start_time = None
            current_fingers_open = [False, False, False, False]
            thumb_is_up = False
            confirmed_gesture = "None"
            last_spoken_gesture = "None"
            
            # Send updates to OLED state
            update_oled_values(hand="NO", gesture="None", msg="Show hand to detect")

        # Premium UI Overlay
        # 1. Top status bar
        cv2.rectangle(img, (0, 0), (320, 48), (35, 35, 35), -1)
        
        if last_detected_gesture != "None" and last_detected_gesture != "Unknown":
            if confirmed_gesture == last_detected_gesture:
                cv2.putText(img, f"MATCH: {confirmed_gesture}", (10, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 0), 1, cv2.LINE_AA)
                cv2.rectangle(img, (10, 28), (310, 36), (0, 255, 0), -1)
            else:
                elapsed = time.time() - gesture_start_time
                progress = min(elapsed / STABLE_DURATION, 1.0)
                
                cv2.putText(img, f"Holding: {last_detected_gesture} ({progress:.1f}s/{STABLE_DURATION}s)", (10, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 165, 255), 1, cv2.LINE_AA)
                cv2.rectangle(img, (10, 28), (310, 36), (60, 60, 60), -1)
                fill_width = int(10 + progress * 300)
                cv2.rectangle(img, (10, 28), (fill_width, 36), (0, 165, 255), -1)
        else:
            idle_text = f"Show Hand | Last Confirmed: {confirmed_gesture}" if confirmed_gesture != "None" else "Show Hand to Detect"
            cv2.putText(img, idle_text, (10, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (200, 200, 200), 1, cv2.LINE_AA)

        # 2. Bottom guide panel
        cv2.rectangle(img, (0, 202), (320, 240), (25, 25, 25), -1)
        cv2.putText(img, "Fingers detected:", (5, 216), cv2.FONT_HERSHEY_SIMPLEX, 0.33, (180, 180, 180), 1, cv2.LINE_AA)
        
        # Draw circles representing Thumb, Index, Middle, Ring, Pinky
        labels = ["T", "I", "M", "R", "P"]
        states = [thumb_is_up] + current_fingers_open
        for idx, name in enumerate(labels):
            x_pos = 105 + idx * 22
            is_open = states[idx]
            color = (0, 255, 0) if is_open else (80, 80, 80)
            cv2.circle(img, (x_pos, 214), 5, color, -1 if is_open else 1)
            cv2.putText(img, name, (x_pos - 3, 230), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (200, 200, 200), 1, cv2.LINE_AA)

        # Help guide map for matching positions
        guide_map = {
            "Hello / Yes": "All Up (I,M,R,P)",
            "Thumbs Up": "Thumb Up, Others Down",
            "Need Help (Fist)": "Closed (None)",
            "Thank You (Peace)": "I,M Up",
            "Pointing / One": "I Up",
            "Rock On / Spider-Man": "I,P Up",
            "OK Sign": "M,R,P Up",
            "Awesome / Three": "I,M,R Up",
            "Beckon / Two": "R,P Up"
        }

        hint_text = ""
        if last_detected_gesture != "None" and last_detected_gesture in guide_map:
            hint_text = f"Guide: {guide_map[last_detected_gesture]}"
        else:
            hint_text = "Tip: Hold stable for 2s"

        cv2.putText(img, hint_text, (215, 216), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (0, 180, 255), 1, cv2.LINE_AA)

        # Update global frame for Flask streaming
        ret, jpeg = cv2.imencode('.jpg', img)
        if ret:
            with frame_lock:
                latest_frame = jpeg.tobytes()

        # Optional GUI Display (falls back gracefully if headless)
        try:
            cv2.imshow("Raspi Sign Language", img)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break
        except Exception:
            pass

    cap.release()
    cv2.destroyAllWindows()


# Flask Routes
@flask_app.route('/')
def index():
    return """
    <!DOCTYPE html>
    <html>
      <head>
        <title>Raspi Sign Language Stream</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
          body {
            margin: 0;
            background: linear-gradient(135deg, #121212 0%, #1e1e24 100%);
            color: #ffffff;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
          }
          h1 {
            margin: 15px 0 5px 0;
            font-size: 22px;
            letter-spacing: 1px;
            color: #00a5ff;
            text-shadow: 0 2px 4px rgba(0,0,0,0.3);
          }
          .stream-container {
            border: 4px solid #2a2a35;
            border-radius: 12px;
            overflow: hidden;
            box-shadow: 0 8px 24px rgba(0,0,0,0.6);
            background-color: #000;
            max-width: 95%;
            display: flex;
          }
          img {
            width: 100%;
            height: auto;
            max-width: 480px;
            display: block;
          }
          .info {
            margin: 15px 0;
            color: #a0a0b0;
            font-size: 13px;
            text-align: center;
            line-height: 1.5;
          }
          .badge {
            background-color: #00d26a;
            color: black;
            padding: 3px 8px;
            border-radius: 12px;
            font-weight: bold;
            font-size: 11px;
            display: inline-block;
            margin-top: 5px;
          }
        </style>
      </head>
      <body>
        <h1>Raspi Sign Language</h1>
        <div class="stream-container">
          <img src="/video_feed" />
        </div>
        <p class="info">
          Live feed streaming from Raspberry Pi<br>
          <span class="badge">ONLINE</span>
        </p>
      </body>
    </html>
    """

def generate_frames():
    global latest_frame
    while True:
        with frame_lock:
            if latest_frame is None:
                time.sleep(0.03)
                continue
            frame_bytes = latest_frame
        
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
        time.sleep(0.04)  # ~25 FPS streaming is ideal for phone bandwidth

@flask_app.route('/video_feed')
def video_feed():
    return Response(generate_frames(), mimetype='multipart/x-mixed-replace; boundary=frame')


if __name__ == "__main__":
    # Start video processing loop in a background thread
    video_thread = threading.Thread(target=video_loop, daemon=True)
    video_thread.start()

    # Start the OLED refresh loop thread
    oled_th = threading.Thread(target=oled_updater_thread, daemon=True)
    oled_th.start()

    # Launch Flask Server on all local network interfaces on port 5000
    print("\n" + "="*50)
    print("  🚀 RASPI SIGN LANGUAGE LOCAL SERVER IS STARTING!")
    print("  Open this link on your mobile phone browser (same Wi-Fi):")
    print("  👉 http://192.168.1.100:5000")
    print("="*50 + "\n")
    
    flask_app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)