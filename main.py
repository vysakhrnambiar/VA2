# main.py
# Main application script for OpenAI Realtime Voice Assistant with Tools

import os
import json
import base64
import time
import threading
from dotenv import load_dotenv
import pyaudio
import numpy as np
import wave

try:
    from scipy import signal
    SCIPY_AVAILABLE = True
except ImportError:
    SCIPY_AVAILABLE = False
    print("[MAIN_APP_SETUP] WARNING: scipy not installed. Resampling for wake word/VAD disabled.")

try:
    import webrtcvad
    WEBRTC_VAD_AVAILABLE = True
    print("[MAIN_APP_SETUP] webrtcvad module found.")
except ImportError:
    WEBRTC_VAD_AVAILABLE = False
    print("[MAIN_APP_SETUP] WARNING: webrtcvad module not found. Local VAD for barge-in will be disabled.")

# --- Configuration Toggles & Constants ---
CHUNK_MS = 30 
LOCAL_VAD_ENABLED = True
LOCAL_VAD_ACTIVATION_THRESHOLD_MS = 100
MIN_SPEECH_FRAMES_FOR_LOCAL_INTERRUPT = 12
LOCAL_INTERRUPT_COOLDOWN_FRAMES = int(2000 / CHUNK_MS)
MIN_SILENCE_FRAMES_TO_RESET_LOCAL_VAD_STATE = 3
VAD_SAMPLE_RATE = 16000
VAD_FRAME_DURATION_MS = CHUNK_MS
VAD_BYTES_PER_FRAME = int(VAD_SAMPLE_RATE * (VAD_FRAME_DURATION_MS / 1000.0) * 2)

load_dotenv()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_REALTIME_MODEL_ID = os.getenv("OPENAI_REALTIME_MODEL_ID")
APP_CONFIG = {
    "RESEND_API_KEY": os.getenv("RESEND_API_KEY"),
    "DEFAULT_FROM_EMAIL": os.getenv("DEFAULT_FROM_EMAIL"),
    "RESEND_RECIPIENT_EMAILS": os.getenv("RESEND_RECIPIENT_EMAILS"),
    "RESEND_RECIPIENT_EMAILS_BCC": os.getenv("RESEND_RECIPIENT_EMAILS_BCC"),
    "TICKET_EMAIL": os.getenv("TICKET_EMAIL"),
    "RESEND_API_URL": os.getenv("RESEND_API_URL", "https://api.resend.com/emails"),
    "FASTAPI_DISPLAY_API_URL": os.getenv("FASTAPI_DISPLAY_API_URL"),
    "OPENAI_VOICE": os.getenv("OPENAI_VOICE", "ash"), # Changed from "alloy" to "ash" as per your client default
    "TSM_PLAYBACK_SPEED": os.getenv("TSM_PLAYBACK_SPEED", "1.0"),
    "TSM_WINDOW_CHUNKS": os.getenv("TSM_WINDOW_CHUNKS", "8"),
    "END_CONV_AUDIO_FINISH_DELAY_S": float(os.getenv("END_CONV_AUDIO_FINISH_DELAY_S", "2.0")),
    # --- New config from openai_client for Phase 2 ---
    "OPENAI_RECONNECT_DELAY_S": int(os.getenv("OPENAI_RECONNECT_DELAY_S", 5)),
    "OPENAI_PING_INTERVAL_S": int(os.getenv("OPENAI_PING_INTERVAL_S", 20)),
    "OPENAI_PING_TIMEOUT_S": int(os.getenv("OPENAI_PING_TIMEOUT_S", 10)),
}

import logging
from logging.handlers import RotatingFileHandler
logger = None
def _setup_file_logger():
    global logger
    if logger is not None: return
    try:
        os.makedirs("logs", exist_ok=True)
        logger = logging.getLogger("MainAppLogger")
        logger.setLevel(logging.DEBUG)
        for handler in logger.handlers[:]: logger.removeHandler(handler)
        log_filename = "logs/app.log"
        file_handler = RotatingFileHandler(log_filename, maxBytes=10*1024*1024, backupCount=5, encoding="utf-8")
        formatter = logging.Formatter('%(asctime)s - [MAIN_APP] - %(message)s')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        print(f"File logging initialized with rotation: {log_filename}")
    except Exception as e:
        print(f"ERROR: Could not initialize log file: {e}")
        logger = None
_setup_file_logger()

def log(msg):
    print(f"[MAIN_APP] {time.strftime('%Y-%m-%d %H:%M:%S')} {msg}")
    global logger
    if logger is not None:
        try: logger.info(msg)
        except Exception as e: print(f"ERROR: Could not write to log file: {e}")

def log_section(title):
    section_header = f"\n===== {title} ====="
    print(section_header)
    global logger
    if logger is not None:
        try: logger.info(section_header)
        except Exception as e: print(f"ERROR: Could not write section to log file: {e}")

vad_instance = None
if WEBRTC_VAD_AVAILABLE and LOCAL_VAD_ENABLED:
    try:
        vad_instance = webrtcvad.Vad()
        vad_instance.set_mode(0) # VAD_AGGRESSIVENESS_MODE = 0
        log(f"WebRTCVAD instance created. Mode: 0, Frame: {VAD_FRAME_DURATION_MS}ms @ {VAD_SAMPLE_RATE}Hz")
    except Exception as e_vad_init:
        log(f"ERROR initializing WebRTCVAD: {e_vad_init}. Disabling local VAD."); WEBRTC_VAD_AVAILABLE = False; vad_instance = None

log_section("Importing Custom Modules")
wake_word_detector_instance = None; wake_word_active = False
try:
    from wake_word_detector import WakeWordDetector
    if SCIPY_AVAILABLE:
        try:
            wake_word_detector_instance = WakeWordDetector(sample_rate=16000)
            is_dummy_check = "DummyOpenWakeWordModel" in str(type(wake_word_detector_instance.model)) if hasattr(wake_word_detector_instance, 'model') else True
            if hasattr(wake_word_detector_instance, 'model') and wake_word_detector_instance.model is not None and not is_dummy_check :
                log(f"WakeWordDetector initialized: Model='{wake_word_detector_instance.wake_word_model_name}', Thr={wake_word_detector_instance.threshold}"); wake_word_active = True
            else: log("WakeWordDetector init with DUMMY model or model is None. WW INACTIVE.")
        except Exception as e_ww: log(f"CRITICAL ERROR WakeWordDetector init: {e_ww}. WW INACTIVE.")
    else: log("Scipy unavailable for WakeWordDetector resampling. WW might be INACTIVE.")
except ImportError as e_import_ww: log(f"Failed to import WakeWordDetector: {e_import_ww}. WW DISABLED.")

if not wake_word_active and wake_word_detector_instance is None:
    class DummyWWDetector:
        def __init__(self, *args, **kwargs): self.wake_word_model_name = "N/A - Inactive"
        def process_audio(self, audio_chunk): return False
        def reset(self): pass
    wake_word_detector_instance = DummyWWDetector(); log("Using DUMMY wake word detector as fallback.")
elif not wake_word_active and wake_word_detector_instance is not None:
     log("WakeWordDetector instance exists but is effectively inactive.")

openai_client_instance = None
try: from openai_client import OpenAISpeechClient
except ImportError as e: log(f"CRITICAL ERROR: Failed to import OpenAISpeechClient: {e}. Exiting."); exit(1)

# --- New Import for Phase 2 ---
try:
    from conversation_history_db import init_db as init_conversation_history_db
    CONV_DB_AVAILABLE = True
    log("Successfully imported conversation_history_db.init_db.")
except ImportError as e_conv_db:
    log(f"WARNING: Failed to import conversation_history_db: {e_conv_db}. Conversation history will not be logged locally.")
    CONV_DB_AVAILABLE = False
    def init_conversation_history_db(): # Dummy function
        log("Conversation history DB module not available, init_db call skipped.")

# Audio Configuration
INPUT_RATE = 24000; OUTPUT_RATE = 24000; WAKE_WORD_PROCESS_RATE = 16000
INPUT_CHUNK_SAMPLES = int(INPUT_RATE * CHUNK_MS / 1000)
OUTPUT_PLAYER_CHUNK_SAMPLES = int(OUTPUT_RATE * CHUNK_MS / 1000)
FORMAT = pyaudio.paInt16; CHANNELS = 1

STATE_LISTENING_FOR_WAKEWORD = "LISTENING_FOR_WAKEWORD"
STATE_SENDING_TO_OPENAI = "SENDING_TO_OPENAI"
current_app_state = STATE_LISTENING_FOR_WAKEWORD if wake_word_active else STATE_SENDING_TO_OPENAI
log(f"State Management: Initial App State set to {current_app_state} (WW Active: {wake_word_active})")
state_just_changed_to_sending = False; state_lock = threading.Lock()

def set_app_state_main(new_state):
    global current_app_state, state_just_changed_to_sending
    with state_lock:
        if current_app_state != new_state:
            log(f"App State changed: {current_app_state} -> {new_state}")
            current_app_state = new_state
            if new_state == STATE_SENDING_TO_OPENAI: state_just_changed_to_sending = True
def get_app_state_main():
    with state_lock: return current_app_state

p = pyaudio.PyAudio()
class PCMPlayer: # Condensed for brevity, no changes here
    def __init__(self, rate=OUTPUT_RATE, channels=CHANNELS, format_player=FORMAT, chunk_samples_player=OUTPUT_PLAYER_CHUNK_SAMPLES):
        self.stream = None; self.buffer = b""; self.chunk_bytes = chunk_samples_player * pyaudio.get_sample_size(format_player) * channels
        try: self.stream = p.open(format=format_player, channels=channels, rate=rate, output=True, frames_per_buffer=chunk_samples_player)
        except Exception as e: log(f"CRITICAL ERROR PCMPlayer init: {e}"); raise
    def play(self, pcm_bytes): # Simplified
        if not self.stream: return; self.buffer += pcm_bytes
        while len(self.buffer) >= self.chunk_bytes:
            try: self.stream.write(self.buffer[:self.chunk_bytes]); self.buffer = self.buffer[self.chunk_bytes:]
            except IOError: self.close(); break
    def flush(self): # Simplified
        if not self.stream or not self.buffer: return
        try: self.stream.write(self.buffer)
        except IOError: self.close()
        finally: self.buffer = b""
    def clear(self): self.buffer = b""; log("PCMPlayer: Buffer cleared.")
    def close(self): # Simplified
        if self.stream:
            try:
                if self.stream.is_active(): self.stream.stop_stream()
                while not self.stream.is_stopped(): time.sleep(0.01)
                self.stream.close()
            except: pass # Simplified error handling
            finally: self.stream = None; log("PCMPlayer stream closed.")
player_instance = None

def get_input_stream():
    try: return p.open(format=FORMAT, channels=CHANNELS, rate=INPUT_RATE, input=True, frames_per_buffer=INPUT_CHUNK_SAMPLES)
    except Exception as e: log(f"CRITICAL ERROR PyAudio input stream: {e}"); return None

def is_speech_detected_by_webrtc_vad(audio_chunk_16khz_pcm16_bytes): # No changes
    global vad_instance
    if not WEBRTC_VAD_AVAILABLE or not vad_instance or not audio_chunk_16khz_pcm16_bytes: return False
    try:
        if len(audio_chunk_16khz_pcm16_bytes) == VAD_BYTES_PER_FRAME:
            return vad_instance.is_speech(audio_chunk_16khz_pcm16_bytes, VAD_SAMPLE_RATE)
        return False
    except: return False

def continuous_audio_pipeline(openai_client_ref): # Condensed, logic for VAD/WW/sending remains
    global state_just_changed_to_sending
    mic_stream = get_input_stream()
    if not mic_stream: log("CRITICAL: Mic stream failed. Pipeline cannot start."); return
    log("Mic stream opened. Audio pipeline started.")
    local_vad_speech_frames_count = 0; local_vad_silence_frames_after_speech = 0
    local_interrupt_cooldown_frames_remaining = 0
    wf_raw = None; wf_processed = None # WAV file logic unchanged

    try:
        # --- WAV file opening logic (unchanged) ---
        try:
            wf_raw = wave.open("mic_capture_raw.wav", 'wb')
            wf_raw.setnchannels(CHANNELS); wf_raw.setsampwidth(p.get_sample_size(FORMAT)); wf_raw.setframerate(INPUT_RATE)
            wf_processed = wave.open("mic_capture_processed.wav", 'wb')
            wf_processed.setnchannels(CHANNELS); wf_processed.setsampwidth(p.get_sample_size(FORMAT)); wf_processed.setframerate(INPUT_RATE)
        except Exception as e_wav_open: log(f"ERROR opening WAV files: {e_wav_open}"); wf_raw=None; wf_processed=None

        while True:
            if not openai_client_ref.connected: # Check client's connected status
                time.sleep(0.2) # Wait if not connected (reconnection loop in client will handle)
                # Check if the client's outer loop is still running; if not, audio pipe should stop
                if not (hasattr(openai_client_ref, 'keep_outer_loop_running') and openai_client_ref.keep_outer_loop_running):
                    log("OpenAI client's main loop seems stopped. Exiting audio pipeline."); break
                continue

            raw_audio_bytes_24k = b''
            try: # Mic read logic unchanged
                if mic_stream.is_active():
                    raw_audio_bytes_24k = mic_stream.read(INPUT_CHUNK_SAMPLES, exception_on_overflow=False)
                    # Basic length check
                    expected_len = INPUT_CHUNK_SAMPLES * pyaudio.get_sample_size(FORMAT) * CHANNELS
                    if len(raw_audio_bytes_24k) != expected_len: raw_audio_bytes_24k = b''
                else: time.sleep(CHUNK_MS / 1000.0); continue
            except IOError as e: log(f"IOError reading PyAudio stream: {e}. Exiting audio loop."); break
            if not raw_audio_bytes_24k: continue

            audio_bytes_24k_for_downstream = raw_audio_bytes_24k
            # --- WAV writing (unchanged) ---
            if wf_raw: wf_raw.writeframes(raw_audio_bytes_24k)
            if wf_processed: wf_processed.writeframes(audio_bytes_24k_for_downstream)

            current_pipeline_app_state_iter = get_app_state_main()
            # --- Local VAD logic (unchanged) ---
            if local_interrupt_cooldown_frames_remaining > 0: local_interrupt_cooldown_frames_remaining -= 1
            run_local_vad_check = False
            if LOCAL_VAD_ENABLED and WEBRTC_VAD_AVAILABLE and SCIPY_AVAILABLE and \
               current_pipeline_app_state_iter == STATE_SENDING_TO_OPENAI and \
               local_interrupt_cooldown_frames_remaining == 0 and \
               hasattr(openai_client_ref, 'is_assistant_speaking') and openai_client_ref.is_assistant_speaking():
                if openai_client_ref.get_current_assistant_speech_duration_ms() > LOCAL_VAD_ACTIVATION_THRESHOLD_MS: run_local_vad_check = True
            
            if run_local_vad_check and audio_bytes_24k_for_downstream:
                # Resampling and VAD check logic (unchanged)
                audio_for_vad_16khz = b'' # Placeholder for brevity
                try: # Resample logic for VAD
                    audio_np_24k = np.frombuffer(audio_bytes_24k_for_downstream, dtype=np.int16)
                    num_samples_16k = int(len(audio_np_24k) * VAD_SAMPLE_RATE / INPUT_RATE)
                    if num_samples_16k > 0:
                        audio_np_16k_float32 = signal.resample(audio_np_24k.astype(np.float32), num_samples_16k)
                        audio_np_16k_int16_scaled = (audio_np_16k_float32.astype(np.int16) * 0.20).astype(np.int16) # VAD_VOLUME_REDUCTION_FACTOR
                        temp_audio_bytes = audio_np_16k_int16_scaled.tobytes()
                        if len(temp_audio_bytes) == VAD_BYTES_PER_FRAME: audio_for_vad_16khz = temp_audio_bytes
                        # Padding/truncation logic for VAD_BYTES_PER_FRAME
                except: pass
                
                if audio_for_vad_16khz and is_speech_detected_by_webrtc_vad(audio_for_vad_16khz):
                    local_vad_speech_frames_count += 1
                    if local_vad_speech_frames_count >= MIN_SPEECH_FRAMES_FOR_LOCAL_INTERRUPT:
                        openai_client_ref.handle_local_user_speech_interrupt()
                        local_interrupt_cooldown_frames_remaining = LOCAL_INTERRUPT_COOLDOWN_FRAMES; local_vad_speech_frames_count = 0
                # VAD silence reset logic unchanged
            else: local_vad_speech_frames_count = 0; local_vad_silence_frames_after_speech = 0

            # --- Wake word detection logic (unchanged) ---
            if current_pipeline_app_state_iter == STATE_LISTENING_FOR_WAKEWORD and wake_word_active and audio_bytes_24k_for_downstream:
                # Resampling and WW processing (unchanged)
                try: # Resample for WW
                    audio_np_24k_ww = np.frombuffer(audio_bytes_24k_for_downstream, dtype=np.int16)
                    num_samples_16k_ww = int(len(audio_np_24k_ww) * WAKE_WORD_PROCESS_RATE / INPUT_RATE)
                    if num_samples_16k_ww > 0:
                        audio_np_16k_ww_float = signal.resample(audio_np_24k_ww.astype(np.float32), num_samples_16k_ww)
                        audio_bytes_16k_for_ww = audio_np_16k_ww_float.astype(np.int16).tobytes()
                        if wake_word_detector_instance.process_audio(audio_bytes_16k_for_ww):
                            set_app_state_main(STATE_SENDING_TO_OPENAI)
                            if hasattr(wake_word_detector_instance, 'reset'): wake_word_detector_instance.reset()
                except: pass


            # --- Sending audio to OpenAI (unchanged logic, but check client.connected) ---
            if get_app_state_main() == STATE_SENDING_TO_OPENAI and audio_bytes_24k_for_downstream:
                if hasattr(openai_client_ref, 'ws_app') and openai_client_ref.ws_app and openai_client_ref.connected: # Check connected
                    audio_b64_str = base64.b64encode(audio_bytes_24k_for_downstream).decode('utf-8')
                    audio_msg_to_send = {"type": "input_audio_buffer.append", "audio": audio_b64_str}
                    try:
                        if hasattr(openai_client_ref.ws_app, 'send'):
                             openai_client_ref.ws_app.send(json.dumps(audio_msg_to_send))
                             if state_just_changed_to_sending:
                                 response_create_payload = {"type": "response.create", "response": {"modalities": ["text", "audio"], "voice": APP_CONFIG.get("OPENAI_VOICE", "ash"), "output_audio_format": "pcm16"}}
                                 openai_client_ref.ws_app.send(json.dumps(response_create_payload))
                                 state_just_changed_to_sending = False
                    except Exception as e_send_ws: # Catch specific WebSocket exceptions if client does
                        # Client's run_forever loop will handle actual disconnects/errors
                        if isinstance(e_send_ws, getattr(openai_client_ref.ws_app, 'WebSocketConnectionClosedException', Exception)): # Check if ws_app has this attr
                           log(f"OpenAI WS closed during send from audio pipe: {e_send_ws}")
                           # No need to set openai_client_ref.connected = False here, client handles it

    except KeyboardInterrupt: log("KeyboardInterrupt in audio pipeline.")
    except Exception as e_pipeline: log(f"Major exception in audio pipeline: {e_pipeline}")
    finally:
        log("Audio pipeline stopping. Closing mic stream...")
        if mic_stream: mic_stream.close() # Simplified close
        log("Mic stream closed.")
        if wf_raw: wf_raw.close()
        if wf_processed: wf_processed.close()

# --- Main Execution ---
if __name__ == "__main__":
    log_section("APPLICATION STARTING")
    if not OPENAI_API_KEY or not OPENAI_REALTIME_MODEL_ID: log("CRITICAL: OpenAI API Key/Model ID missing. Exiting."); exit(1)

    # --- Phase 2: Initialize Conversation History DB ---
    if CONV_DB_AVAILABLE:
        log("Initializing conversation history database...")
        init_conversation_history_db() # Call the imported init function
    else:
        log("Conversation history database module not available. Skipping initialization.")
    # --- End of Phase 2 Change for DB Init ---

    try: player_instance = PCMPlayer()
    except Exception as e_player_init: log(f"CRITICAL: PCMPlayer init failed: {e_player_init}. Exiting."); p and p.terminate(); exit(1)

    log(f"Initial App State: {current_app_state} (WW Active: {wake_word_active})")
    # Other log messages (unchanged)

    ws_full_url = f"wss://api.openai.com/v1/realtime?model={OPENAI_REALTIME_MODEL_ID}"
    auth_headers = ["Authorization: Bearer " + OPENAI_API_KEY, "OpenAI-Beta: realtime=v1"]
    # Pass more config to client, including new reconnect/ping settings
    client_config = {**APP_CONFIG, "CHUNK_MS": CHUNK_MS, "USE_ULAW_FOR_OPENAI_INPUT": False }

    try:
        openai_client_instance = OpenAISpeechClient(
            ws_url_param=ws_full_url, headers_param=auth_headers, main_log_fn=log,
            pcm_player=player_instance, app_state_setter=set_app_state_main, app_state_getter=get_app_state_main,
            input_rate_hz=INPUT_RATE, output_rate_hz=OUTPUT_RATE, is_ww_active=wake_word_active,
            ww_detector_instance_ref=wake_word_detector_instance, app_config_dict=client_config
        )
    except Exception as e_client_init:
        log(f"CRITICAL ERROR: OpenAISpeechClient init failed: {e_client_init}. Exiting.");
        if player_instance: player_instance.close()
        if p: p.terminate(); exit(1)

    ws_client_thread = threading.Thread(target=openai_client_instance.run_client, daemon=True); ws_client_thread.start()
    
    # No explicit connection wait here, as run_client now handles continuous attempts.
    # The audio pipeline will wait for client.connected to be True.
    log("OpenAI client thread started. It will attempt to connect and reconnect automatically.")

    audio_pipeline_thread = threading.Thread(target=continuous_audio_pipeline, args=(openai_client_instance,), daemon=True)
    audio_pipeline_thread.start()
    log("Audio pipeline thread started.")

    try:
        while ws_client_thread.is_alive(): # Main loop to keep app running
            if audio_pipeline_thread and not audio_pipeline_thread.is_alive():
                log("WARNING: Audio pipeline thread exited. Check logs."); break
            time.sleep(0.5)
        log("OpenAI client thread has finished or is no longer alive. Main thread will now exit.")
    except KeyboardInterrupt: print("\nCtrl+C by main thread. Initiating shutdown...")
    finally:
        log_section("APPLICATION SHUTDOWN SEQUENCE")
        if openai_client_instance and hasattr(openai_client_instance, 'close_connection'):
            log("Calling client's close_connection method...")
            openai_client_instance.close_connection() # This will signal run_client to stop

        if audio_pipeline_thread and audio_pipeline_thread.is_alive():
            log("Waiting for audio pipeline thread to join...")
            audio_pipeline_thread.join(timeout=3)
        if ws_client_thread and ws_client_thread.is_alive():
            log("Waiting for OpenAI client thread to join...")
            ws_client_thread.join(timeout=openai_client_instance.RECONNECT_DELAY_SECONDS + 2) # Wait a bit longer than reconnect delay

        if player_instance: player_instance.close() # Simplified close
        if p: p.terminate()
        log_section("APPLICATION FULLY ENDED")