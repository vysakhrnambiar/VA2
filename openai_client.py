# openai_client.py
import json
import base64
import time
import threading 

# Imports from our other new modules
from tools_definition import ALL_TOOLS, END_CONVERSATION_TOOL_NAME 
from tools_definition import (
    SEND_EMAIL_SUMMARY_TOOL_NAME, 
    RAISE_TICKET_TOOL_NAME,
    GET_BOLT_KB_TOOL_NAME,
    GET_DTC_KB_TOOL_NAME,
    DISPLAY_ON_INTERFACE_TOOL_NAME,
    # Add new tool name constants here if used, e.g.:
    # GET_DAILY_EXECUTIVE_BRIEFING_TOOL_NAME 
)
from tool_executor import TOOL_HANDLERS 
from llm_prompt_config import INSTRUCTIONS as LLM_DEFAULT_INSTRUCTIONS

class OpenAISpeechClient:
    def __init__(self, ws_url_param, headers_param, main_log_fn, pcm_player, 
                 app_state_setter, app_state_getter, 
                 input_rate_hz, output_rate_hz, # These might not be strictly needed by client itself
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

        # For truncation logic
        self.last_assistant_item_id = None
        self.current_assistant_item_played_ms = 0
        self.client_audio_chunk_duration_ms = self.config.get("CHUNK_MS", 30) 
        self.client_initiated_truncated_item_ids = set() 
        
        # For dynamic input audio format (though uLaw is currently off in your main.py)
        self.use_ulaw_for_openai = self.config.get("USE_ULAW_FOR_OPENAI_INPUT", False)

    def _log_section(self, title): 
        self.log(f"\n===== [Client] {title} =====")

    def on_open(self, ws): 
        self._log_section("WebSocket OPEN")
        self.log("Client: Connected to OpenAI Realtime API")
        self.connected = True 
        
        llm_instructions = LLM_DEFAULT_INSTRUCTIONS
        input_format_to_use = "g711_ulaw" if self.use_ulaw_for_openai else "pcm16"
        
        session_config = {
            "type": "session.update",
            "session": {
                "voice": "ash",
                "turn_detection": {
                    "type": "server_vad",
                    "interrupt_response": True 
                },
                "input_audio_format": input_format_to_use, 
                "output_audio_format": "pcm16",
                "tools": ALL_TOOLS, 
                "tool_choice": "auto", 
                "instructions": llm_instructions
            }
        }
        ws.send(json.dumps(session_config))
        self.log(f"Client: Session config sent. Input format: {input_format_to_use}. Instructions: {len(llm_instructions)} chars. Voice: 'ash'. interrupt_response: True.")

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

        tool_response_payload = {
            "type": "conversation.item.create",
            "item": {"type": "function_call_output", "call_id": call_id, "output": tool_output_for_llm}
        }

        if self.ws_app and self.connected:
            try:
                self.ws_app.send(json.dumps(tool_response_payload))
                self.log(f"Client (Thread - {function_name}): Sent tool output for Call_ID='{call_id}'.")

                response_create_payload = {
                    "type": "response.create",
                    "response": {"modalities": ["text", "audio"], "voice": "ash", "output_audio_format": "pcm16"}
                }
                self.ws_app.send(json.dumps(response_create_payload))
                self.log(f"Client (Thread - {function_name}): Sent 'response.create' to trigger assistant after tool output for Call_ID='{call_id}'.")
            except Exception as e_send_thread:
                self.log(f"Client (Thread - {function_name}) ERROR: Could not send tool output or response.create for Call_ID='{call_id}': {e_send_thread}")
        else:
            self.log(f"Client (Thread - {function_name}) ERROR: WebSocket not available/connected. Cannot send tool output for Call_ID='{call_id}'.")

    # --- Methods for Local VAD Integration & Truncation ---
    def is_assistant_speaking(self) -> bool:
        """Indicates if an assistant message is likely being spoken or generated."""
        return self.last_assistant_item_id is not None

    def get_current_assistant_speech_duration_ms(self) -> int:
        """Returns the estimated played duration of the current assistant message item."""
        if self.last_assistant_item_id:
            return self.current_assistant_item_played_ms
        return 0

    def _perform_truncation(self, reason_prefix: str):
        """Helper to perform player clear and send truncate message."""
        item_id_to_truncate = self.last_assistant_item_id 
        if not item_id_to_truncate: # Check the stored ID
            self.log(f"Client ({reason_prefix}): No active assistant item ID to truncate.")
            return

        self.player.clear() 

        timestamp_to_send_ms = 10 
        if self.current_assistant_item_played_ms > 0:
            timestamp_to_send_ms = self.current_assistant_item_played_ms
        # else: # Log if default 10ms is used
            # self.log(f"Client ({reason_prefix}): current_assistant_item_played_ms was 0 for {item_id_to_truncate}, sending default {timestamp_to_send_ms}ms.")

        truncate_payload = {
            "type": "conversation.item.truncate",
            "item_id": item_id_to_truncate,
            "content_index": 0, 
            "audio_end_ms": timestamp_to_send_ms
        }
        
        self.log(f"Client ({reason_prefix}): Will attempt to truncate item {item_id_to_truncate} [content_index 0] with latest_media_timestamp_ms: {timestamp_to_send_ms}ms")

        try:
            if self.ws_app and self.connected:
                self.ws_app.send(json.dumps(truncate_payload))
                self.log(f"Client ({reason_prefix}): Sent conversation.item.truncate for item_id: {item_id_to_truncate}")
                self.client_initiated_truncated_item_ids.add(item_id_to_truncate)
                self.log(f"Client ({reason_prefix}): Added {item_id_to_truncate} to client_initiated_truncated_item_ids (now {len(self.client_initiated_truncated_item_ids)} items).")
            else:
                self.log(f"Client ({reason_prefix}) WARN: Cannot send truncate, WebSocket not available/connected.")
        except Exception as e_send_truncate:
            self.log(f"Client ({reason_prefix}) ERROR: Could not send truncate message: {e_send_truncate}")
        
        # Reset tracking for the current assistant item, as we've attempted to truncate it.
        # A new item will need to start for self.last_assistant_item_id to be set again.
        self.log(f"Client ({reason_prefix}): Resetting current assistant item tracking (was {self.last_assistant_item_id}).")
        self.last_assistant_item_id = None 
        self.current_assistant_item_played_ms = 0

    def handle_local_user_speech_interrupt(self):
        """Called by main.py when its local VAD detects user speech during assistant output."""
        self.log("Client: Received signal for local user speech interrupt.")
        if self.get_app_state() == "SENDING_TO_OPENAI":
            self._perform_truncation(reason_prefix="Local VAD")
        else:
            self.log("Client (Local VAD): User speech detected, but app not in SENDING_TO_OPENAI state. No truncation action.")

    def on_message(self, ws, message_str):
        msg = json.loads(message_str)
        msg_type = msg.get("type")

        if msg_type in ["response.audio.delta", "response.audio_transcript.delta", 
                        "input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped"]:
            pass # Handled specifically below, too noisy for general RAW_MSG log
        else: 
            log_content = json.dumps(msg, indent=2) if len(message_str) < 1000 else str(msg)
            #self.log(f"Client RAW_MSG TYPE: {msg_type} | CONTENT: {log_content}")

        if msg_type == "conversation.item.created":
            item = msg.get("item", {})
            item_id = item.get("id")
            item_role = item.get("role")
            item_type = item.get("type")
            item_status = item.get("status")

            if item_role == "assistant" and item_type == "message" and item_status == "in_progress":
                if self.last_assistant_item_id != item_id: 
                    self.log(f"Client: New assistant message item starting. ID: {item_id}. Resetting played duration.")
                    self.last_assistant_item_id = item_id
                    self.current_assistant_item_played_ms = 0
        
        elif msg_type == "response.output.delta":
            item_type_od = msg.get("item_type") 
            delta_content = msg.get("delta") 
            if item_type_od == "tool_calls": 
                if isinstance(delta_content, dict) and "tool_calls" in delta_content:
                    # ... (tool argument accumulation logic as in your provided code) ...
                    tc_array = delta_content.get("tool_calls", [])
                    for tc_obj in tc_array:
                        if isinstance(tc_obj, dict):
                            call_id = tc_obj.get("id")
                            fn_name = tc_obj.get('function',{}).get('name')
                            fn_args_partial = tc_obj.get('function',{}).get('arguments',"")
                            if call_id and fn_name:
                                self.accumulated_tool_args[call_id] = self.accumulated_tool_args.get(call_id, "") + fn_args_partial
        
        elif msg_type == "response.function_call_arguments.delta":
            call_id = msg.get("call_id")
            delta_args = msg.get("delta", "") 
            self.accumulated_tool_args[call_id] = self.accumulated_tool_args.get(call_id, "") + delta_args

        elif msg_type == "response.function_call_arguments.done":
            # (This entire block is identical to your provided version with always-threaded tools)
            call_id = msg.get("call_id")
            function_to_execute_name = msg.get("name") 
            final_args_str_from_event = msg.get("arguments", "{}")
            # ... (rest of arg parsing and tool dispatching logic) ...
            final_accumulated_args = self.accumulated_tool_args.pop(call_id, "{}") 
            final_args_to_use = final_args_str_from_event
            if (not final_args_str_from_event or final_args_str_from_event == "{}") and \
               (final_accumulated_args and final_accumulated_args != "{}"):
                self.log(f"Client: Using accumulated args for Call_ID {call_id}.")
                final_args_to_use = final_accumulated_args
            
            if not function_to_execute_name:
                self.log(f"Client WARN: 'function_call_arguments.done' for Call_ID='{call_id}' did not include function name. Args='{final_args_to_use}'.")
                return

            self.log(f"Client: Function Call Finalized by LLM: Name='{function_to_execute_name}', Call_ID='{call_id}', Args='{final_args_to_use}'")
            parsed_args = {}
            try:
                if final_args_to_use: 
                    parsed_args = json.loads(final_args_to_use) 
            except json.JSONDecodeError as e:
                self.log(f"Client WARN: Could not decode JSON arguments for {function_to_execute_name}: '{final_args_to_use}'. Error: {e}")
                error_detail_for_llm = f"Invalid JSON arguments for tool {function_to_execute_name}. Error: {str(e)}"
                error_output_for_llm = json.dumps({"error": error_detail_for_llm })
                error_result_payload = {"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": call_id, "output": error_output_for_llm }}
                try:
                    if self.ws_app and self.connected:
                        ws.send(json.dumps(error_result_payload))
                        self.log(f"Client: Sent arg parsing error for Call_ID='{call_id}'.")
                        response_create_payload = {"type": "response.create", "response": {"modalities": ["text", "audio"], "voice": "ash", "output_audio_format": "pcm16"}}
                        ws.send(json.dumps(response_create_payload))
                        self.log("Client: Sent 'response.create' after arg parsing error.")
                except Exception as e_send_err: self.log(f"Client ERROR sending arg parsing error: {e_send_err}")
                return 

            if function_to_execute_name == END_CONVERSATION_TOOL_NAME:
                reason = parsed_args.get("reason", "No reason specified by LLM.")
                self.log(f"Client: Executing '{END_CONVERSATION_TOOL_NAME}' (synchronously) for reason: '{reason}'.")
                if self.wake_word_active: 
                    self.player.clear(); self.player.flush() 
                    self.set_app_state("LISTENING_FOR_WAKEWORD") 
                    time.sleep(0.1) 
                    print(f"\n*** Assistant listening for wake word: '{self.wake_word_detector_instance.wake_word_model_name}' (Reason: {reason}) ***\n")
                else: 
                    print(f"\n*** Conversation turn ended by LLM (Reason: {reason}). Ready for next query. ***\n")
                self.last_assistant_item_id = None # Clear tracking
                self.current_assistant_item_played_ms = 0
                return

            elif function_to_execute_name in TOOL_HANDLERS:
                handler_function = TOOL_HANDLERS[function_to_execute_name]
                self.log(f"Client: Dispatching tool '{function_to_execute_name}' to thread. Call_ID='{call_id}'.")
                tool_thread = threading.Thread(
                    target=self._execute_tool_in_thread,
                    args=(handler_function, parsed_args, call_id, self.config, function_to_execute_name),
                    daemon=True)
                tool_thread.start()
                self.log(f"Client: Thread started for '{function_to_execute_name}'. Main handler continuing.")
                return 
            else: 
                self.log(f"Client WARN: No handler for function '{function_to_execute_name}'. Call_ID='{call_id}'.")
                unhandled_error_out = json.dumps({"error": f"Tool '{function_to_execute_name}' not implemented by client."})
                error_payload = {"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": call_id, "output": unhandled_error_out}}
                try:
                    if self.ws_app and self.connected:
                        ws.send(json.dumps(error_payload))
                        self.log(f"Client: Sent 'unhandled tool' error for Call_ID='{call_id}'.")
                        response_create_payload = {"type": "response.create", "response": {"modalities": ["text", "audio"], "voice": "ash", "output_audio_format":"pcm16"}}
                        ws.send(json.dumps(response_create_payload))
                        self.log("Client: Sent 'response.create' after unhandled tool error.")
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
                self.log(f"Client: Ignoring audio.delta for client-truncated item_id: {item_id_of_delta}")
            elif audio_data_b64:
                audio_data_bytes = base64.b64decode(audio_data_b64)
                self.player.play(audio_data_bytes)
                if self.last_assistant_item_id and self.last_assistant_item_id == item_id_of_delta:
                    self.current_assistant_item_played_ms += self.client_audio_chunk_duration_ms
        
        elif msg_type == "response.audio.done":
            self.log("Client: OpenAI Audio reply 'done' received.")
            self.player.flush() 
            self.log(f"Client: Audio done. Current app state: {self.get_app_state()}.")
            print(f"\n*** Assistant has finished speaking. Ready for your next query. (Ctrl+C to exit) ***\n")

        elif msg_type == "response.output_item.done":
            item_done = msg.get("item", {})
            item_id_done = item_done.get("id")
            if self.last_assistant_item_id and self.last_assistant_item_id == item_id_done:
                self.log(f"Client: Current assistant message item {item_id_done} is now fully done (server ack). Clearing current tracking.")
                self.last_assistant_item_id = None
                self.current_assistant_item_played_ms = 0
            if item_id_done in self.client_initiated_truncated_item_ids:
                self.log(f"Client: Removing {item_id_done} from client_initiated_truncated_item_ids due to response.output_item.done.")
                self.client_initiated_truncated_item_ids.discard(item_id_done)
        
        elif msg_type == "response.done": # Handling for server-side cancellations
            response_details = msg.get("response", {})
            response_status = response_details.get("status")
            if response_status == "cancelled":
                self.log(f"Client: response.done received with status 'cancelled'. Checking its output items for cleanup.")
                output_items = response_details.get("output", [])
                for item_in_cancelled_response in output_items:
                    if isinstance(item_in_cancelled_response, dict):
                        item_id_in_cancelled = item_in_cancelled_response.get("id")
                        if item_id_in_cancelled and item_id_in_cancelled in self.client_initiated_truncated_item_ids:
                            self.log(f"Client: Removing {item_id_in_cancelled} from truncated_set due to cancelled response.done.")
                            self.client_initiated_truncated_item_ids.discard(item_id_in_cancelled)
                        # If this cancelled item was the one we were tracking, clear current tracking too
                        if self.last_assistant_item_id == item_id_in_cancelled:
                            self.log(f"Client: Current assistant item {self.last_assistant_item_id} was part of a server-cancelled response. Clearing tracking.")
                            self.last_assistant_item_id = None
                            self.current_assistant_item_played_ms = 0


        elif msg_type == "input_audio_buffer.speech_started": 
            self.log(f"Client: !!! input_audio_buffer.speech_started RECEIVED (Server VAD) !!! State: {self.get_app_state()}")
            if self.get_app_state() == "SENDING_TO_OPENAI":
                self._perform_truncation(reason_prefix="Server VAD")
        
        elif msg_type == "input_audio_buffer.speech_stopped":
            self.log("Client: OpenAI VAD: User speech stopped detection by server.")
        
        elif msg_type == "error":
            error_message = msg.get('error', {}).get('message', 'Unknown error from OpenAI.')
            self.log(f"Client ERROR from OpenAI: {error_message}")
            if "session" in error_message.lower() or "authorization" in error_message.lower():
                self.log("Client: Critical OpenAI session/auth error. Closing connection.")
                self.connected = False 
                if self.ws_app and hasattr(self.ws_app, 'close'): self.ws_app.close()

    def on_error(self, ws, error): 
        self._log_section("WebSocket ERROR") 
        self.log(f"Client: WebSocket error: {error}")
        self.connected = False
        self.last_assistant_item_id = None
        self.current_assistant_item_played_ms = 0
        self.accumulated_tool_args.clear()
        self.client_initiated_truncated_item_ids.clear()


    def on_close(self, ws, close_status_code, close_msg): 
        self._log_section("WebSocket CLOSE") 
        self.log(f"Client: WebSocket closed: Code={close_status_code}, Reason='{close_msg}'")
        self.connected = False
        self.last_assistant_item_id = None
        self.current_assistant_item_played_ms = 0
        self.accumulated_tool_args.clear()
        self.client_initiated_truncated_item_ids.clear()


    def run_client(self): 
        self.log(f"Client: Attempting WebSocket connection to: {self.ws_url}")
        try:
            import websocket 
        except ImportError:
            self.log("CRITICAL: websocket-client library not found. Please install it: pip install websocket-client")
            return 

        self.ws_app = websocket.WebSocketApp( 
            self.ws_url,
            header=self.headers,
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
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