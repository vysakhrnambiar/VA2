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

# audioop is not used in this version as u-law is abandoned for now
# try:
#     import audioop 
#     AUDIOOP_AVAILABLE = True
# except ImportError:
#     AUDIOOP_AVAILABLE = False


# --- Configuration Toggles & Constants ---
CHUNK_MS = 30 # Defined early as it's used by VAD constants

LOCAL_VAD_ENABLED = True  # Master toggle for local VAD feature for barge-in
LOCAL_VAD_ACTIVATION_THRESHOLD_MS = 400 # Only activate local VAD if assistant speaking > 1s
MIN_SPEECH_FRAMES_FOR_LOCAL_INTERRUPT = 10  # e.g., 3 * 30ms = 90ms of speech to trigger
LOCAL_INTERRUPT_COOLDOWN_FRAMES = int(1000 / CHUNK_MS) # Approx. 1 second cooldown
MIN_SILENCE_FRAMES_TO_RESET_LOCAL_VAD_STATE = 10 # e.g., 300ms of silence

VAD_SAMPLE_RATE = 16000  # webrtcvad works well with 16kHz
VAD_FRAME_DURATION_MS = CHUNK_MS 
VAD_BYTES_PER_FRAME = int(VAD_SAMPLE_RATE * (VAD_FRAME_DURATION_MS / 1000.0) * 2) # 2 bytes for PCM16

# --- Load Environment Variables ---
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
    "FASTAPI_DISPLAY_API_URL": os.getenv("FASTAPI_DISPLAY_API_URL")
}

# --- Logging ---
def log(msg): 
    print(f"[MAIN_APP] {time.strftime('%Y-%m-%d %H:%M:%S')} {msg}")
def log_section(title):
    print(f"\n===== {title} =====")

# --- Initialize VAD Instance ---
vad_instance = None
VAD_AGGRESSIVENESS_MODE = 0
if WEBRTC_VAD_AVAILABLE and LOCAL_VAD_ENABLED:
    try:
        vad_instance = webrtcvad.Vad()
        vad_instance.set_mode(VAD_AGGRESSIVENESS_MODE) # Mode 0-3 (0=least aggressive, 3=most aggressive)
        log(f"WebRTCVAD instance created. Mode: {VAD_AGGRESSIVENESS_MODE}, Frame: {VAD_FRAME_DURATION_MS}ms @ {VAD_SAMPLE_RATE}Hz ({VAD_BYTES_PER_FRAME} bytes)")
    except Exception as e_vad_init:
        log(f"ERROR initializing WebRTCVAD: {e_vad_init}. Disabling local VAD.")
        WEBRTC_VAD_AVAILABLE = False # Ensure it's disabled if init fails
        vad_instance = None # Ensure instance is None if failed

# --- Import Custom Modules (WakeWordDetector initialization sets wake_word_active) ---
log_section("Importing Custom Modules")
wake_word_detector_instance = None 
wake_word_active = False # Initialize to False
try:
    from wake_word_detector import WakeWordDetector
    log("Successfully imported WakeWordDetector class.")
    if SCIPY_AVAILABLE: 
        try:
            wake_word_detector_instance = WakeWordDetector(sample_rate=16000) # WW expects 16kHz
            is_dummy_check = "DummyOpenWakeWordModel" in str(type(wake_word_detector_instance.model)) if hasattr(wake_word_detector_instance, 'model') else True
            if hasattr(wake_word_detector_instance, 'model') and \
               wake_word_detector_instance.model is not None and not is_dummy_check :
                log(f"WakeWordDetector initialized: Model='{wake_word_detector_instance.wake_word_model_name}', Thr={wake_word_detector_instance.threshold}")
                wake_word_active = True # Set True if real WW model loaded
            else:
                log("WakeWordDetector init with DUMMY model or model is None. WW INACTIVE.")
                wake_word_active = False
        except Exception as e_ww:
            log(f"CRITICAL ERROR WakeWordDetector init: {e_ww}. WW INACTIVE.")
            wake_word_active = False
    else:
        log("Scipy unavailable, cannot resample for WW. WW DISABLED.")
        wake_word_active = False
except ImportError as e_import_ww:
    log(f"Failed to import WakeWordDetector: {e_import_ww}. WW DISABLED.")
    wake_word_active = False

# Fallback to DummyWWDetector if no real instance was created or it failed
if not wake_word_active and wake_word_detector_instance is None: 
    class DummyWWDetector: 
        def __init__(self, *args, **kwargs): self.wake_word_model_name = "N/A - Inactive"
        def process_audio(self, audio_chunk): return False
        def reset(self): pass
    wake_word_detector_instance = DummyWWDetector()
    log("Using DUMMY wake word detector as fallback.")
elif not wake_word_active and wake_word_detector_instance is not None:
     log("WakeWordDetector instance exists but is effectively inactive (e.g. dummy model).")


openai_client_instance = None 
try:
    from openai_client import OpenAISpeechClient 
    log("Successfully imported OpenAISpeechClient.")
except ImportError as e:
    log(f"CRITICAL ERROR: Failed to import OpenAISpeechClient from openai_client.py: {e}. Exiting.")
    exit(1) 

# --- Audio Configuration ---
INPUT_RATE = 24000 
OUTPUT_RATE = 24000 
WAKE_WORD_PROCESS_RATE = 16000 
# CHUNK_MS defined at the top
INPUT_CHUNK_SAMPLES = int(INPUT_RATE * CHUNK_MS / 1000) 
OUTPUT_PLAYER_CHUNK_SAMPLES = int(OUTPUT_RATE * CHUNK_MS / 1000) 
FORMAT = pyaudio.paInt16 
CHANNELS = 1            

# --- State Management ---
STATE_LISTENING_FOR_WAKEWORD = "LISTENING_FOR_WAKEWORD"
STATE_SENDING_TO_OPENAI = "SENDING_TO_OPENAI"

# Correctly initialize current_app_state AFTER wake_word_active is definitively set
if wake_word_active:
    current_app_state = STATE_LISTENING_FOR_WAKEWORD
else:
    current_app_state = STATE_SENDING_TO_OPENAI
log(f"State Management: Initial App State set to {current_app_state} (WW Active: {wake_word_active})")

state_just_changed_to_sending = False
state_lock = threading.Lock()

def set_app_state_main(new_state): 
    global current_app_state, state_just_changed_to_sending
    with state_lock:
        if current_app_state != new_state:
            log(f"App State changed: {current_app_state} -> {new_state}")
            current_app_state = new_state
            if new_state == STATE_SENDING_TO_OPENAI:
                state_just_changed_to_sending = True
def get_app_state_main(): 
    with state_lock:
        return current_app_state

# --- PyAudio Setup & PCMPlayer ---
p = pyaudio.PyAudio()
class PCMPlayer: 
    def __init__(self, rate=OUTPUT_RATE, channels=CHANNELS, format_player=FORMAT, chunk_samples_player=OUTPUT_PLAYER_CHUNK_SAMPLES):
        log(f"PCMPlayer Init: Rate={rate}, ChunkSamples={chunk_samples_player}")
        self.stream = None
        try:
            self.stream = p.open(format=format_player, channels=channels, rate=rate, output=True, frames_per_buffer=chunk_samples_player)
        except Exception as e_pyaudio:
            log(f"CRITICAL ERROR initializing PyAudio output stream: {e_pyaudio}")
            raise 
        self.buffer = b""
        self.chunk_bytes = chunk_samples_player * pyaudio.get_sample_size(format_player) * channels 
    def play(self, pcm_bytes): 
        if not self.stream: return
        self.buffer += pcm_bytes
        while len(self.buffer) >= self.chunk_bytes: 
            try:
                self.stream.write(self.buffer[:self.chunk_bytes])
                self.buffer = self.buffer[self.chunk_bytes:]
            except IOError as e:
                log(f"PCMPlayer IOError during write: {e}. Stream might be closed.")
                self.close() 
                break 
    def flush(self): 
        if not self.stream or not self.buffer: return
        try: self.stream.write(self.buffer) 
        except IOError as e: log(f"PCMPlayer IOError during flush: {e}."); self.close()
        finally: self.buffer = b""
    def clear(self): 
        self.buffer = b""
        log("PCMPlayer: Buffer cleared for barge-in.")
    def close(self): 
        if self.stream:
            try:
                if self.stream.is_active(): self.stream.stop_stream()
                while not self.stream.is_stopped(): time.sleep(0.01)
                self.stream.close()
            except Exception as e_close: log(f"PCMPlayer error during close: {e_close}")
            finally: self.stream = None; log("PCMPlayer stream closed by main_app.")
player_instance = None 

def get_input_stream(): 
    log(f"PyAudio Input Stream Open Request: Rate={INPUT_RATE}, ChunkSize={INPUT_CHUNK_SAMPLES}")
    try:
        return p.open(format=FORMAT, channels=CHANNELS, rate=INPUT_RATE, input=True, frames_per_buffer=INPUT_CHUNK_SAMPLES)
    except Exception as e_pyaudio_in:
        log(f"CRITICAL ERROR initializing PyAudio input stream: {e_pyaudio_in}")
        return None

# --- Local VAD Helper ---
def is_speech_detected_by_webrtc_vad(audio_chunk_16khz_pcm16_bytes):
    global vad_instance # Ensure we're using the globally initialized one
    if not WEBRTC_VAD_AVAILABLE or not vad_instance or not audio_chunk_16khz_pcm16_bytes:
        return False
    try:
        if len(audio_chunk_16khz_pcm16_bytes) == VAD_BYTES_PER_FRAME:
            return vad_instance.is_speech(audio_chunk_16khz_pcm16_bytes, VAD_SAMPLE_RATE)
        # else:
            # log(f"VAD_DEBUG: Incorrect frame size for WebRTCVAD. Got {len(audio_chunk_16khz_pcm16_bytes)}, expected {VAD_BYTES_PER_FRAME}")
        return False 
    except Exception as e_vad_process:
        log(f"ERROR during WebRTCVAD is_speech check: {e_vad_process} (len: {len(audio_chunk_16khz_pcm16_bytes)})")
        return False

# --- Continuous Audio Streaming Logic ---
def continuous_audio_pipeline(openai_client_ref): 
    global state_just_changed_to_sending
    VAD_VOLUME_REDUCTION_FACTOR = 0.20 # Reduce to 70% of original amplitude (30% reduction)
    if not OPENAI_API_KEY: log("CRITICAL: OpenAI API key not found. Stopping."); return
    mic_stream = get_input_stream()
    if not mic_stream: log("CRITICAL: Failed to open mic stream. Pipeline cannot start."); return
        
    log("Mic stream opened (24kHz).")
    log(f"Audio pipeline started. Initial state: {get_app_state_main()}")
    
    last_log_time = time.time()
    chunks_sent_to_openai_this_period = 0 
    
    local_vad_speech_frames_count = 0
    local_vad_silence_frames_after_speech = 0
    local_interrupt_cooldown_frames_remaining = 0

    try:
        while True: 
            if not openai_client_ref.connected: 
                time.sleep(0.2) 
                if not (hasattr(openai_client_ref, 'ws_app') and openai_client_ref.ws_app and \
                   getattr(openai_client_ref.ws_app, 'keep_running', False)): 
                    log("OpenAI WebSocket seems stopped (from audio pipeline). Exiting.")
                    break
            
            audio_bytes_24k = b''
            try:
                if mic_stream.is_active():
                    audio_bytes_24k = mic_stream.read(INPUT_CHUNK_SAMPLES, exception_on_overflow=False)
                    expected_len = INPUT_CHUNK_SAMPLES * pyaudio.get_sample_size(FORMAT) * CHANNELS
                    if len(audio_bytes_24k) != expected_len:
                        log(f"WARN: Mic read {len(audio_bytes_24k)} bytes, expected {expected_len}. Discarding.")
                        audio_bytes_24k = b'' 
                else:
                    log("WARN: Mic stream not active. Sleeping.")
                    time.sleep(CHUNK_MS / 1000.0)
            except IOError as e:
                log(f"IOError reading PyAudio stream: {e}. Exiting audio loop.")
                break 

            if audio_bytes_24k:
                current_pipeline_app_state_iter = get_app_state_main() # State for this iteration

                # --- Manage Local VAD Cooldown ---
                if local_interrupt_cooldown_frames_remaining > 0:
                    local_interrupt_cooldown_frames_remaining -= 1
                    if local_interrupt_cooldown_frames_remaining == 0:
                        log("LOCAL_VAD: Interrupt cooldown finished.")
                        local_vad_speech_frames_count = 0 
                        local_vad_silence_frames_after_speech = 0

                # --- Local VAD for Proactive Barge-in ---
                run_local_vad_check = False
                if LOCAL_VAD_ENABLED and WEBRTC_VAD_AVAILABLE and SCIPY_AVAILABLE and \
                   current_pipeline_app_state_iter == STATE_SENDING_TO_OPENAI and \
                   local_interrupt_cooldown_frames_remaining == 0 and \
                   hasattr(openai_client_ref, 'is_assistant_speaking') and \
                   openai_client_ref.is_assistant_speaking():
                    
                    assistant_speech_duration = openai_client_ref.get_current_assistant_speech_duration_ms() \
                                                if hasattr(openai_client_ref, 'get_current_assistant_speech_duration_ms') else 0
                    
                    log(f"Assistant Spoken For  {assistant_speech_duration} .")
                    if assistant_speech_duration > LOCAL_VAD_ACTIVATION_THRESHOLD_MS:
                        run_local_vad_check = True
                
                if run_local_vad_check:
                    audio_for_vad_16khz = b''
                    try:
                        audio_np_24k = np.frombuffer(audio_bytes_24k, dtype=np.int16)
                        num_samples_16k = int(len(audio_np_24k) * VAD_SAMPLE_RATE / INPUT_RATE)

                        if num_samples_16k > 0:
                            audio_np_16k_float32 = signal.resample(audio_np_24k.astype(np.float32), num_samples_16k)
                            audio_np_16k_int16_original = audio_np_16k_float32.astype(np.int16)
                            #audio_np_16k = signal.resample(audio_np_24k.astype(np.float32), num_samples_16k)
                            audio_np_16k_int16_scaled = (audio_np_16k_int16_original * VAD_VOLUME_REDUCTION_FACTOR).astype(np.int16)
                            #temp_audio_bytes = audio_np_16k.astype(np.int16).tobytes()
                            temp_audio_bytes = audio_np_16k_int16_scaled.tobytes()
                            
                            # Ensure frame alignment for WebRTCVAD
                            if len(temp_audio_bytes) == VAD_BYTES_PER_FRAME:
                                audio_for_vad_16khz = temp_audio_bytes
                            elif len(temp_audio_bytes) > VAD_BYTES_PER_FRAME:
                                audio_for_vad_16khz = temp_audio_bytes[:VAD_BYTES_PER_FRAME]
                                # log(f"VAD_DEBUG: Truncated resampled audio for VAD: {len(temp_audio_bytes)} to {VAD_BYTES_PER_FRAME}")
                            elif len(temp_audio_bytes) > 0: # Too short, pad with silence
                                audio_for_vad_16khz = temp_audio_bytes + (b'\x00' * (VAD_BYTES_PER_FRAME - len(temp_audio_bytes)))
                                # log(f"VAD_DEBUG: Padded resampled audio for VAD: {len(temp_audio_bytes)} to {VAD_BYTES_PER_FRAME}")
                    except Exception as e_resample_vad:
                        log(f"ERROR resampling/preparing for local VAD: {e_resample_vad}")
                    
                    if audio_for_vad_16khz: 
                        speech_detected_this_chunk = is_speech_detected_by_webrtc_vad(audio_for_vad_16khz)
                        if speech_detected_this_chunk:
                            log(f" Local Vad Speech Detected  ")
                            local_vad_speech_frames_count += 1
                            local_vad_silence_frames_after_speech = 0 
                            if local_vad_speech_frames_count >= MIN_SPEECH_FRAMES_FOR_LOCAL_INTERRUPT:
                                log(f"LOCAL_VAD (WebRTC): User speech INTERRUPT detected ({local_vad_speech_frames_count} frames). Signaling client.")
                                if hasattr(openai_client_ref, 'handle_local_user_speech_interrupt'):
                                    openai_client_ref.handle_local_user_speech_interrupt()
                                local_interrupt_cooldown_frames_remaining = LOCAL_INTERRUPT_COOLDOWN_FRAMES
                                local_vad_speech_frames_count = 0 
                        else: 
                            log(f" Local Vad Speech Detected not detected ")
                            if local_vad_speech_frames_count > 0: 
                                local_vad_silence_frames_after_speech += 1
                                if local_vad_silence_frames_after_speech >= MIN_SILENCE_FRAMES_TO_RESET_LOCAL_VAD_STATE:
                                    local_vad_speech_frames_count = 0 
                                    local_vad_silence_frames_after_speech = 0
                else: # Conditions for run_local_vad_check not met
                    local_vad_speech_frames_count = 0
                    local_vad_silence_frames_after_speech = 0

                # --- Wake Word Processing ---
                if current_pipeline_app_state_iter == STATE_LISTENING_FOR_WAKEWORD and wake_word_active:
                    if SCIPY_AVAILABLE and wake_word_detector_instance:
                        audio_np_24k_ww = np.frombuffer(audio_bytes_24k, dtype=np.int16)
                        num_samples_16k_ww = int(len(audio_np_24k_ww) * WAKE_WORD_PROCESS_RATE / INPUT_RATE)
                        if num_samples_16k_ww > 0:
                            audio_np_16k_ww_float = signal.resample(audio_np_24k_ww.astype(np.float32), num_samples_16k_ww)
                            audio_bytes_16k_for_ww = audio_np_16k_ww_float.astype(np.int16).tobytes()
                            if wake_word_detector_instance.process_audio(audio_bytes_16k_for_ww):
                                log_section(f"WAKE WORD DETECTED: '{wake_word_detector_instance.wake_word_model_name.upper()}'!")
                                set_app_state_main(STATE_SENDING_TO_OPENAI) 
                                if hasattr(wake_word_detector_instance, 'reset'): wake_word_detector_instance.reset()
                                print("\n*** Wake word detected! Sending 24kHz audio to OpenAI... ***\n")
                
                # --- Sending Audio to OpenAI ---
                if get_app_state_main() == STATE_SENDING_TO_OPENAI: 
                    audio_to_send_final = audio_bytes_24k # Default: PCM16@24kHz
                    # u-law conversion is disabled for now based on previous comments.
                    # If you re-enable it, ensure AUDIOOP_AVAILABLE and SCIPY_AVAILABLE checks are in place.

                    if hasattr(openai_client_ref, 'ws_app') and openai_client_ref.ws_app and openai_client_ref.connected: 
                        audio_b64_str = base64.b64encode(audio_to_send_final).decode('utf-8')
                        audio_msg_to_send = {"type": "input_audio_buffer.append", "audio": audio_b64_str}
                        try:
                            if hasattr(openai_client_ref.ws_app, 'send'):
                                 openai_client_ref.ws_app.send(json.dumps(audio_msg_to_send))
                                 chunks_sent_to_openai_this_period += 1 
                                 if state_just_changed_to_sending:
                                     response_create_payload = {"type": "response.create", "response": {"modalities": ["text", "audio"], "voice": "ash", "output_audio_format": "pcm16"}}
                                     openai_client_ref.ws_app.send(json.dumps(response_create_payload))
                                     log("Sent initial 'response.create' after state change")
                                     state_just_changed_to_sending = False
                            else: log("ERROR: ws_app has no send method.")
                        except Exception as e_send_ws: 
                            log(f"Exception during WS send: {e_send_ws}")
                            try:
                                import websocket # Local import for type check
                                if isinstance(e_send_ws, websocket.WebSocketConnectionClosedException) : 
                                    log("OpenAI WS closed during send. Setting connected=False.")
                                    if hasattr(openai_client_ref, 'connected'): openai_client_ref.connected = False
                            except ImportError: pass # Silently pass if websocket module not available here
            
            current_time = time.time()
            if current_time - last_log_time >= 2.0:
                #log(f"AUDIO_SEND_LOG: Attempted to send {chunks_sent_to_openai_this_period} audio chunks to OpenAI in last ~2s. AppState={get_app_state_main()}")
                last_log_time = current_time
                chunks_sent_to_openai_this_period = 0
                    
    except KeyboardInterrupt: log("KeyboardInterrupt in audio pipeline.")
    except Exception as e_pipeline: log(f"Major exception in audio pipeline: {e_pipeline}")
    finally:
        log("Audio pipeline stopping. Closing mic stream...")
        if mic_stream: 
            if hasattr(mic_stream, 'is_active') and mic_stream.is_active(): mic_stream.stop_stream()
            if hasattr(mic_stream, 'close'): mic_stream.close()
        log("Mic stream closed.")

# --- Main Execution ---
if __name__ == "__main__":
    log_section("APPLICATION STARTING")
    if not OPENAI_API_KEY: log("CRITICAL: OPENAI_API_KEY not found. Exiting."); exit(1)
    if not OPENAI_REALTIME_MODEL_ID: log("CRITICAL: OPENAI_REALTIME_MODEL_ID not found. Exiting."); exit(1)
    if not APP_CONFIG.get("FASTAPI_DISPLAY_API_URL") and not os.environ.get("CI_TEST_MODE"):
        log("WARNING: FASTAPI_DISPLAY_API_URL not found. Display tool will not function.")

    try:
        player_instance = PCMPlayer()
    except Exception as e_player_init:
        log(f"CRITICAL: Failed to initialize PCMPlayer: {e_player_init}. Exiting.");
        if p: p.terminate(); exit(1)

    log(f"Initial App State (determined after WW init): {current_app_state} (WW Active: {wake_word_active})")
    
    log(f"OpenAI Model: {OPENAI_REALTIME_MODEL_ID}")
    log(f"Audio Rates: MicIn={INPUT_RATE}Hz, PlayerOut={OUTPUT_RATE}Hz, WWProcess={WAKE_WORD_PROCESS_RATE}Hz, VADIn={VAD_SAMPLE_RATE if WEBRTC_VAD_AVAILABLE and LOCAL_VAD_ENABLED else 'N/A'}Hz")
    log(f"Chunk MS: {CHUNK_MS}")
    # log(f"Using uLaw for OpenAI Input: {USE_ULAW_FOR_OPENAI_INPUT}") # uLaw disabled for now
    log(f"Local VAD (WebRTC) Enabled: {LOCAL_VAD_ENABLED and WEBRTC_VAD_AVAILABLE}")
    if wake_word_active and wake_word_detector_instance and hasattr(wake_word_detector_instance, 'wake_word_model_name'): 
        log(f"WW ACTIVE: Model='{wake_word_detector_instance.wake_word_model_name}'.")
    else: log("WW INACTIVE.")
    log(f"Display API URL: {APP_CONFIG.get('FASTAPI_DISPLAY_API_URL', 'Not Set')}")

    ws_full_url = f"wss://api.openai.com/v1/realtime?model={OPENAI_REALTIME_MODEL_ID}"
    log(f"OpenAI WebSocket URL: {ws_full_url}")
    auth_headers = ["Authorization: Bearer " + OPENAI_API_KEY, "OpenAI-Beta: realtime=v1"]
    
    client_config = {
        **APP_CONFIG, 
        "CHUNK_MS": CHUNK_MS, 
        "USE_ULAW_FOR_OPENAI_INPUT": False # Explicitly False as uLaw is out for now
    }

    try:
        openai_client_instance = OpenAISpeechClient(
            ws_url_param=ws_full_url, headers_param=auth_headers, main_log_fn=log, 
            pcm_player=player_instance, app_state_setter=set_app_state_main, app_state_getter=get_app_state_main,
            input_rate_hz=INPUT_RATE, output_rate_hz=OUTPUT_RATE, is_ww_active=wake_word_active, 
            ww_detector_instance_ref=wake_word_detector_instance, app_config_dict=client_config 
        )
    except Exception as e_client_init: 
        log(f"CRITICAL ERROR: OpenAISpeechClient init failed: {e_client_init}. Exiting.")
        if player_instance: player_instance.close()
        if p: p.terminate(); exit(1)
        
    ws_client_thread = threading.Thread(target=openai_client_instance.run_client, daemon=True)
    ws_client_thread.start()

    connection_wait_timeout = 15  
    log(f"Waiting up to {connection_wait_timeout}s for OpenAI WebSocket connection...")
    time_waited = 0
    while openai_client_instance and not (hasattr(openai_client_instance, 'connected') and openai_client_instance.connected) and time_waited < connection_wait_timeout:
        if not ws_client_thread.is_alive(): log("ERROR: OpenAI client thread terminated prematurely."); break
        time.sleep(0.1); time_waited += 0.1
    
    audio_pipeline_thread = None
    if openai_client_instance and hasattr(openai_client_instance, 'connected') and openai_client_instance.connected:
        log("OpenAI WebSocket client connected. Starting audio pipeline.")
        audio_pipeline_thread = threading.Thread(target=continuous_audio_pipeline, args=(openai_client_instance,), daemon=True)
        audio_pipeline_thread.start()
    else:
        log("ERROR: Failed to connect to OpenAI WebSocket or client instance not available. Audio pipeline NOT started.")

    try:
        while ws_client_thread.is_alive(): 
            if audio_pipeline_thread and not audio_pipeline_thread.is_alive() and \
               openai_client_instance and (hasattr(openai_client_instance, 'connected') and openai_client_instance.connected):
                log("WARNING: Audio pipeline thread exited while OpenAI client connected.")
                break 
            time.sleep(0.5) 
        log("OpenAI client thread has finished or is no longer alive.")
    except KeyboardInterrupt: print("\nCtrl+C by main thread. Initiating shutdown...")
    finally:
        log_section("APPLICATION SHUTDOWN SEQUENCE")
        if openai_client_instance and hasattr(openai_client_instance, 'close_connection') and callable(openai_client_instance.close_connection):
            log("Closing OpenAI WS via client's close_connection method...")
            openai_client_instance.close_connection()
        elif openai_client_instance and hasattr(openai_client_instance, 'ws_app') and openai_client_instance.ws_app:
            if hasattr(openai_client_instance.ws_app, 'sock') and openai_client_instance.ws_app.sock and \
               (hasattr(openai_client_instance, 'connected') and openai_client_instance.connected):
                log("Closing OpenAI WS directly via ws_app.close()...")
                try:
                    if hasattr(openai_client_instance.ws_app, 'close'): openai_client_instance.ws_app.close()
                except Exception as e_ws_close: log(f"Exception during ws_app.close(): {e_ws_close}")
            elif not (hasattr(openai_client_instance, 'connected') and openai_client_instance.connected):
                 log("WS client indicates not connected, ws_app.close() skipped.")

        if audio_pipeline_thread and audio_pipeline_thread.is_alive():
            log("Waiting for audio pipeline thread to join..."); audio_pipeline_thread.join(timeout=3) 
            if audio_pipeline_thread.is_alive(): log("WARN: Audio pipeline thread did not join.")
        if ws_client_thread and ws_client_thread.is_alive():
            log("Waiting for OpenAI client thread to join..."); ws_client_thread.join(timeout=5) 
            if ws_client_thread.is_alive(): log("WARN: OpenAI client thread did not join.")
        
        if player_instance: log("Flushing/closing PCMPlayer."); player_instance.flush(); player_instance.close()
        if p: log("Terminating PyAudio."); p.terminate()
        log_section("APPLICATION FULLY ENDED")