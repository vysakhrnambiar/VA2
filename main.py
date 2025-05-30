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
import requests # For DB monitor thread
import sqlite3  # For DB monitor thread
from datetime import datetime # For DB monitor thread (already implicitly imported via time but good to be explicit)

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
    "OPENAI_API_KEY": OPENAI_API_KEY, # Added for openai_client sync summarizer
    "RESEND_API_KEY": os.getenv("RESEND_API_KEY"),
    "DEFAULT_FROM_EMAIL": os.getenv("DEFAULT_FROM_EMAIL"),
    "RESEND_RECIPIENT_EMAILS": os.getenv("RESEND_RECIPIENT_EMAILS"),
    "RESEND_RECIPIENT_EMAILS_BCC": os.getenv("RESEND_RECIPIENT_EMAILS_BCC"),
    "TICKET_EMAIL": os.getenv("TICKET_EMAIL"),
    "RESEND_API_URL": os.getenv("RESEND_API_URL", "https://api.resend.com/emails"),
    "FASTAPI_DISPLAY_API_URL": os.getenv("FASTAPI_DISPLAY_API_URL"),
    "OPENAI_VOICE": os.getenv("OPENAI_VOICE", "ash"),
    "TSM_PLAYBACK_SPEED": os.getenv("TSM_PLAYBACK_SPEED", "1.0"),
    "TSM_WINDOW_CHUNKS": os.getenv("TSM_WINDOW_CHUNKS", "8"),
    "END_CONV_AUDIO_FINISH_DELAY_S": float(os.getenv("END_CONV_AUDIO_FINISH_DELAY_S", "2.0")),
    "OPENAI_RECONNECT_DELAY_S": int(os.getenv("OPENAI_RECONNECT_DELAY_S", 5)),
    "OPENAI_PING_INTERVAL_S": int(os.getenv("OPENAI_PING_INTERVAL_S", 20)),
    "OPENAI_PING_TIMEOUT_S": int(os.getenv("OPENAI_PING_TIMEOUT_S", 10)),
    # --- New Config for Phase 4 DB Monitor Thread ---
    "DB_MONITOR_POLL_INTERVAL_S": int(os.getenv("DB_MONITOR_POLL_INTERVAL_S", 20)),
    "FASTAPI_UI_STATUS_UPDATE_URL": os.getenv("FASTAPI_UI_STATUS_UPDATE_URL", "http://localhost:8001/api/ui_status_update"),
    "FASTAPI_NOTIFY_CALL_UPDATE_URL": os.getenv("FASTAPI_NOTIFY_CALL_UPDATE_URL", "http://localhost:8001/api/notify_call_update_available"),
    "SCHEDULED_CALLS_DB_PATH": os.path.join(os.path.dirname(os.path.abspath(__file__)), "scheduled_calls.db"),
}


import logging
from logging.handlers import RotatingFileHandler
logger = None
def _setup_file_logger(): # Unchanged
    global logger; # ... (same as before) ...
    if logger is not None: return
    try:
        os.makedirs("logs", exist_ok=True)
        logger = logging.getLogger("MainAppLogger")
        logger.setLevel(logging.DEBUG)
        for handler in logger.handlers[:]: logger.removeHandler(handler)
        log_filename = "logs/app.log"
        file_handler = RotatingFileHandler(log_filename, maxBytes=10*1024*1024, backupCount=5, encoding="utf-8")
        formatter = logging.Formatter('%(asctime)s - [MAIN_APP] - %(levelname)s - %(message)s') # Added levelname
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        print(f"File logging initialized with rotation: {log_filename}")
    except Exception as e:
        print(f"ERROR: Could not initialize log file: {e}")
        logger = None
_setup_file_logger()

def log(msg, level=logging.INFO, **kwargs): # Modified to accept **kwargs
    timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
    level_name = logging.getLevelName(level)
    print(f"[{level_name}] [MAIN_APP] {timestamp} {msg}")
    global logger
    if logger is not None:
        try: logger.log(level, msg, **kwargs) # Pass **kwargs to logger.log
        except Exception as e: print(f"ERROR: Could not write to log file: {e}")

def log_section(title): # Unchanged
    section_header = f"\n===== {title} ====="; print(section_header)
    if logger: logger.info(section_header)


vad_instance = None # VAD Init unchanged
# ... (same as before) ...
if WEBRTC_VAD_AVAILABLE and LOCAL_VAD_ENABLED:
    try:
        vad_instance = webrtcvad.Vad()
        vad_instance.set_mode(0) 
        log(f"WebRTCVAD instance created. Mode: 0, Frame: {VAD_FRAME_DURATION_MS}ms @ {VAD_SAMPLE_RATE}Hz")
    except Exception as e_vad_init:
        log(f"ERROR initializing WebRTCVAD: {e_vad_init}. Disabling local VAD.", logging.ERROR); WEBRTC_VAD_AVAILABLE = False; vad_instance = None


log_section("Importing Custom Modules")
wake_word_detector_instance = None; wake_word_active = False # WW Init unchanged
# ... (same as before) ...
try:
    from wake_word_detector import WakeWordDetector
    if SCIPY_AVAILABLE:
        try:
            wake_word_detector_instance = WakeWordDetector(sample_rate=16000)
            is_dummy_check = "DummyOpenWakeWordModel" in str(type(wake_word_detector_instance.model)) if hasattr(wake_word_detector_instance, 'model') else True
            if hasattr(wake_word_detector_instance, 'model') and wake_word_detector_instance.model is not None and not is_dummy_check :
                log(f"WakeWordDetector initialized: Model='{wake_word_detector_instance.wake_word_model_name}', Thr={wake_word_detector_instance.threshold}"); wake_word_active = True
            else: log("WakeWordDetector init with DUMMY model or model is None. WW INACTIVE.", logging.WARNING)
        except Exception as e_ww: log(f"CRITICAL ERROR WakeWordDetector init: {e_ww}. WW INACTIVE.", logging.CRITICAL)
    else: log("Scipy unavailable for WakeWordDetector resampling. WW might be INACTIVE.", logging.WARNING)
except ImportError as e_import_ww: log(f"Failed to import WakeWordDetector: {e_import_ww}. WW DISABLED.", logging.ERROR)
if not wake_word_active and wake_word_detector_instance is None:
    class DummyWWDetector: # Dummy unchanged
        def __init__(self, *args, **kwargs): self.wake_word_model_name = "N/A - Inactive"
        def process_audio(self, audio_chunk): return False
        def reset(self): pass
    wake_word_detector_instance = DummyWWDetector(); log("Using DUMMY wake word detector as fallback.", logging.WARNING)


openai_client_instance = None
try: from openai_client import OpenAISpeechClient
except ImportError as e: log(f"CRITICAL ERROR: Failed to import OpenAISpeechClient: {e}. Exiting.", logging.CRITICAL); exit(1)

try: # Conv DB Init unchanged
    from conversation_history_db import init_db as init_conversation_history_db
    CONV_DB_AVAILABLE = True
    log("Successfully imported conversation_history_db.init_db.")
except ImportError as e_conv_db:
    log(f"WARNING: Failed to import conversation_history_db: {e_conv_db}. Conversation history will not be logged locally.", logging.WARNING)
    CONV_DB_AVAILABLE = False
    def init_conversation_history_db(): log("Conversation history DB module not available, init_db call skipped.", logging.WARNING)

# Audio Config & State Management unchanged
# ... (same as before) ...
INPUT_RATE = 24000; OUTPUT_RATE = 24000; WAKE_WORD_PROCESS_RATE = 16000
INPUT_CHUNK_SAMPLES = int(INPUT_RATE * CHUNK_MS / 1000)
OUTPUT_PLAYER_CHUNK_SAMPLES = int(OUTPUT_RATE * CHUNK_MS / 1000)
FORMAT = pyaudio.paInt16; CHANNELS = 1
STATE_LISTENING_FOR_WAKEWORD = "LISTENING_FOR_WAKEWORD"
STATE_SENDING_TO_OPENAI = "SENDING_TO_OPENAI"
current_app_state = STATE_LISTENING_FOR_WAKEWORD if wake_word_active else STATE_SENDING_TO_OPENAI
log(f"State Management: Initial App State set to {current_app_state} (WW Active: {wake_word_active})")
state_just_changed_to_sending = False; state_lock = threading.Lock()
def set_app_state_main(new_state): # Unchanged
    global current_app_state, state_just_changed_to_sending; # ...
    with state_lock:
        if current_app_state != new_state:
            log(f"App State changed: {current_app_state} -> {new_state}")
            current_app_state = new_state
            # Clear audio state when transitioning to wake word mode
            if new_state == STATE_LISTENING_FOR_WAKEWORD and openai_client_instance:
                openai_client_instance._clear_audio_state()
            elif new_state == STATE_SENDING_TO_OPENAI:
                state_just_changed_to_sending = True
def get_app_state_main(): # Unchanged
    with state_lock: return current_app_state

p = pyaudio.PyAudio()
player_instance = None # PCMPlayer class and get_input_stream unchanged
# ... (same as before) ...
class PCMPlayer:
    def __init__(self, rate=OUTPUT_RATE, channels=CHANNELS, format_player=FORMAT, chunk_samples_player=OUTPUT_PLAYER_CHUNK_SAMPLES):
        log(f"PCMPlayer Init: Rate={rate}, ChunkSamples={chunk_samples_player}")
        self.stream = None
        try:
            self.stream = p.open(format=format_player, channels=channels, rate=rate, output=True, frames_per_buffer=chunk_samples_player)
        except Exception as e_pyaudio: log(f"CRITICAL ERROR initializing PyAudio output stream: {e_pyaudio}"); raise
        self.buffer = b""; self.chunk_bytes = chunk_samples_player * pyaudio.get_sample_size(format_player) * channels
    def play(self, pcm_bytes):
        if not self.stream: return
        self.buffer += pcm_bytes
        while len(self.buffer) >= self.chunk_bytes:
            try: self.stream.write(self.buffer[:self.chunk_bytes]); self.buffer = self.buffer[self.chunk_bytes:]
            except IOError as e: log(f"PCMPlayer IOError during write: {e}. Stream might be closed."); self.close(); break
    def flush(self):
        if not self.stream or not self.buffer: return
        try: self.stream.write(self.buffer)
        except IOError as e: log(f"PCMPlayer IOError during flush: {e}."); self.close()
        finally: self.buffer = b""
    def clear(self): self.buffer = b""; log("PCMPlayer: Buffer cleared for barge-in.")
    def close(self):
        if self.stream:
            try:
                if self.stream.is_active(): self.stream.stop_stream()
                while not self.stream.is_stopped(): time.sleep(0.01)
                self.stream.close()
            except Exception as e_close: log(f"PCMPlayer error during close: {e_close}")
            finally: self.stream = None; log("PCMPlayer stream closed by main_app.")
def get_input_stream():
    try: return p.open(format=FORMAT, channels=CHANNELS, rate=INPUT_RATE, input=True, frames_per_buffer=INPUT_CHUNK_SAMPLES)
    except Exception as e: log(f"CRITICAL ERROR PyAudio input stream: {e}", logging.CRITICAL); return None

def is_speech_detected_by_webrtc_vad(audio_chunk_16khz_pcm16_bytes): # Unchanged
    # ... (same as before) ...
    global vad_instance
    if not WEBRTC_VAD_AVAILABLE or not vad_instance or not audio_chunk_16khz_pcm16_bytes: return False
    try:
        if len(audio_chunk_16khz_pcm16_bytes) == VAD_BYTES_PER_FRAME:
            return vad_instance.is_speech(audio_chunk_16khz_pcm16_bytes, VAD_SAMPLE_RATE)
        return False
    except Exception as e_vad: log(f"VAD error: {e_vad}", logging.WARNING); return False

def continuous_audio_pipeline(openai_client_ref): # Unchanged
    # ... (same extensive logic as before) ...
    global state_just_changed_to_sending; mic_stream = get_input_stream()
    if not mic_stream: log("CRITICAL: Mic stream failed. Pipeline cannot start.", logging.CRITICAL); return
    # ... (rest of the function as provided in the previous step, including VAD, WW, sending to OpenAI)
    # Ensure the while loop correctly checks openai_client_ref.keep_outer_loop_running
    log("Mic stream opened. Audio pipeline started.")
    local_vad_speech_frames_count = 0; local_vad_silence_frames_after_speech = 0
    local_interrupt_cooldown_frames_remaining = 0
    wf_raw = None; wf_processed = None
    
    # Audio sending counter
    audio_send_counter = 0
    try:
        wf_raw = wave.open("mic_capture_raw.wav", 'wb'); wf_raw.setnchannels(CHANNELS); wf_raw.setsampwidth(p.get_sample_size(FORMAT)); wf_raw.setframerate(INPUT_RATE)
        wf_processed = wave.open("mic_capture_processed.wav", 'wb'); wf_processed.setnchannels(CHANNELS); wf_processed.setsampwidth(p.get_sample_size(FORMAT)); wf_processed.setframerate(INPUT_RATE)
    except Exception as e_wav_open: log(f"ERROR opening WAV files: {e_wav_open}", logging.ERROR); wf_raw=None; wf_processed=None

    try:
        while True:
            if not openai_client_ref.connected:
                time.sleep(0.2)
                if not (hasattr(openai_client_ref, 'keep_outer_loop_running') and openai_client_ref.keep_outer_loop_running):
                    log("OpenAI client's main loop seems stopped. Exiting audio pipeline.", logging.INFO); break
                continue
            
            # Get current state at beginning of loop iteration
            current_pipeline_app_state_iter = get_app_state_main()
            
            # --- Mic Read and VAD/WW/OpenAI Send Logic (as before) ---
            raw_audio_bytes_24k = b''
            try: 
                if mic_stream.is_active():
                    raw_audio_bytes_24k = mic_stream.read(INPUT_CHUNK_SAMPLES, exception_on_overflow=False)
                    expected_len = INPUT_CHUNK_SAMPLES * pyaudio.get_sample_size(FORMAT) * CHANNELS
                    if len(raw_audio_bytes_24k) != expected_len: raw_audio_bytes_24k = b'' # Discard partial
                else: time.sleep(CHUNK_MS / 1000.0); continue
            except IOError as e: log(f"IOError reading PyAudio stream: {e}. Exiting audio loop.", logging.ERROR); break
            if not raw_audio_bytes_24k: continue

            if wf_raw: wf_raw.writeframes(raw_audio_bytes_24k)
            if wf_processed: wf_processed.writeframes(raw_audio_bytes_24k) # Assuming raw for processed for now

            # --- Local VAD for Barge-in ---
            if local_interrupt_cooldown_frames_remaining > 0:
                local_interrupt_cooldown_frames_remaining -=1
            elif LOCAL_VAD_ENABLED and WEBRTC_VAD_AVAILABLE and SCIPY_AVAILABLE and \
                 current_pipeline_app_state_iter == STATE_SENDING_TO_OPENAI and openai_client_ref.is_assistant_speaking() and \
                 openai_client_ref.get_current_assistant_speech_duration_ms() > LOCAL_VAD_ACTIVATION_THRESHOLD_MS:
                try:
                    audio_np_24k = np.frombuffer(raw_audio_bytes_24k, dtype=np.int16)
                    num_samples_16k = int(len(audio_np_24k) * VAD_SAMPLE_RATE / INPUT_RATE)
                    if num_samples_16k > 0:
                        audio_np_16k_float = signal.resample(audio_np_24k.astype(np.float32), num_samples_16k)
                        audio_np_16k_scaled = (audio_np_16k_float.astype(np.int16) * 0.20).astype(np.int16)  # VAD_VOLUME_REDUCTION_FACTOR = 0.20
                        vad_chunk = audio_np_16k_scaled.tobytes()
                        # Ensure vad_chunk is exactly VAD_BYTES_PER_FRAME
                        if len(vad_chunk) > VAD_BYTES_PER_FRAME: vad_chunk = vad_chunk[:VAD_BYTES_PER_FRAME]
                        elif len(vad_chunk) < VAD_BYTES_PER_FRAME and len(vad_chunk) > 0 : vad_chunk += b'\x00' * (VAD_BYTES_PER_FRAME - len(vad_chunk))
                        
                        if len(vad_chunk) == VAD_BYTES_PER_FRAME and is_speech_detected_by_webrtc_vad(vad_chunk):
                            local_vad_speech_frames_count += 1
                            local_vad_silence_frames_after_speech = 0
                            log(f"LOCAL_VAD: Speech detected - Frame count: {local_vad_speech_frames_count}/{MIN_SPEECH_FRAMES_FOR_LOCAL_INTERRUPT}", logging.DEBUG)
                            if local_vad_speech_frames_count >= MIN_SPEECH_FRAMES_FOR_LOCAL_INTERRUPT:
                                log(f"LOCAL_VAD: User speech INTERRUPT detected.", logging.DEBUG)
                                openai_client_ref.handle_local_user_speech_interrupt()
                                local_interrupt_cooldown_frames_remaining = LOCAL_INTERRUPT_COOLDOWN_FRAMES
                                local_vad_speech_frames_count = 0
                        elif local_vad_speech_frames_count > 0: # Speech was detected, now silence
                            local_vad_silence_frames_after_speech += 1
                            log(f"LOCAL_VAD: Silence after speech - Count: {local_vad_silence_frames_after_speech}/{MIN_SILENCE_FRAMES_TO_RESET_LOCAL_VAD_STATE}", logging.DEBUG)
                            if local_vad_silence_frames_after_speech >= MIN_SILENCE_FRAMES_TO_RESET_LOCAL_VAD_STATE:
                                log("LOCAL_VAD: Reset due to silence threshold reached", logging.DEBUG)
                                local_vad_speech_frames_count = 0
                                local_vad_silence_frames_after_speech = 0
                except Exception as e_vad_proc: log(f"Error in local VAD processing: {e_vad_proc}", logging.WARNING)
            else: # Reset if not in VAD check conditions
                local_vad_speech_frames_count = 0; local_vad_silence_frames_after_speech = 0

            # --- Wake Word Detection ---
            if current_pipeline_app_state_iter == STATE_LISTENING_FOR_WAKEWORD and wake_word_active:
                audio_for_ww = b''
                if SCIPY_AVAILABLE:
                    try:
                        audio_np_24k_ww = np.frombuffer(raw_audio_bytes_24k, dtype=np.int16)
                        num_samples_16k_ww = int(len(audio_np_24k_ww) * WAKE_WORD_PROCESS_RATE / INPUT_RATE)
                        if num_samples_16k_ww > 0:
                            audio_np_16k_ww_float = signal.resample(audio_np_24k_ww.astype(np.float32), num_samples_16k_ww)
                            audio_for_ww = audio_np_16k_ww_float.astype(np.int16).tobytes()
                    except Exception as e_ww_resample: log(f"Error resampling for WW: {e_ww_resample}", logging.WARNING)
                elif INPUT_RATE == WAKE_WORD_PROCESS_RATE: # No resampling needed if rates match
                    audio_for_ww = raw_audio_bytes_24k
                
                if audio_for_ww and wake_word_detector_instance.process_audio(audio_for_ww):
                    log_section(f"WAKE WORD DETECTED: '{wake_word_detector_instance.wake_word_model_name.upper()}'!")
                    set_app_state_main(STATE_SENDING_TO_OPENAI)
                    if hasattr(wake_word_detector_instance, 'reset'): wake_word_detector_instance.reset()
                    log("*** Wake word detected! Sending audio to OpenAI... ***", logging.INFO)

            if current_pipeline_app_state_iter == STATE_SENDING_TO_OPENAI and raw_audio_bytes_24k:
                if openai_client_ref.connected: # Send only if connected
                    # Increment counter and log periodically
                    audio_send_counter += 1
                    if audio_send_counter % 75 == 0:  # Log every 75th message
                        log(f"🎤 AUDIO: Sent {audio_send_counter} chunks to OpenAI", logging.INFO)
                        
                    audio_b64_str = base64.b64encode(raw_audio_bytes_24k).decode('utf-8')
                    audio_msg_to_send = {"type": "input_audio_buffer.append", "audio": audio_b64_str}
                    try:
                        if hasattr(openai_client_ref.ws_app, 'send'):
                            openai_client_ref.ws_app.send(json.dumps(audio_msg_to_send))
                            if state_just_changed_to_sending:
                                # Log initial response create message
                                log("🎙️ CONVERSATION: Initiating new assistant response", logging.INFO)
                                # Directly use the string without any get() calls
                                voice_to_use = "ash"  # Hardcoded for now to bypass any potential issues
                                response_create_payload = {"type": "response.create", "response": {"modalities": ["text", "audio"], "voice": APP_CONFIG.get("OPENAI_VOICE", "ash"), "output_audio_format": "pcm16"}}
                                openai_client_ref.ws_app.send(json.dumps(response_create_payload))
                                state_just_changed_to_sending = False
                    except Exception as e_send_ws:
                        log(f"❌ ERROR: Failed to send audio: {e_send_ws}", logging.WARNING)
                        # Let client's run_client handle major disconnects
            # ... rest of VAD/WW logic ...

    except KeyboardInterrupt: log("KeyboardInterrupt in audio pipeline.", logging.INFO)
    except Exception as e_pipeline: log(f"Major exception in audio pipeline: {e_pipeline}", logging.CRITICAL, exc_info=True)
    finally:
        log("Audio pipeline stopping. Closing mic stream...", logging.INFO)
        if mic_stream: mic_stream.close()
        if wf_raw: wf_raw.close()
        if wf_processed: wf_processed.close()


# --- Phase 4: DB Monitor Thread ---
db_monitor_shutdown_event = threading.Event()

def get_db_connection_for_monitor():
    """Establishes a SQLite connection for the monitor thread."""
    try:
        # Using the path from APP_CONFIG for consistency
        conn = sqlite3.connect(APP_CONFIG["SCHEDULED_CALLS_DB_PATH"], timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as e:
        log(f"DB_MONITOR: Error connecting to scheduled_calls_db: {e}", logging.ERROR)
        return None
def play_update_announcement(openai_client_instance, contact_name):
    """
    Generate and play a TTS announcement about an update when in wake word mode.
    
    Args:
        openai_client_instance: Reference to the OpenAI client
        contact_name: Name of the contact associated with the update
    """
    # Only play announcements in wake word mode
    if get_app_state_main() != STATE_LISTENING_FOR_WAKEWORD:
        log("Update announcement skipped - not in wake word mode")
        return False
        
    # Check if player is available
    if not player_instance:
        log("Update announcement skipped - player not available")
        return False
        
    # Generate the announcement audio
    announcement_audio = openai_client_instance.generate_update_announcement(contact_name)
    if not announcement_audio:
        log("Failed to generate announcement audio")
        return False
        
    # Play the announcement
    try:
        log(f"Playing update announcement for contact: {contact_name}")
        player_instance.play(announcement_audio)
        player_instance.flush()
        return True
    except Exception as e:
        log(f"ERROR playing update announcement: {e}")
        return False

def db_monitor_thread_func(shutdown_event: threading.Event, openai_client_ref=None):
    log("DB_MONITOR: Thread started.", logging.INFO)
    poll_interval = APP_CONFIG.get("DB_MONITOR_POLL_INTERVAL_S", 30)
    notify_url = APP_CONFIG.get("FASTAPI_NOTIFY_CALL_UPDATE_URL")

    if not notify_url:
        log("DB_MONITOR: FASTAPI_NOTIFY_CALL_UPDATE_URL not configured. Thread will not send notifications.", logging.ERROR)
        return
        
    if not openai_client_ref:
        log("DB_MONITOR: OpenAI client reference not provided. TTS announcements will be disabled.", logging.WARNING)
        return

    while not shutdown_event.is_set():
        conn = get_db_connection_for_monitor()
        if not conn:
            log("DB_MONITOR: Failed to get DB connection. Retrying next cycle.", logging.WARNING)
            shutdown_event.wait(poll_interval) # Wait before retrying connection
            continue
        
        try:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, contact_name, overall_status, final_summary_for_main_agent 
                FROM scheduled_calls 
                WHERE main_agent_informed_user = 0 
                  AND overall_status IN ('COMPLETED_SUCCESS', 'FAILED_MAX_RETRIES', 
                                         'COMPLETED_OBJECTIVE_NOT_MET', 'FAILED_PERMANENT_ERROR')
            """)
            jobs_to_notify = cursor.fetchall()

            for job_row in jobs_to_notify:
                job = dict(job_row) # Convert to dict
                log(f"DB_MONITOR: Found un-notified completed job ID: {job['id']}, Contact: {job['contact_name']}, Status: {job['overall_status']}", logging.INFO)
                
                payload = {
                    "type": "new_call_update_available",
                    "job_id": job['id'], # Good to include for potential UI use
                    "contact_name": job['contact_name'],
                    "status_summary": job.get('final_summary_for_main_agent', f"Call concluded with status: {job['overall_status']}")
                }
                try:
                    response = requests.post(notify_url, json=payload, timeout=5)
                    if response.status_code == 200:
                        log(f"DB_MONITOR: Successfully notified frontend for job ID {job['id']}.", logging.INFO)
                        
                        # Add TTS announcement if in wake word mode and we have OpenAI client reference
                        if get_app_state_main() == STATE_LISTENING_FOR_WAKEWORD and openai_client_ref:
                            # Use a separate thread to avoid blocking the DB monitor thread
                            announcement_thread = threading.Thread(
                                target=play_update_announcement,
                                args=(openai_client_ref, job['contact_name']),
                                daemon=True
                            )
                            announcement_thread.start()
                            log(f"DB_MONITOR: Started TTS announcement thread for job ID {job['id']}")
                        
                        # DO NOT update main_agent_informed_user here. openai_client will do it after LLM priming.
                    else:
                        log(f"DB_MONITOR: Failed to notify frontend for job ID {job['id']}. Status: {response.status_code}, Resp: {response.text[:100]}", logging.WARNING)
                except requests.exceptions.RequestException as e_req:
                    log(f"DB_MONITOR: RequestException notifying frontend for job ID {job['id']}: {e_req}", logging.WARNING)
                except Exception as e_gen: # Catch any other exception during POST
                    log(f"DB_MONITOR: Unexpected error notifying frontend for job ID {job['id']}: {e_gen}", logging.ERROR)

        except sqlite3.Error as e_sql:
            log(f"DB_MONITOR: SQLite error during polling: {e_sql}", logging.ERROR)
        except Exception as e: # Catch-all for other errors in the loop
            log(f"DB_MONITOR: Unexpected error in polling loop: {e}", logging.ERROR)
        finally:
            if conn:
                conn.close()
        
        # Wait for the poll interval or until shutdown is signaled
        shutdown_event.wait(timeout=poll_interval) 
    
    log("DB_MONITOR: Thread shutting down.", logging.INFO)
# --- End of Phase 4 DB Monitor Thread ---

# --- Main Execution ---
if __name__ == "__main__":
    log_section("APPLICATION STARTING")
    if not OPENAI_API_KEY or not OPENAI_REALTIME_MODEL_ID: log("CRITICAL: OpenAI API Key/Model ID missing. Exiting.", logging.CRITICAL); exit(1)

    if CONV_DB_AVAILABLE: # DB Init unchanged
        log("Initializing conversation history database...")
        init_conversation_history_db()
    else: log("Conversation history database module not available.", logging.WARNING)

    try: player_instance = PCMPlayer()
    except Exception as e_player_init: log(f"CRITICAL: PCMPlayer init failed: {e_player_init}. Exiting.", logging.CRITICAL); p and p.terminate(); exit(1)

    log(f"Initial App State: {current_app_state} (WW Active: {wake_word_active})")
    log(f"OpenAI Model: {OPENAI_REALTIME_MODEL_ID}")
    log(f"Audio Rates: MicIn={INPUT_RATE}Hz, PlayerOut={OUTPUT_RATE}Hz, WWProcess={WAKE_WORD_PROCESS_RATE}Hz")
    log(f"Local VAD (WebRTC) Enabled: {LOCAL_VAD_ENABLED and WEBRTC_VAD_AVAILABLE and SCIPY_AVAILABLE}")
    if wake_word_active and wake_word_detector_instance: log(f"WW ACTIVE: Model='{wake_word_detector_instance.wake_word_model_name}'.")
    else: log("WW INACTIVE or model/resampling issue.", logging.WARNING)
    log(f"Display API URL: {APP_CONFIG.get('FASTAPI_DISPLAY_API_URL', 'Not Set')}")
    log(f"UI Status Update URL: {APP_CONFIG.get('FASTAPI_UI_STATUS_UPDATE_URL', 'Not Set')}")
    log(f"Notify Call Update URL: {APP_CONFIG.get('FASTAPI_NOTIFY_CALL_UPDATE_URL', 'Not Set')}")
    log(f"TSM Playback Speed: {APP_CONFIG.get('TSM_PLAYBACK_SPEED', '1.0')} (1.0 = TSM disabled, direct play)")

    ws_full_url = f"wss://api.openai.com/v1/realtime?model={OPENAI_REALTIME_MODEL_ID}"
    auth_headers = ["Authorization: Bearer " + OPENAI_API_KEY, "OpenAI-Beta: realtime=v1"]
    # Make sure OPENAI_VOICE is a string, not a complex object
    APP_CONFIG["OPENAI_VOICE"] = APP_CONFIG.get("OPENAI_VOICE", "ash")
    # Match exactly the format in working openai_client.py
    client_config = {**APP_CONFIG, "CHUNK_MS": CHUNK_MS, "USE_ULAW_FOR_OPENAI_INPUT": False }

    try:
        openai_client_instance = OpenAISpeechClient(
            ws_url_param=ws_full_url, headers_param=auth_headers, main_log_fn=log,
            pcm_player=player_instance, app_state_setter=set_app_state_main, app_state_getter=get_app_state_main,
            input_rate_hz=INPUT_RATE, output_rate_hz=OUTPUT_RATE, is_ww_active=wake_word_active,
            ww_detector_instance_ref=wake_word_detector_instance, app_config_dict=client_config
        )
    except Exception as e_client_init:
        log(f"CRITICAL ERROR: OpenAISpeechClient init failed: {e_client_init}. Exiting.", logging.CRITICAL, exc_info=True)
        if player_instance: player_instance.close();
        if p: p.terminate(); exit(1)

    ws_client_thread = threading.Thread(target=openai_client_instance.run_client, daemon=True)
    ws_client_thread.start()
    log("OpenAI client thread started.")

    audio_pipeline_thread = threading.Thread(target=continuous_audio_pipeline, args=(openai_client_instance,), daemon=True)
    audio_pipeline_thread.start()
    log("Audio pipeline thread started.")

    # --- Phase 4: Start DB Monitor Thread ---
    db_monitor_th = threading.Thread(
        target=db_monitor_thread_func,
        args=(db_monitor_shutdown_event, openai_client_instance),
        daemon=True
    )
    db_monitor_th.start()
    log("DB monitor thread started with TTS announcement capability.")
    # --- End of Phase 4 DB Monitor Thread Start ---

    try:
        while ws_client_thread.is_alive():
            if audio_pipeline_thread and not audio_pipeline_thread.is_alive():
                log("WARNING: Audio pipeline thread exited. Check logs.", logging.WARNING); break
            if db_monitor_th and not db_monitor_th.is_alive(): # Check DB monitor thread too
                log("WARNING: DB monitor thread exited. Check logs.", logging.WARNING); break
            time.sleep(0.5)
        log("A critical thread (OpenAI client, audio, or DB monitor) has finished or is no longer alive. Main thread will now exit.", logging.INFO)
    except KeyboardInterrupt: log("\nCtrl+C by main thread. Initiating shutdown...", logging.INFO)
    finally:
        log_section("APPLICATION SHUTDOWN SEQUENCE")
        
        # --- Phase 4: Signal DB Monitor Thread to shut down ---
        log("Signaling DB monitor thread to shut down...", logging.INFO)
        db_monitor_shutdown_event.set()
        # --- End of Phase 4 DB Monitor Thread Shutdown Signal ---

        if openai_client_instance and hasattr(openai_client_instance, 'close_connection'):
            log("Calling client's close_connection method...", logging.INFO)
            openai_client_instance.close_connection()

        if audio_pipeline_thread and audio_pipeline_thread.is_alive():
            log("Waiting for audio pipeline thread to join...", logging.INFO)
            audio_pipeline_thread.join(timeout=3)
            if audio_pipeline_thread.is_alive(): log("WARN: Audio pipeline thread did not join cleanly.", logging.WARNING)
        
        if ws_client_thread and ws_client_thread.is_alive():
            log("Waiting for OpenAI client thread to join...", logging.INFO)
            ws_client_thread.join(timeout=APP_CONFIG.get("OPENAI_RECONNECT_DELAY_S", 5) + 2)
            if ws_client_thread.is_alive(): log("WARN: OpenAI client thread did not join cleanly.", logging.WARNING)

        # --- Phase 4: Wait for DB Monitor Thread to shut down ---
        if db_monitor_th and db_monitor_th.is_alive():
            log("Waiting for DB monitor thread to join...", logging.INFO)
            db_monitor_th.join(timeout=5) # Give it a few seconds
            if db_monitor_th.is_alive(): log("WARN: DB monitor thread did not join cleanly.", logging.WARNING)
        # --- End of Phase 4 DB Monitor Thread Join ---

        if player_instance: player_instance.close()
        if p: p.terminate()
        log_section("APPLICATION FULLY ENDED")