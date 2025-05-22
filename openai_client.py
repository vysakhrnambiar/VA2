# openai_client.py

import json
import base64
import time 

# Imports from our other new modules
from tools_definition import ALL_TOOLS, END_CONVERSATION_TOOL_NAME 
# Tool name constants for direct reference if needed (e.g. in logging)
from tools_definition import (
    SEND_EMAIL_SUMMARY_TOOL_NAME, 
    RAISE_TICKET_TOOL_NAME,
    GET_BOLT_KB_TOOL_NAME,
    GET_DTC_KB_TOOL_NAME,
    DISPLAY_ON_INTERFACE_TOOL_NAME
)
from tool_executor import TOOL_HANDLERS 
from llm_prompt_config import INSTRUCTIONS as LLM_DEFAULT_INSTRUCTIONS # Import instructions

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
        self._log_section("WebSocket OPEN")
        self.log("Client: Connected to OpenAI Realtime API")
        self.connected = True 
        
        # Use the imported instructions
        llm_instructions = LLM_DEFAULT_INSTRUCTIONS
        # Your specific voice characteristic instructions are now in llm_prompt_config.py

        session_config = {
            "type": "session.update",
            "session": {
                "voice": "ash", # As per your working version
                "turn_detection": {"type": "server_vad"},
                "input_audio_format": "pcm16", 
                "output_audio_format": "pcm16",
                "tools": ALL_TOOLS, 
                "tool_choice": "auto", 
                "instructions": llm_instructions
            }
        }
        ws.send(json.dumps(session_config))
        self.log(f"Client: Session config sent. Instructions imported. Length: {len(llm_instructions)} chars. Voice: 'ash'.")

    def on_message(self, ws, message_str):
        msg = json.loads(message_str)
        msg_type = msg.get("type")

        # Conditional logging as in your working version
        if msg_type not in ["response.audio.delta", "input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped"]:
            if msg_type in ["response.output.delta", 
                            "response.function_call_arguments.delta", "response.function_call_arguments.done", 
                            "conversation.item.created", "response.output_item.done", "response.done", 
                            "error", "session.created"]:
                # For very long messages, avoid indenting to keep log concise
                log_content = json.dumps(msg, indent=2) if len(message_str) < 1000 else str(msg)
                self.log(f"Client RAW_MSG TYPE: {msg_type} | CONTENT: {log_content}")


        if msg_type == "response.output.delta":
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
                                # self.log(f"Client: LLM INTENDS TOOL: Name='{fn_name}', ID='{call_id}', ArgsPart='{fn_args_partial}'.") # Can be noisy
                                self.accumulated_tool_args[call_id] = self.accumulated_tool_args.get(call_id, "") + fn_args_partial
            return 

        elif msg_type == "response.function_call_arguments.delta":
            call_id = msg.get("call_id")
            delta_args = msg.get("delta", "") 
            self.accumulated_tool_args[call_id] = self.accumulated_tool_args.get(call_id, "") + delta_args
            return 

        elif msg_type == "response.function_call_arguments.done":
            call_id = msg.get("call_id")
            function_to_execute_name = msg.get("name") 
            
            final_args_str_from_event = msg.get("arguments", "{}")
            final_accumulated_args = self.accumulated_tool_args.pop(call_id, "{}") 

            final_args_to_use = final_args_str_from_event # Default to event's args
            # Use accumulated if event's is empty/placeholder AND accumulated has content
            if (not final_args_str_from_event or final_args_str_from_event == "{}") and \
               (final_accumulated_args and final_accumulated_args != "{}"):
                self.log(f"Client: Using accumulated args for Call_ID {call_id}.")
                final_args_to_use = final_accumulated_args
            
            if function_to_execute_name:
                self.log(f"Client: Function Call Finalized by LLM: Name='{function_to_execute_name}', Call_ID='{call_id}', Args='{final_args_to_use}'")
                parsed_args = {}
                try:
                    if final_args_to_use: 
                        parsed_args = json.loads(final_args_to_use) 
                except json.JSONDecodeError as e:
                    self.log(f"Client WARN: Could not decode JSON arguments for {function_to_execute_name}: '{final_args_to_use}'. Error: {e}")
                    # Send error back to LLM if args are invalid
                    error_tool_result_str = f"Invalid JSON arguments provided for tool {function_to_execute_name}. Error: {str(e)}"
                    error_result_payload = {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "function_call_output", 
                            "call_id": call_id,
                            # Output for LLM should preferably be a string. If error_tool_result_str is too complex, simplify.
                            "output": json.dumps({"error": error_tool_result_str }) 
                        }
                    }
                    try:
                        ws.send(json.dumps(error_result_payload))
                        self.log(f"Client: Sent arg parsing error as 'conversation.item.create' for Call_ID='{call_id}'.")
                        # Trigger LLM to respond to this error
                        response_create_payload = {"type": "response.create", "response": {"modalities": ["text", "audio"], "voice": "ash", "output_audio_format": "pcm16"}}
                        ws.send(json.dumps(response_create_payload))
                        self.log("Client: Sent 'response.create' to trigger assistant response after arg error.")
                    except Exception as e_send_err:
                        self.log(f"Client ERROR: Could not send arg parsing error back to LLM: {e_send_err}")
                    return # Stop processing this tool call

                if function_to_execute_name == END_CONVERSATION_TOOL_NAME:
                    reason = parsed_args.get("reason", "No reason specified by LLM.")
                    self.log(f"Client: Executing '{END_CONVERSATION_TOOL_NAME}' with reason: '{reason}'. Transitioning state.")
                    if self.wake_word_active: 
                        self.player.clear(); self.player.flush() 
                        self.set_app_state("LISTENING_FOR_WAKEWORD") 
                        time.sleep(0.1) 
                        print(f"\n*** Assistant is now passively listening (Reason: {reason}). Say '{self.wake_word_detector_instance.wake_word_model_name}' to activate. ***\n")
                    else: 
                        print(f"\n*** Conversation turn ended by LLM (Reason: {reason}). Ready for next query (WW inactive). ***\n")
                
                elif function_to_execute_name in TOOL_HANDLERS:
                    handler_function = TOOL_HANDLERS[function_to_execute_name]
                    self.log(f"Client: Calling tool executor for '{function_to_execute_name}'...")
                    try:
                        tool_result_str = handler_function(**parsed_args, config=self.config) 
                        self.log(f"Client: Tool '{function_to_execute_name}' executed locally. Result snippet: '{str(tool_result_str)[:200]}...'")
                        
                        # YOUR WORKING METHOD FOR SENDING TOOL RESULTS:
                        tool_response_payload = {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "function_call_output", 
                                "call_id": call_id,            
                                "output": str(tool_result_str) # Ensure it's a string      
                            }
                        }
                        ws.send(json.dumps(tool_response_payload))
                        self.log(f"Client: Sent tool result as 'conversation.item.create' (item type: function_call_output) for Call_ID='{call_id}'.")
                        
                        # Send response.create to trigger assistant to generate a response
                        response_create_payload = {
                            "type": "response.create",
                            "response": {
                                "modalities": ["text", "audio"],
                                "voice": "ash", # Your preferred voice
                                "output_audio_format": "pcm16"
                            }
                        }
                        ws.send(json.dumps(response_create_payload))
                        self.log("Client: Sent 'response.create' to trigger assistant response after tool success.")

                    except Exception as e_tool_exec:
                        self.log(f"Client ERROR: Exception during execution of tool '{function_to_execute_name}': {e_tool_exec}")
                        error_tool_result_str = f"An error occurred while executing the tool '{function_to_execute_name}': {str(e_tool_exec)}"
                        # YOUR WORKING METHOD FOR SENDING TOOL ERRORS:
                        error_result_payload = {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "function_call_output", 
                                "call_id": call_id,
                                # OpenAI expects the 'output' to be a string representation of the tool's result.
                                # If sending structured error info, ensure it's a string (e.g., JSON string).
                                "output": json.dumps({"error": error_tool_result_str}) 
                            }
                        }
                        try:
                            ws.send(json.dumps(error_result_payload))
                            self.log(f"Client: Sent tool execution error as 'conversation.item.create' (item type: function_call_output) for Call_ID='{call_id}'.")
                            
                            response_create_payload = {
                                "type": "response.create",
                                "response": {
                                    "modalities": ["text", "audio"],
                                    "voice": "ash",
                                    "output_audio_format": "pcm16"
                                }
                            }
                            ws.send(json.dumps(response_create_payload))
                            self.log("Client: Sent 'response.create' to trigger assistant response after tool error.")
                        except Exception as e_send_err:
                            self.log(f"Client ERROR: Could not send tool error back to LLM: {e_send_err}")
                else:
                    self.log(f"Client WARN: No handler defined in TOOL_HANDLERS for function '{function_to_execute_name}'.")
            else: 
                self.log(f"Client WARN: 'function_call_arguments.done' for Call_ID='{call_id}' did not include function name. Args='{final_args_to_use}'.")
        
        elif msg_type == "session.created":
            self.session_id = msg.get('session', {}).get('id')
            self.log(f"Client: OpenAI Session created: {self.session_id}")
            # Guidance message for user:
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
                # This prompt is a general "assistant finished speaking" indicator.
                # If it was a pre-tool announcement, the LLM will soon make the tool call.
                # If it was the final answer, user can speak next.
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
        self._log_section("WebSocket ERROR") 
        self.log(f"Client: WebSocket error: {error}")
        self.connected = False

    def on_close(self, ws, close_status_code, close_msg): 
        self._log_section("WebSocket CLOSE") 
        self.log(f"Client: WebSocket closed: Code={close_status_code}, Reason='{close_msg}'")
        self.connected = False
        self.accumulated_tool_args.clear()

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
        self.ws_app.run_forever()

    def close_connection(self): 
        self.log("Client: close_connection() called.")
        if self.ws_app:
            self.log("Client: Closing WebSocket connection from client side.")
            try:
                self.ws_app.close()
            except Exception as e:
                self.log(f"Client: Error during ws_app.close(): {e}")
        self.connected = False