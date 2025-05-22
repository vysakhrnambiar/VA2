# main.py
# Main application script for OpenAI Realtime Voice Assistant with Tools

import os
import json # For client sending tool result payload
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
    print("[MAIN_APP_SETUP] WARNING: scipy not installed. Resampling for wake word disabled. Wake word may not function if enabled.")

# --- Load Environment Variables ---
load_dotenv() 
# OpenAI Keys
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_REALTIME_MODEL_ID = os.getenv("OPENAI_REALTIME_MODEL_ID") 
# Resend Email Configuration
APP_CONFIG = {
    "RESEND_API_KEY": os.getenv("RESEND_API_KEY"), # Ensure this matches your .env key (was RESEND_API_TOKEN in one example)
    "DEFAULT_FROM_EMAIL": os.getenv("DEFAULT_FROM_EMAIL"),
    "RESEND_RECIPIENT_EMAILS": os.getenv("RESEND_RECIPIENT_EMAILS"),
    "RESEND_RECIPIENT_EMAILS_BCC": os.getenv("RESEND_RECIPIENT_EMAILS_BCC"),
    "TICKET_EMAIL": os.getenv("TICKET_EMAIL"), # Used by tool_executor for ticket destination
    "RESEND_API_URL": os.getenv("RESEND_API_URL", "https://api.resend.com/emails") # Default if not in .env
}
# Wake Word Configuration (used by WakeWordDetector internally, but good to acknowledge)
# WAKE_WORD_MODEL = os.getenv("WAKE_WORD_MODEL")
# WAKE_WORD_THRESHOLD = os.getenv("WAKE_WORD_THRESHOLD")


# --- Logging (main application logger) ---
def log(msg): 
    print(f"[MAIN_APP] {time.strftime('%Y-%m-%d %H:%M:%S')} {msg}")

def log_section(title):
    print(f"\n===== {title} =====")

# --- Import Custom Modules ---
log_section("Importing Custom Modules")
wake_word_detector_instance = None 
wake_word_active = False         
try:
    from wake_word_detector import WakeWordDetector
    log("Successfully imported WakeWordDetector.")
    if SCIPY_AVAILABLE: 
        try:
            wake_word_detector_instance = WakeWordDetector(sample_rate=16000) 
            is_dummy_check = "DummyOpenWakeWordModel" in str(type(wake_word_detector_instance.model)) if hasattr(wake_word_detector_instance, 'model') else True
            if hasattr(wake_word_detector_instance, 'model') and \
               wake_word_detector_instance.model is not None and not is_dummy_check :
                log(f"WakeWordDetector initialized: Model='{wake_word_detector_instance.wake_word_model_name}', Thr={wake_word_detector_instance.threshold}")
                wake_word_active = True
            else:
                log("WakeWordDetector init with DUMMY model or model is None. WW INACTIVE.")
                wake_word_active = False
        except Exception as e:
            log(f"CRITICAL ERROR WakeWordDetector init: {e}. WW INACTIVE.")
            wake_word_active = False
    else:
        log("Scipy unavailable, cannot resample for WW. WW DISABLED.")
        wake_word_active = False
except ImportError as e:
    log(f"Failed to import WakeWordDetector: {e}. WW DISABLED.")
    wake_word_active = False

if not wake_word_active: 
    class DummyWWDetector: 
        def __init__(self, *args, **kwargs): self.wake_word_model_name = "N/A - Inactive"
        def process_audio(self, audio_chunk): return False
        def reset(self): pass
    wake_word_detector_instance = DummyWWDetector()
    log("Using DUMMY wake word detector.")

openai_client_instance = None # Initialize before try-except for OpenAISpeechClient
try:
    from openai_client import OpenAISpeechClient 
    log("Successfully imported OpenAISpeechClient.")
except ImportError as e:
    log(f"CRITICAL ERROR: Failed to import OpenAISpeechClient from openai_client.py: {e}. Exiting.")
    exit(1) # Cannot proceed without the client

# tool_executor and tools_definition are used by openai_client.py, no direct import needed here
# unless main.py itself needs to call tool handlers, which it currently doesn't.

# --- Audio Configuration ---
INPUT_RATE = 24000 
OUTPUT_RATE = 24000 
WAKE_WORD_PROCESS_RATE = 16000 
CHUNK_MS = 30 
INPUT_CHUNK_SAMPLES = int(INPUT_RATE * CHUNK_MS / 1000) 
OUTPUT_PLAYER_CHUNK_SAMPLES = int(OUTPUT_RATE * CHUNK_MS / 1000) 
FORMAT = pyaudio.paInt16 
CHANNELS = 1            

# --- State Management ---
STATE_LISTENING_FOR_WAKEWORD = "LISTENING_FOR_WAKEWORD"
STATE_SENDING_TO_OPENAI = "SENDING_TO_OPENAI"
current_app_state = STATE_SENDING_TO_OPENAI
state_just_changed_to_sending = False  # Flag to track state change
if wake_word_active:
    current_app_state = STATE_LISTENING_FOR_WAKEWORD
state_lock = threading.Lock()

def set_app_state_main(new_state):
    global current_app_state, state_just_changed_to_sending
    with state_lock:
        if current_app_state != new_state:
            log(f"App State changed: {current_app_state} -> {new_state}")
            current_app_state = new_state
            # Set flag when changing to SENDING_TO_OPENAI
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
        self.stream = None # Initialize to None
        try:
            self.stream = p.open(format=format_player, channels=channels, rate=rate, output=True, frames_per_buffer=chunk_samples_player)
        except Exception as e_pyaudio:
            log(f"CRITICAL ERROR initializing PyAudio output stream: {e_pyaudio}")
            # Potentially exit or run in a mode without audio playback
            raise # Re-raise for now to make it obvious
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
                self.close() # Attempt to close cleanly
                break 
    def flush(self): 
        if not self.stream or not self.buffer: return
        try:
            self.stream.write(self.buffer) 
        except IOError as e:
            log(f"PCMPlayer IOError during flush: {e}.")
            self.close()
        finally:
            self.buffer = b""
    def clear(self): self.buffer = b"" 
    def close(self): 
        if self.stream:
            try:
                if self.stream.is_active(): self.stream.stop_stream()
                if not self.stream.is_stopped(): self.stream.stop_stream() # Ensure stopped
                self.stream.close()
            except Exception as e_close:
                log(f"PCMPlayer error during close: {e_close}")
            finally:
                self.stream = None 
                log("PCMPlayer stream closed by main_app.")
player_instance = None 

def get_input_stream(): 
    log(f"PyAudio Input Stream Open Request: Rate={INPUT_RATE}, ChunkSize={INPUT_CHUNK_SAMPLES}")
    try:
        return p.open(format=FORMAT, channels=CHANNELS, rate=INPUT_RATE, input=True, frames_per_buffer=INPUT_CHUNK_SAMPLES)
    except Exception as e_pyaudio_in:
        log(f"CRITICAL ERROR initializing PyAudio input stream: {e_pyaudio_in}")
        return None # Return None to indicate failure

# --- Continuous Audio Streaming Logic ---
def continuous_audio_pipeline(openai_client_ref): 
    if not OPENAI_API_KEY: 
        log("CRITICAL: OpenAI API key not found in audio pipeline. Stopping.")
        return

    mic_stream = get_input_stream()
    if not mic_stream:
        log("CRITICAL: Failed to open microphone stream. Audio pipeline cannot start.")
        return
        
    log("Mic stream opened (24kHz).")
    log(f"Audio pipeline started. Initial state: {get_app_state_main()}")
    chunk_counter = 0 
    try:
        while True: 
            if not openai_client_ref.connected: 
                time.sleep(0.2) 
                if not hasattr(openai_client_ref, 'ws_app') or not openai_client_ref.ws_app or \
                   (hasattr(openai_client_ref.ws_app, 'keep_running') and not openai_client_ref.ws_app.keep_running): 
                    log("OpenAI WebSocket seems stopped (from audio pipeline). Exiting.")
                    break
                continue
            try:
                audio_bytes_24k = mic_stream.read(INPUT_CHUNK_SAMPLES, exception_on_overflow=False)
                expected_len = INPUT_CHUNK_SAMPLES * pyaudio.get_sample_size(FORMAT) * CHANNELS
                if len(audio_bytes_24k) != expected_len:
                    continue
            except IOError as e:
                log(f"IOError reading PyAudio stream: {e}. Exiting audio loop.")
                break 

            current_processing_state = get_app_state_main()
            audio_to_send_to_openai = audio_bytes_24k 

            if current_processing_state == STATE_LISTENING_FOR_WAKEWORD and wake_word_active:
                if SCIPY_AVAILABLE and wake_word_detector_instance:
                    audio_np_24k_int16 = np.frombuffer(audio_bytes_24k, dtype=np.int16)
                    num_samples_24k = len(audio_np_24k_int16)
                    num_samples_16k = int(num_samples_24k * WAKE_WORD_PROCESS_RATE / INPUT_RATE)

                    if num_samples_16k > 0:
                        audio_np_16k_float32 = signal.resample(audio_np_24k_int16.astype(np.float32), num_samples_16k)
                        audio_bytes_16k = audio_np_16k_float32.astype(np.int16).tobytes()
                        
                        chunk_counter += 1
                        if chunk_counter % 20 == 1: 
                            max_amplitude_24k = np.max(np.abs(audio_np_24k_int16)) if num_samples_24k > 0 else 0
                            max_amplitude_16k = np.max(np.abs(audio_np_16k_float32.astype(np.int16))) if num_samples_16k > 0 else 0
                            log(f"WW_DEBUG: Max amp 24k={max_amplitude_24k}, 16k={max_amplitude_16k}. Feed {len(audio_bytes_16k)}b WW.")
                        
                        detected = wake_word_detector_instance.process_audio(audio_bytes_16k)
                        if detected:
                            log_section(f"WAKE WORD DETECTED (main_app): '{wake_word_detector_instance.wake_word_model_name.upper()}'!")
                            set_app_state_main(STATE_SENDING_TO_OPENAI) 
                            if hasattr(wake_word_detector_instance, 'reset'):
                                wake_word_detector_instance.reset()
                            print("\n*** Wake word detected! Sending 24kHz audio to OpenAI... ***\n")
                        else:
                            continue 
                    else: 
                        log("WARN: Resampling for WW resulted in 0 samples. Skipping WW.")
                        continue 
                else: 
                    log("WARN: In WW listening state but cannot process (scipy/detector issue).")
                    continue 
            
            if get_app_state_main() == STATE_SENDING_TO_OPENAI:
                if hasattr(openai_client_ref, 'ws_app') and openai_client_ref.ws_app and openai_client_ref.connected:
                    audio_b64_str = base64.b64encode(audio_to_send_to_openai).decode('utf-8')
                    audio_msg_to_send = {"type": "input_audio_buffer.append", "audio": audio_b64_str}
                    try:
                        if hasattr(openai_client_ref.ws_app, 'send'):
                             openai_client_ref.ws_app.send(json.dumps(audio_msg_to_send))
                             
                             # Check if state just changed and send response.create
                             global state_just_changed_to_sending
                             if state_just_changed_to_sending:
                                 response_create_payload = {
                                     "type": "response.create",
                                     "response": {
                                         "modalities": ["text", "audio"],
                                         "voice": "ash",
                                         "output_audio_format": "pcm16"
                                     }
                                 }
                                 openai_client_ref.ws_app.send(json.dumps(response_create_payload))
                                 log("Sent initial 'response.create' after state change to trigger assistant")
                                 state_just_changed_to_sending = False  # Reset flag
                        else:
                             log("ERROR: openai_client_ref.ws_app not available or has no send method.")
                    except Exception as e_send:
                        log(f"Exception during WebSocket send from main_app: {e_send}")
                        import websocket # For checking exception type
                        if isinstance(e_send, websocket.WebSocketConnectionClosedException):
                            openai_client_ref.connected = False
    except KeyboardInterrupt:
        log("KeyboardInterrupt in audio pipeline.")
    except Exception as e_pipeline:
        log(f"Major exception in audio pipeline: {e_pipeline}")
    finally:
        log("Audio pipeline stopping. Closing mic stream...")
        if mic_stream and mic_stream.is_active():
            mic_stream.stop_stream()
            mic_stream.close()
        log("Mic stream closed.")

# --- Main Execution ---
if __name__ == "__main__":
    log_section("APPLICATION STARTING")

    if not OPENAI_API_KEY: log("CRITICAL: OPENAI_API_KEY not found. Exiting."); exit(1)
    if not OPENAI_REALTIME_MODEL_ID: log("CRITICAL: OPENAI_REALTIME_MODEL_ID not found. Exiting."); exit(1)
    
    # Initialize PCMPlayer here
    try:
        player_instance = PCMPlayer()
    except Exception as e_player_init:
        log(f"CRITICAL: Failed to initialize PCMPlayer: {e_player_init}. Audio playback will not work. Exiting.")
        if p: p.terminate()
        exit(1)

    log(f"Initial app state (before client): {current_app_state} (WW active: {wake_word_active})")

    if not SCIPY_AVAILABLE and wake_word_active: # Re-check and finalize state
        log("FATAL: Scipy unavailable, disabling WW. App in direct OpenAI mode.")
        wake_word_active = False 
        set_app_state_main(STATE_SENDING_TO_OPENAI)

    log(f"OpenAI Model: {OPENAI_REALTIME_MODEL_ID}")
    log(f"Audio Rates: Mic/OpenAI_In={INPUT_RATE}Hz, Player_Out={OUTPUT_RATE}Hz, WW_Process={WAKE_WORD_PROCESS_RATE}Hz.")
    if wake_word_active and wake_word_detector_instance:
        log(f"WW ACTIVE: Model='{wake_word_detector_instance.wake_word_model_name}'.")
    else:
        log("WW INACTIVE.")

    ws_full_url = f"wss://api.openai.com/v1/realtime?model={OPENAI_REALTIME_MODEL_ID}"
    log(f"OpenAI WebSocket URL: {ws_full_url}")
    
    auth_headers = ["Authorization: Bearer " + OPENAI_API_KEY, "OpenAI-Beta: realtime=v1"]
    
    try:
        openai_client_instance = OpenAISpeechClient(
            ws_url_param=ws_full_url, 
            headers_param=auth_headers,
            main_log_fn=log, 
            pcm_player=player_instance,
            app_state_setter=set_app_state_main,
            app_state_getter=get_app_state_main,
            input_rate_hz=INPUT_RATE,
            output_rate_hz=OUTPUT_RATE,
            is_ww_active=wake_word_active,
            ww_detector_instance_ref=wake_word_detector_instance,
            app_config_dict=APP_CONFIG # Pass the .env config dict
        )
    except NameError: 
        log("CRITICAL ERROR: OpenAISpeechClient class not defined (Import failed). Exiting.")
        if player_instance: player_instance.close()
        if p: p.terminate()
        exit(1)
        
    ws_client_thread = threading.Thread(target=openai_client_instance.run_client, daemon=True)
    ws_client_thread.start()

    connection_wait_timeout = 15  
    log(f"Waiting up to {connection_wait_timeout}s for WebSocket connection...")
    time_waited = 0
    # Initial prompt to user after connection is established
    initial_prompt_printed = False

    while not openai_client_instance.connected and time_waited < connection_wait_timeout:
        if not ws_client_thread.is_alive(): 
            log("ERROR: OpenAI client thread terminated prematurely. Cannot start audio.")
            break
        time.sleep(0.1)
        time_waited += 0.1
    
    audio_pipeline_thread = None
    if openai_client_instance.connected:
        log("WebSocket client connected. Starting audio pipeline.")
        # Print initial user guidance message AFTER client is connected and session likely created
        # The client's on_message for session.created will also print a similar message.
        # This can be coordinated better, but for now, let client handle its specific prompt.
        # This main prompt is more about app readiness.
        # if get_app_state_main() == STATE_LISTENING_FOR_WAKEWORD and wake_word_active:
        #     print(f"\n*** MAIN: Listening for wake word: '{wake_word_detector_instance.wake_word_model_name}' ***\n")
        # else:
        #     print(f"\n*** MAIN: Speak now to interact with OpenAI (WW inactive or sending mode). ***\n")

        audio_pipeline_thread = threading.Thread(target=continuous_audio_pipeline, args=(openai_client_instance,), daemon=True)
        audio_pipeline_thread.start()
    else:
        log("ERROR: Failed to connect to OpenAI WebSocket. Audio pipeline NOT started.")

    try:
        while ws_client_thread.is_alive(): 
            if audio_pipeline_thread and not audio_pipeline_thread.is_alive() and openai_client_instance.connected:
                log("WARNING: Audio pipeline thread exited while OpenAI client connected.")
                break 
            time.sleep(0.5) 
        log("OpenAI client thread has finished or is no longer alive.")
    except KeyboardInterrupt:
        print("\nCtrl+C by main thread. Initiating shutdown...")
    finally:
        log_section("APPLICATION SHUTDOWN SEQUENCE")
        if hasattr(openai_client_instance, 'ws_app') and openai_client_instance.ws_app and openai_client_instance.connected:
            log("Attempting to close OpenAI WebSocket connection...")
            try:
                if hasattr(openai_client_instance.ws_app, 'close'):
                    openai_client_instance.ws_app.close()
            except Exception as e_ws_close:
                log(f"Exception during WebSocket close: {e_ws_close}")
        
        if ws_client_thread and ws_client_thread.is_alive():
            log("Waiting for OpenAI client thread to join...")
            ws_client_thread.join(timeout=2)
        if audio_pipeline_thread and audio_pipeline_thread.is_alive():
            log("Waiting for audio pipeline thread to join...")
            audio_pipeline_thread.join(timeout=2)
        
        if player_instance and player_instance.stream:
            log("Closing PCMPlayer stream.")
            player_instance.close()
        if p: 
            log("Terminating PyAudio instance.")
            p.terminate()
        log_section("APPLICATION FULLY ENDED")