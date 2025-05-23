# openai_client.py
import json
import base64
import time
import threading # Make sure threading is imported

# Imports from our other new modules
from tools_definition import ALL_TOOLS, END_CONVERSATION_TOOL_NAME 
# Tool name constants for direct reference if needed (e.g. in logging)
from tools_definition import (
    SEND_EMAIL_SUMMARY_TOOL_NAME, 
    RAISE_TICKET_TOOL_NAME,
    GET_BOLT_KB_TOOL_NAME,
    GET_DTC_KB_TOOL_NAME,
    DISPLAY_ON_INTERFACE_TOOL_NAME,
    # Make sure to import your new tool name constant here if you've added it
    # e.g., GET_DAILY_EXECUTIVE_BRIEFING_TOOL_NAME
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
        self.INPUT_RATE = input_rate_hz
        self.OUTPUT_RATE = output_rate_hz
        self.wake_word_active = is_ww_active
        self.wake_word_detector_instance = ww_detector_instance_ref
        self.config = app_config_dict 

        self.ws_app = None
        self.connected = False
        self.session_id = None
        self.accumulated_tool_args = {} 

    def _log_section(self, title): 
        self.log(f"\n===== [Client] {title} =====")

    def on_open(self, ws): 
        # ... (on_open logic remains the same) ...
        self._log_section("WebSocket OPEN")
        self.log("Client: Connected to OpenAI Realtime API")
        self.connected = True 
        
        llm_instructions = LLM_DEFAULT_INSTRUCTIONS
        session_config = {
            "type": "session.update",
            "session": {
                "voice": "ash",
                "turn_detection": {"type": "server_vad","interrupt_response": True},
                "input_audio_format": "pcm16", 
                "output_audio_format": "pcm16",
                "tools": ALL_TOOLS, 
                "tool_choice": "auto", 
                "instructions": llm_instructions
            }
        }
        ws.send(json.dumps(session_config))
        self.log(f"Client: Session config sent. Instructions imported. Length: {len(llm_instructions)} chars. Voice: 'ash'.")


    def _execute_tool_in_thread(self, handler_function, parsed_args, call_id, config, function_name):
        """
        Executes a tool in a separate thread and sends the result (or error)
        and a subsequent response.create message back to the WebSocket.
        """
        self.log(f"Client (Thread - {function_name}): Starting execution for Call_ID {call_id}. Args: {parsed_args}")
        tool_output_for_llm = ""
        try:
            tool_result_str = handler_function(**parsed_args, config=config)
            tool_output_for_llm = str(tool_result_str) # Ensure it's a string
            self.log(f"Client (Thread - {function_name}): Execution complete. Result snippet: '{tool_output_for_llm[:150]}...'")
        
        except Exception as e_tool_exec_thread:
            self.log(f"Client (Thread - {function_name}) ERROR: Exception during execution: {e_tool_exec_thread}")
            # Format a JSON string error message for the LLM
            error_detail = f"An error occurred while executing the tool '{function_name}': {str(e_tool_exec_thread)}"
            tool_output_for_llm = json.dumps({"error": error_detail})
            self.log(f"Client (Thread - {function_name}): Sending error back to LLM: {tool_output_for_llm}")

        # Construct the payload for function_call_output
        tool_response_payload = {
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output", 
                "call_id": call_id,            
                "output": tool_output_for_llm # This is either the success string or JSON error string
            }
        }

        if self.ws_app and self.connected:
            try:
                # Send the tool's output
                self.ws_app.send(json.dumps(tool_response_payload))
                self.log(f"Client (Thread - {function_name}): Sent tool output for Call_ID='{call_id}'.")

                # Now, trigger the LLM to respond to this tool output
                # This response.create is crucial for the LLM to generate its next turn based on the tool's output.
                response_create_payload = {
                    "type": "response.create",
                    "response": {
                        "modalities": ["text", "audio"],
                        "voice": "ash", # Your preferred voice                        
                        "output_audio_format": "pcm16"
                    }
                }
                self.ws_app.send(json.dumps(response_create_payload))
                self.log(f"Client (Thread - {function_name}): Sent 'response.create' to trigger assistant after tool output for Call_ID='{call_id}'.")
            
            except Exception as e_send_thread:
                # Log errors if sending fails (e.g., WebSocket closed abruptly)
                self.log(f"Client (Thread - {function_name}) ERROR: Could not send tool output or response.create for Call_ID='{call_id}': {e_send_thread}")
        else:
            self.log(f"Client (Thread - {function_name}) ERROR: WebSocket not available or not connected. Cannot send tool output for Call_ID='{call_id}'.")


    def on_message(self, ws, message_str):
        msg = json.loads(message_str)
        msg_type = msg.get("type")

        # ... (your existing conditional logging for RAW_MSG) ...
        if msg_type not in ["response.audio.delta", "input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped"]:
            if msg_type in ["response.output.delta", 
                            "response.function_call_arguments.delta", "response.function_call_arguments.done", 
                            "conversation.item.created", "response.output_item.done", "response.done", 
                            "error", "session.created"]:
                log_content = json.dumps(msg, indent=2) if len(message_str) < 1000 else str(msg)
                self.log(f"Client RAW_MSG TYPE: {msg_type} | CONTENT: {log_content}")


        if msg_type == "response.output.delta":
            # ... (your existing response.output.delta handling for tool_calls argument accumulation) ...
            item_type = msg.get("item_type")
            delta_content = msg.get("delta") 
            if item_type == "tool_calls":
                if isinstance(delta_content, dict) and "tool_calls" in delta_content:
                    tc_array = delta_content.get("tool_calls", [])
                    for tc_obj in tc_array:
                        if isinstance(tc_obj, dict):
                            call_id = tc_obj.get("id")
                            fn_name = tc_obj.get('function',{}).get('name')
                            fn_args_partial = tc_obj.get('function',{}).get('arguments',"")
                            if call_id and fn_name:
                                self.accumulated_tool_args[call_id] = self.accumulated_tool_args.get(call_id, "") + fn_args_partial
            return 

        elif msg_type == "response.function_call_arguments.delta":
            # ... (your existing response.function_call_arguments.delta handling) ...
            call_id = msg.get("call_id")
            delta_args = msg.get("delta", "") 
            self.accumulated_tool_args[call_id] = self.accumulated_tool_args.get(call_id, "") + delta_args
            return 

        elif msg_type == "response.function_call_arguments.done":
            call_id = msg.get("call_id")
            function_to_execute_name = msg.get("name") 
            
            final_args_str_from_event = msg.get("arguments", "{}")
            final_accumulated_args = self.accumulated_tool_args.pop(call_id, "{}") 

            final_args_to_use = final_args_str_from_event
            if (not final_args_str_from_event or final_args_str_from_event == "{}") and \
               (final_accumulated_args and final_accumulated_args != "{}"):
                self.log(f"Client: Using accumulated args for Call_ID {call_id}.")
                final_args_to_use = final_accumulated_args
            
            if not function_to_execute_name:
                self.log(f"Client WARN: 'function_call_arguments.done' for Call_ID='{call_id}' did not include function name. Args='{final_args_to_use}'.")
                # Potentially send an error back to LLM if this is critical
                return

            self.log(f"Client: Function Call Finalized by LLM: Name='{function_to_execute_name}', Call_ID='{call_id}', Args='{final_args_to_use}'")
            parsed_args = {}
            try:
                if final_args_to_use: 
                    parsed_args = json.loads(final_args_to_use) 
            except json.JSONDecodeError as e:
                self.log(f"Client WARN: Could not decode JSON arguments for {function_to_execute_name}: '{final_args_to_use}'. Error: {e}")
                # Prepare error message for LLM to be sent by the thread
                error_detail_for_llm = f"Invalid JSON arguments provided for tool {function_to_execute_name}. Error: {str(e)}"
                error_output_for_llm = json.dumps({"error": error_detail_for_llm })
                
                # Send this error back via the threaded mechanism as well for consistency
                # Although, this specific error (arg parsing) happens before tool handler is even called.
                # We can send it directly here, or let the thread mechanism handle a "pre-tool error".
                # For now, let's send it directly and then trigger response.create, then return.
                # This is an error *before* the tool handler itself is invoked.
                error_result_payload = {
                    "type": "conversation.item.create",
                    "item": {"type": "function_call_output", "call_id": call_id, "output": error_output_for_llm }
                }
                try:
                    ws.send(json.dumps(error_result_payload))
                    self.log(f"Client: Sent arg parsing error as 'conversation.item.create' for Call_ID='{call_id}'.")
                    response_create_payload = {"type": "response.create", "response": {"modalities": ["text", "audio"], "voice": "ash", "output_audio_format": "pcm16"}}
                    ws.send(json.dumps(response_create_payload))
                    self.log("Client: Sent 'response.create' to trigger assistant response after arg parsing error.")
                except Exception as e_send_err:
                    self.log(f"Client ERROR: Could not send arg parsing error back to LLM: {e_send_err}")
                return # Stop processing this tool call

            # --- ALL TOOL CALLS ARE NOW THREADED ---
            if function_to_execute_name == END_CONVERSATION_TOOL_NAME:
                # This tool is special: it directly changes client state and might not need a response.create from the thread.
                # It's also very quick. Let's handle it synchronously for simplicity, as it affects client state directly.
                reason = parsed_args.get("reason", "No reason specified by LLM.")
                self.log(f"Client: Executing '{END_CONVERSATION_TOOL_NAME}' (synchronously) with reason: '{reason}'. Transitioning state.")
                if self.wake_word_active: 
                    self.player.clear(); self.player.flush() 
                    self.set_app_state("LISTENING_FOR_WAKEWORD") 
                    time.sleep(0.1) 
                    # No ws.send from here, the state change is the action.
                    # The LLM implicitly knows the conversation ended.
                    print(f"\n*** Assistant is now passively listening (Reason: {reason}). Say '{self.wake_word_detector_instance.wake_word_model_name}' to activate. ***\n")
                else: 
                    print(f"\n*** Conversation turn ended by LLM (Reason: {reason}). Ready for next query (WW inactive). ***\n")
                # No explicit `function_call_output` or `response.create` needed for this tool from the client side
                # as its purpose is to terminate the LLM's active response turn.
                return # Handled.

            elif function_to_execute_name in TOOL_HANDLERS:
                handler_function = TOOL_HANDLERS[function_to_execute_name]
                self.log(f"Client: Dispatching tool '{function_to_execute_name}' to execute in a new thread. Call_ID='{call_id}'.")
                
                tool_execution_thread = threading.Thread(
                    target=self._execute_tool_in_thread,
                    args=(handler_function, parsed_args, call_id, self.config, function_to_execute_name),
                    daemon=True # Daemon threads will exit when the main program exits
                )
                tool_execution_thread.start()
                # The on_message handler returns here. The thread will send the tool output and response.create.
                # The LLM might have already given an interim verbal feedback before this point if instructed.
                self.log(f"Client: Thread started for '{function_to_execute_name}'. Main handler continuing.")
                return # IMPORTANT: Return here to not fall through to unhandled tool logic

            else:
                self.log(f"Client WARN: No handler defined in TOOL_HANDLERS for function '{function_to_execute_name}'. Call_ID='{call_id}'.")
                # Send an error back to LLM indicating tool is not implemented.
                # This error should also go through the threaded mechanism ideally, or be handled consistently.
                # For now, treating as an immediate error:
                unhandled_tool_error = json.dumps({"error": f"Tool '{function_to_execute_name}' is not implemented or recognized by the client."})
                error_payload = {
                    "type": "conversation.item.create",
                    "item": {"type": "function_call_output", "call_id": call_id, "output": unhandled_tool_error}
                }
                try:
                    ws.send(json.dumps(error_payload))
                    self.log(f"Client: Sent 'unhandled tool' error for Call_ID='{call_id}'.")
                    response_create_payload = {"type": "response.create", "response": {"modalities": ["text", "audio"], "voice": "ash"}}
                    ws.send(json.dumps(response_create_payload))
                    self.log("Client: Sent 'response.create' after unhandled tool error.")
                except Exception as e_send_unhandled:
                    self.log(f"Client ERROR: Could not send unhandled tool error: {e_send_unhandled}")
                return
        
        # ... (rest of your on_message handlers for session.created, audio.delta, audio.done, speech_started, speech_stopped, error)
        elif msg_type == "session.created":
            self.session_id = msg.get('session', {}).get('id')
            self.log(f"Client: OpenAI Session created: {self.session_id}")
            if self.get_app_state() == "LISTENING_FOR_WAKEWORD" and self.wake_word_active:
                 print(f"\n*** CLIENT: Listening for wake word: '{self.wake_word_detector_instance.wake_word_model_name}' ***\n")
            else:
                 print(f"\n*** CLIENT: Speak now to interact with OpenAI (WW inactive or sending mode). ***\n")

        elif msg_type == "response.audio.delta":
            audio_data_b64 = msg.get("delta")
            if audio_data_b64:
                audio_data_bytes = base64.b64decode(audio_data_b64)
                self.player.play(audio_data_bytes)
        
        elif msg_type == "response.audio.done":
            self.log("Client: OpenAI Audio reply 'done' received.")
            self.player.flush() 
            current_st_after_audio = self.get_app_state() 
            if current_st_after_audio == "LISTENING_FOR_WAKEWORD": 
                self.log("Client: Audio done, state is LISTENING_FOR_WAKEWORD.")
            elif current_st_after_audio == "SENDING_TO_OPENAI": 
                self.log(f"Client: Audio done. Current state is SENDING_TO_OPENAI.")
                print(f"\n*** Assistant has finished speaking. Ready for your next query. (Ctrl+C to exit) ***\n")
        
        elif msg_type == "input_audio_buffer.speech_started":
            self.log("Client: OpenAI VAD: User speech detected by server.") 
            if self.get_app_state() == "SENDING_TO_OPENAI": 
                self.player.clear() 
        
        elif msg_type == "input_audio_buffer.speech_stopped":
            self.log("Client: OpenAI VAD: User speech stopped detection by server.")
        
        elif msg_type == "error":
            error_message = msg.get('error', {}).get('message', 'Unknown error from OpenAI.')
            self.log(f"Client ERROR from OpenAI. Message: {error_message}")
            if "session" in error_message.lower() or "authorization" in error_message.lower():
                self.log("Client: Critical OpenAI session or auth error. Connection may be unusable.")
                self.connected = False 
                if hasattr(self.ws_app, 'close'): self.ws_app.close()


    def on_error(self, ws, error): 
        # ... (on_error logic remains the same) ...
        self._log_section("WebSocket ERROR") 
        self.log(f"Client: WebSocket error: {error}")
        self.connected = False

    def on_close(self, ws, close_status_code, close_msg): 
        # ... (on_close logic remains the same) ...
        self._log_section("WebSocket CLOSE") 
        self.log(f"Client: WebSocket closed: Code={close_status_code}, Reason='{close_msg}'")
        self.connected = False
        self.accumulated_tool_args.clear()

    def run_client(self): 
        # ... (run_client logic remains the same) ...
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
        self.ws_app.run_forever()

    def close_connection(self): 
        # ... (close_connection logic remains the same) ...
        self.log("Client: close_connection() called.")
        if self.ws_app:
            self.log("Client: Closing WebSocket connection from client side.")
            try:
                self.ws_app.close()
            except Exception as e:
                self.log(f"Client: Error during ws_app.close(): {e}")
        self.connected = False