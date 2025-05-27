# openai_client.py
import json
import base64
import time
import threading 
import numpy as np
from pytsmod import wsola # Using pytsmod for TSM

# Imports from our other new modules
from tools_definition import ALL_TOOLS, END_CONVERSATION_TOOL_NAME 
from tools_definition import (
    SEND_EMAIL_SUMMARY_TOOL_NAME, 
    RAISE_TICKET_TOOL_NAME,
    GET_BOLT_KB_TOOL_NAME,
    GET_DTC_KB_TOOL_NAME,
    DISPLAY_ON_INTERFACE_TOOL_NAME,
)
from tool_executor import TOOL_HANDLERS 
from llm_prompt_config import INSTRUCTIONS as LLM_DEFAULT_INSTRUCTIONS

class OpenAISpeechClient:
    def __init__(self, ws_url_param, headers_param, main_log_fn, pcm_player, 
                 app_state_setter, app_state_getter, 
                 input_rate_hz, output_rate_hz, 
                 is_ww_active, ww_detector_instance_ref,
                 app_config_dict):
        self.ws_url = ws_url_param
        self.headers = headers_param
        self.log = main_log_fn 
        self.player = pcm_player
        self.set_app_state = app_state_setter
        self.get_app_state = app_state_getter
        self.wake_word_active = is_ww_active
        self.wake_word_detector_instance = ww_detector_instance_ref
        self.config = app_config_dict 

        self.ws_app = None
        self.connected = False
        self.session_id = None
        self.accumulated_tool_args = {} 

        self.last_assistant_item_id = None
        self.current_assistant_item_played_ms = 0
        self.client_audio_chunk_duration_ms = self.config.get("CHUNK_MS", 30) 
        self.client_initiated_truncated_item_ids = set() 
        
        self.use_ulaw_for_openai = self.config.get("USE_ULAW_FOR_OPENAI_INPUT", False)

        # --- TSM Attributes (for pytsmod) --- START ---
        # For pytsmod.wsola, 'alpha' is the speed factor.
        # alpha > 1.0 for speedup, alpha < 1.0 for slowdown.
        self.desired_playback_speed = float(self.config.get("TSM_PLAYBACK_SPEED", 1.0)) 
        self.tsm_enabled = self.desired_playback_speed != 1.0 

        self.openai_sample_rate = 24000
        # pytsmod.wsola primarily expects mono (1D array) or will process channels independently if given 2D.
        # For simplicity, we'll ensure our input to wsola is 1D float32 if original is mono.
        self.tsm_channels = 1 # Based on OpenAI output being mono PCM

        if self.tsm_enabled:
            self.log(f"TSM (pytsmod.wsola) enabled. Target speed factor (alpha): {self.desired_playback_speed}")
        else:
            self.log("TSM (pytsmod.wsola) disabled (speed is 1.0).")

        self.NUM_CHUNKS_FOR_TSM_WINDOW = int(self.config.get("TSM_WINDOW_CHUNKS", 8)) 
        self.BYTES_PER_OPENAI_CHUNK = (self.openai_sample_rate * self.client_audio_chunk_duration_ms // 1000) * (16 // 8) * self.tsm_channels
        self.TSM_PROCESSING_THRESHOLD_BYTES = self.BYTES_PER_OPENAI_CHUNK * self.NUM_CHUNKS_FOR_TSM_WINDOW
        
        self.openai_audio_buffer_raw_bytes = b'' 
        if self.tsm_enabled: 
            self.log(f"TSM (pytsmod.wsola) processing threshold set to {self.TSM_PROCESSING_THRESHOLD_BYTES} bytes (~{self.NUM_CHUNKS_FOR_TSM_WINDOW} OpenAI chunks).")
        # --- TSM Attributes (for pytsmod) --- END ---

    def _log_section(self, title): 
        self.log(f"\n===== [Client] {title} =====")

    def _process_and_play_audio(self, audio_data_bytes: bytes):
        """
        Buffers incoming audio, applies TSM with pytsmod.wsola if enabled, and sends to player.
        """
        if not self.tsm_enabled:
            if self.player:
                self.player.play(audio_data_bytes)
            return

        self.openai_audio_buffer_raw_bytes += audio_data_bytes

        while len(self.openai_audio_buffer_raw_bytes) >= self.TSM_PROCESSING_THRESHOLD_BYTES:
            segment_to_process_bytes = self.openai_audio_buffer_raw_bytes[:self.TSM_PROCESSING_THRESHOLD_BYTES]
            self.openai_audio_buffer_raw_bytes = self.openai_audio_buffer_raw_bytes[self.TSM_PROCESSING_THRESHOLD_BYTES:]

            try:
                segment_np_int16 = np.frombuffer(segment_to_process_bytes, dtype=np.int16)
                # pytsmod.wsola expects a 1D (for mono) or 2D (for multi-channel) float array.
                # Normalizing to -1.0 to 1.0 is good practice.
                segment_np_float32 = segment_np_int16.astype(np.float32) / 32768.0 
                
                if segment_np_float32.size == 0:
                    continue 

                # self.log(f"DEBUG_TSM: Input array shape to wsola: {segment_np_float32.shape}, SR: {self.openai_sample_rate}, Alpha: {self.desired_playback_speed}")
                
                # Perform time stretching using pytsmod.wsola
                # x: input signal (1D or 2D NumPy array)
                # alpha: ratio by which the length of the signal is changed ( > 1 for speedup)
                # Fs: sample rate
                self.log(f"Blocking call start ")
                stretched_audio_float32 = wsola(
                    x=segment_np_float32, 
                    s=self.desired_playback_speed 
                    #Fs=self.openai_sample_rate
                )
                self.log(f"BLocking call end.")
                # self.log(f"DEBUG_TSM: Output array shape from wsola: {stretched_audio_float32.shape}")
                
                # Convert back to int16 bytes
                clipped_stretched_audio = np.clip(stretched_audio_float32, -1.0, 1.0)
                stretched_audio_int16 = (clipped_stretched_audio * 32767.0).astype(np.int16)
                stretched_audio_bytes = stretched_audio_int16.tobytes()

                if self.player and len(stretched_audio_bytes) > 0:
                    self.player.play(stretched_audio_bytes)

            except Exception as e_tsm_proc:
                self.log(f"ERROR during TSM processing with pytsmod.wsola: {e_tsm_proc}. Playing segment directly.")
                if self.player: 
                    self.player.play(segment_to_process_bytes) 

    def on_open(self, ws): 
        self._log_section("WebSocket OPEN")
        self.log("Client: Connected to OpenAI Realtime API")
        self.connected = True 
        
        llm_instructions = LLM_DEFAULT_INSTRUCTIONS
        input_format_to_use = "g711_ulaw" if self.use_ulaw_for_openai else "pcm16"
        
        session_config = {
            "type": "session.update",
            "session": {
                "voice": self.config.get("OPENAI_VOICE", "ash"), 
                "turn_detection": {
                    "type": "server_vad",
                    "interrupt_response": True 
                },
                "input_audio_format": input_format_to_use, 
                "output_audio_format": "pcm16",
                "tools": ALL_TOOLS, 
                "tool_choice": "auto", 
                "instructions": llm_instructions,
                "input_audio_transcription": {
                    "model": "whisper-1",   # or another supported model if needed
                    # "language": "en",    # Optional: specify language if needed
                    # "timestamp_granularities": ["word", "segment"], # Optional
                }
            }
        }
        ws.send(json.dumps(session_config))
        self.log(f"Client: Session config sent. Input format: {input_format_to_use}. Instructions: {len(llm_instructions)} chars. Voice: '{session_config['session']['voice']}'. interrupt_response: True.")

    def _execute_tool_in_thread(self, handler_function, parsed_args, call_id, config, function_name):
        self.log(f"Client (Thread - {function_name}): Starting execution for Call_ID {call_id}. Args: {parsed_args}")
        tool_output_for_llm = ""
        try:
            tool_result_str = handler_function(**parsed_args, config=config)
            tool_output_for_llm = str(tool_result_str)
            self.log(f"Client (Thread - {function_name}): Execution complete. Result snippet: '{tool_output_for_llm[:150]}...'")
        except Exception as e_tool_exec_thread:
            self.log(f"Client (Thread - {function_name}) ERROR: Exception during execution: {e_tool_exec_thread}")
            error_detail = f"An error occurred while executing the tool '{function_name}': {str(e_tool_exec_thread)}"
            tool_output_for_llm = json.dumps({"error": error_detail})
            self.log(f"Client (Thread - {function_name}): Sending error back to LLM: {tool_output_for_llm}")

        tool_response_payload = {"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": call_id, "output": tool_output_for_llm}}
        if self.ws_app and self.connected:
            try:
                self.ws_app.send(json.dumps(tool_response_payload))
                self.log(f"Client (Thread - {function_name}): Sent tool output for Call_ID='{call_id}'.")
                response_create_payload = {"type": "response.create", "response": {"modalities": ["text", "audio"], "voice": self.config.get("OPENAI_VOICE", "ash"), "output_audio_format": "pcm16"}}
                self.ws_app.send(json.dumps(response_create_payload))
                self.log(f"Client (Thread - {function_name}): Sent 'response.create' to trigger assistant after tool output for Call_ID='{call_id}'.")
            except Exception as e_send_thread:
                self.log(f"Client (Thread - {function_name}) ERROR: Could not send tool output or response.create for Call_ID='{call_id}': {e_send_thread}")
        else:
            self.log(f"Client (Thread - {function_name}) ERROR: WebSocket not available/connected. Cannot send tool output for Call_ID='{call_id}'.")

    def is_assistant_speaking(self) -> bool:
        return self.last_assistant_item_id is not None

    def get_current_assistant_speech_duration_ms(self) -> int:
        if self.last_assistant_item_id:
            return self.current_assistant_item_played_ms 
        return 0

    def _perform_truncation(self, reason_prefix: str):
        item_id_to_truncate = self.last_assistant_item_id 
        if not item_id_to_truncate: 
            self.log(f"Client ({reason_prefix}): No active assistant item ID to truncate.")
            return
        self.player.clear() 
        self.openai_audio_buffer_raw_bytes = b'' # Clear TSM input buffer

        timestamp_to_send_ms = max(10, self.current_assistant_item_played_ms) 
        truncate_payload = {"type": "conversation.item.truncate", "item_id": item_id_to_truncate, "content_index": 0, "audio_end_ms": timestamp_to_send_ms}
        self.log(f"Client ({reason_prefix}): Will attempt to truncate item {item_id_to_truncate} with audio_end_ms: {timestamp_to_send_ms}ms")
        try:
            if self.ws_app and self.connected:
                self.ws_app.send(json.dumps(truncate_payload))
                self.log(f"Client ({reason_prefix}): Sent conversation.item.truncate for item_id: {item_id_to_truncate}")
                self.client_initiated_truncated_item_ids.add(item_id_to_truncate)
        except Exception as e_send_truncate:
            self.log(f"Client ({reason_prefix}) ERROR: Could not send truncate message: {e_send_truncate}")
        self.log(f"Client ({reason_prefix}): Resetting current assistant item tracking (was {self.last_assistant_item_id}).")
        self.last_assistant_item_id = None 
        self.current_assistant_item_played_ms = 0

    def handle_local_user_speech_interrupt(self):
        self.log("Client: Received signal for local user speech interrupt.")
        if self.get_app_state() == "SENDING_TO_OPENAI":
            self._perform_truncation(reason_prefix="Local VAD")
        else:
            self.log("Client (Local VAD): User speech detected, but app not in SENDING_TO_OPENAI state. No truncation action.")

    def on_message(self, ws, message_str):
        msg = json.loads(message_str)
        msg_type = msg.get("type")

        if msg_type not in ["response.audio.delta", "response.audio_transcript.delta", 
                            "input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped",
                            "response.output.delta", "response.function_call_arguments.delta"]:
            log_content = json.dumps(msg, indent=2) if len(message_str) < 500 else str(msg)[:500] + "..."
            self.log(f"Client RAW_MSG TYPE: {msg_type} | CONTENT_SNIPPET: {log_content}")

        if msg_type == "conversation.item.created":
            item = msg.get("item", {})
            item_id, item_role, item_type, item_status = item.get("id"), item.get("role"), item.get("type"), item.get("status")
            if item_role == "assistant" and item_type == "message" and item_status == "in_progress":
                if self.last_assistant_item_id != item_id: 
                    self.log(f"Client: New assistant message item starting. ID: {item_id}. Resetting played duration.")
                    self.last_assistant_item_id = item_id
                    self.current_assistant_item_played_ms = 0
        
        elif msg_type == "response.output.delta":
            delta_content = msg.get("delta", {}).get("tool_calls", [])
            for tc_obj in delta_content:
                if isinstance(tc_obj, dict):
                    call_id, fn_name = tc_obj.get("id"), tc_obj.get('function',{}).get('name')
                    fn_args_partial = tc_obj.get('function',{}).get('arguments',"")
                    if call_id and fn_name: 
                        self.accumulated_tool_args[call_id] = self.accumulated_tool_args.get(call_id, "") + fn_args_partial
        
        elif msg_type == "response.function_call_arguments.delta":
            call_id, delta_args = msg.get("call_id"), msg.get("delta", "") 
            if call_id: self.accumulated_tool_args[call_id] = self.accumulated_tool_args.get(call_id, "") + delta_args

        elif msg_type == "response.function_call_arguments.done":
            call_id = msg.get("call_id")
            function_to_execute_name = msg.get("name") 
            final_args_str_from_event = msg.get("arguments", "{}")
            final_accumulated_args = self.accumulated_tool_args.pop(call_id, "{}") 
            final_args_to_use = final_args_str_from_event if (final_args_str_from_event and final_args_str_from_event != "{}") else final_accumulated_args
            
            if not function_to_execute_name:
                self.log(f"Client WARN: 'function_call_arguments.done' for Call_ID='{call_id}' missing function name. Args='{final_args_to_use}'.")
                return

            self.log(f"Client: Function Call Finalized by LLM: Name='{function_to_execute_name}', Call_ID='{call_id}', Args='{final_args_to_use}'")
            parsed_args = {}
            try:
                if final_args_to_use: parsed_args = json.loads(final_args_to_use) 
            except json.JSONDecodeError as e:
                self.log(f"Client WARN: Could not decode JSON arguments for {function_to_execute_name}: '{final_args_to_use}'. Error: {e}")
                error_detail_for_llm = f"Invalid JSON arguments for tool {function_to_execute_name}. Error: {str(e)}"
                error_output_for_llm = json.dumps({"error": error_detail_for_llm })
                error_result_payload = {"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": call_id, "output": error_output_for_llm }}
                try:
                    if self.ws_app and self.connected:
                        ws.send(json.dumps(error_result_payload))
                        ws.send(json.dumps({"type": "response.create", "response": {"modalities": ["text", "audio"], "voice": self.config.get("OPENAI_VOICE", "ash")}}))
                except Exception as e_send_err: self.log(f"Client ERROR sending arg parsing error: {e_send_err}")
                return 

            if function_to_execute_name == END_CONVERSATION_TOOL_NAME:
                reason = parsed_args.get("reason", "No reason specified by LLM.")
                self.log(f"Client: LLM requests '{END_CONVERSATION_TOOL_NAME}'. Reason: '{reason}'.")
                end_conv_delay_s = self.config.get("END_CONV_AUDIO_FINISH_DELAY_S", 2.0)
                if self.player and (len(self.player.buffer) > 0 or self.last_assistant_item_id):
                    self.log(f"Client (End_Conv): Player might have audio. Waiting {end_conv_delay_s}s...")
                    time.sleep(end_conv_delay_s) 
                else:
                    time.sleep(0.2) 
                self.log(f"Client: Executing '{END_CONVERSATION_TOOL_NAME}' (after delay) for reason: '{reason}'.")
                if self.wake_word_active: 
                    if self.player: self.player.clear(); self.player.flush() 
                    self.set_app_state("LISTENING_FOR_WAKEWORD") 
                    print(f"\n*** Assistant listening for wake word: '{self.wake_word_detector_instance.wake_word_model_name}' (Reason: {reason}) ***\n")
                else: 
                    if self.player: self.player.flush()
                    print(f"\n*** Conversation turn ended by LLM (Reason: {reason}). Ready for next query. ***\n")
                self.last_assistant_item_id = None 
                self.current_assistant_item_played_ms = 0
                return

            elif function_to_execute_name in TOOL_HANDLERS:
                handler_function = TOOL_HANDLERS[function_to_execute_name]
                tool_thread = threading.Thread(target=self._execute_tool_in_thread, args=(handler_function, parsed_args, call_id, self.config, function_to_execute_name), daemon=True)
                tool_thread.start()
                return 
            else: 
                self.log(f"Client WARN: No handler for function '{function_to_execute_name}'. Call_ID='{call_id}'.")
                unhandled_error_out = json.dumps({"error": f"Tool '{function_to_execute_name}' not implemented by client."})
                error_payload = {"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": call_id, "output": unhandled_error_out}}
                try:
                    if self.ws_app and self.connected:
                        ws.send(json.dumps(error_payload))
                        ws.send(json.dumps({"type": "response.create", "response": {"modalities": ["text", "audio"], "voice": self.config.get("OPENAI_VOICE", "ash")}}))
                except Exception as e_send_unhandled: self.log(f"Client ERROR sending unhandled tool error: {e_send_unhandled}")
                return

        elif msg_type == "session.created":
            self.session_id = msg.get('session', {}).get('id')
            expires_at_ts = msg.get('session', {}).get('expires_at', 0)
            self.log(f"Client: OpenAI Session created: {self.session_id}, Expires At (Unix): {expires_at_ts}")
            if expires_at_ts > 0:
                try: self.log(f"Client: Session expiry datetime: {time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime(expires_at_ts))}")
                except: self.log("Client: Could not parse session expiry to datetime.")
            turn_detection_settings = msg.get('session', {}).get('turn_detection', {})
            self.log(f"Client: Server turn_detection settings: {json.dumps(turn_detection_settings)}")
            if self.get_app_state() == "LISTENING_FOR_WAKEWORD" and self.wake_word_active:
                 print(f"\n*** CLIENT: Listening for wake word: '{self.wake_word_detector_instance.wake_word_model_name}' ***\n")
            else:
                 print(f"\n*** CLIENT: Speak now to interact with OpenAI (WW inactive or sending mode). ***\n")

        elif msg_type == "response.audio.delta":
            audio_data_b64 = msg.get("delta")
            item_id_of_delta = msg.get("item_id") 
            if item_id_of_delta and item_id_of_delta in self.client_initiated_truncated_item_ids:
                pass
            elif audio_data_b64:
                audio_data_bytes = base64.b64decode(audio_data_b64)
                self._process_and_play_audio(audio_data_bytes) 
                if self.last_assistant_item_id and self.last_assistant_item_id == item_id_of_delta:
                    self.current_assistant_item_played_ms += self.client_audio_chunk_duration_ms
        
        elif msg_type == "response.audio.done":
            self.log("Client: OpenAI Audio reply 'done' received.")
            if self.tsm_enabled:
                if len(self.openai_audio_buffer_raw_bytes) > 0:
                    self.log(f"Client (audio.done): Processing {len(self.openai_audio_buffer_raw_bytes)} remaining bytes with pytsmod.wsola.")
                    final_segment_to_process_bytes_for_fallback = self.openai_audio_buffer_raw_bytes 
                    try:
                        final_segment_bytes = self.openai_audio_buffer_raw_bytes
                        self.openai_audio_buffer_raw_bytes = b'' 
                        segment_np_int16 = np.frombuffer(final_segment_bytes, dtype=np.int16)
                        segment_np_float32 = segment_np_int16.astype(np.float32) / 32768.0
                        if segment_np_float32.size > 0:
                            stretched_audio_float32 = wsola(segment_np_float32, s=self.desired_playback_speed) # <--- CORRECTED: use 's'
                            clipped_stretched_audio = np.clip(stretched_audio_float32, -1.0, 1.0)
                            stretched_audio_int16 = (clipped_stretched_audio * 32767.0).astype(np.int16)
                            stretched_audio_bytes = stretched_audio_int16.tobytes()
                            if self.player and len(stretched_audio_bytes) > 0:
                                self.player.play(stretched_audio_bytes)
                    except Exception as e_tsm_flush_proc:
                        self.log(f"ERROR during TSM final processing (pytsmod.wsola) on audio.done: {e_tsm_flush_proc}. Playing raw if any.")
                        if self.player and final_segment_to_process_bytes_for_fallback and len(final_segment_to_process_bytes_for_fallback) > 0:
                           self.player.play(final_segment_to_process_bytes_for_fallback)
            else: 
                if len(self.openai_audio_buffer_raw_bytes) > 0 and self.player:
                    self.player.play(self.openai_audio_buffer_raw_bytes)
                    self.openai_audio_buffer_raw_bytes = b''
            if self.player: self.player.flush() 
            self.log(f"Client: Audio done. Current app state: {self.get_app_state()}.")
            if not (self.get_app_state() == "LISTENING_FOR_WAKEWORD" and self.wake_word_active):
                print(f"\n*** Assistant has finished speaking. Ready for your next query. (Ctrl+C to exit) ***\n")

        elif msg_type == "response.output_item.done":
            item_done = msg.get("item", {})
            item_id_done = item_done.get("id")
            if self.last_assistant_item_id and self.last_assistant_item_id == item_id_done:
                self.log(f"Client: Current assistant message item {item_id_done} is now fully done. Clearing tracking.")
                self.last_assistant_item_id = None
                self.current_assistant_item_played_ms = 0
            if item_id_done in self.client_initiated_truncated_item_ids:
                self.log(f"Client: Removing {item_id_done} from client_initiated_truncated_item_ids.")
                self.client_initiated_truncated_item_ids.discard(item_id_done)
        
        elif msg_type == "response.done": 
            response_details = msg.get("response", {})
            if response_details.get("status") == "cancelled":
                self.log(f"Client: response.done with status 'cancelled'. Cleaning up.")
                for item_in_cancelled in response_details.get("output", []):
                    if isinstance(item_in_cancelled, dict):
                        item_id_cancelled = item_in_cancelled.get("id")
                        if item_id_cancelled:
                            self.client_initiated_truncated_item_ids.discard(item_id_cancelled)
                            if self.last_assistant_item_id == item_id_cancelled:
                                self.last_assistant_item_id = None; self.current_assistant_item_played_ms = 0
        elif msg_type == "input_audio_buffer.speech_started": 
            self.log(f"Client: !!! Server VAD: Speech Started !!! State: {self.get_app_state()}")
            if self.get_app_state() == "SENDING_TO_OPENAI": self._perform_truncation(reason_prefix="Server VAD")
        elif msg_type == "input_audio_buffer.speech_stopped":
            self.log("Client: Server VAD: Speech Stopped.")
        elif msg_type == "error":
            error_message = msg.get('error', {}).get('message', 'Unknown error from OpenAI.')
            self.log(f"Client ERROR from OpenAI: {error_message}")
            if "session" in error_message.lower() or "authorization" in error_message.lower():
                self.log("Client: Critical OpenAI session/auth error. Closing connection."); self.connected = False 
                if self.ws_app: self.ws_app.close()

    def on_error(self, ws, error): 
        self._log_section("WebSocket ERROR"); self.log(f"Client: WebSocket error: {error}"); self.connected = False
        self.last_assistant_item_id = None; self.current_assistant_item_played_ms = 0
        self.accumulated_tool_args.clear(); self.client_initiated_truncated_item_ids.clear()

    def on_close(self, ws, close_status_code, close_msg): 
        self._log_section("WebSocket CLOSE"); self.log(f"Client: WebSocket closed: Code={close_status_code}, Reason='{close_msg}'"); self.connected = False
        self.last_assistant_item_id = None; self.current_assistant_item_played_ms = 0
        self.accumulated_tool_args.clear(); self.client_initiated_truncated_item_ids.clear()

    def run_client(self): 
        self.log(f"Client: Attempting WebSocket connection to: {self.ws_url}")
        try: import websocket 
        except ImportError: self.log("CRITICAL: websocket-client library not found. pip install websocket-client"); return 
        self.ws_app = websocket.WebSocketApp(self.ws_url, header=self.headers, on_open=self.on_open, on_message=self.on_message, on_error=self.on_error, on_close=self.on_close)
        self.ws_app.run_forever(ping_interval=70, ping_timeout=30) 

    def close_connection(self): 
        self.log("Client: close_connection() called.")
        if self.ws_app:
            self.log("Client: Closing WebSocket connection from client side.")
            try:
                if hasattr(self.ws_app, 'keep_running'): self.ws_app.keep_running = False 
                if hasattr(self.ws_app, 'close'): self.ws_app.close()
            except Exception as e: self.log(f"Client: Error during ws_app.close(): {e}")
        self.connected = False