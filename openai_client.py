# openai_client.py

import json
import base64
import time 

# Imports from our other new modules
from tools_definition import (
    ALL_TOOLS, 
    END_CONVERSATION_TOOL_NAME, 
    SEND_EMAIL_SUMMARY_TOOL_NAME, 
    RAISE_TICKET_TOOL_NAME,
    GET_BOLT_KB_TOOL_NAME,
    GET_DTC_KB_TOOL_NAME
)
from tool_executor import TOOL_HANDLERS # This will map names to functions

class OpenAISpeechClient:
    def __init__(self, ws_url_param, headers_param, main_log_fn, pcm_player, 
                 app_state_setter, app_state_getter, 
                 input_rate_hz, output_rate_hz, 
                 is_ww_active, ww_detector_instance_ref,
                 app_config_dict): # New: application configuration from .env
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
        self.config = app_config_dict # Store the app config (for Resend API keys etc.)

        self.ws_app = None
        self.connected = False
        self.session_id = None
        self.accumulated_tool_args = {} # call_id -> accumulated argument string

    def _log_section(self, title): 
        self.log(f"\n===== [Client] {title} =====")

    def on_open(self, ws): 
        self._log_section("WebSocket OPEN")
        self.log("Client: Connected to OpenAI Realtime API")
        self.connected = True 
        
        # Updated instructions for the LLM
        llm_instructions = (
            "You are a voice assistant for DTC (Dubai Taxi Corporation), Limousine Services, and Bolt (a ride-hailing partner). "
            "Your primary goal is to answer user queries based on information retrieved by calling the functions 'get_dtc_knowledge_base_info' and 'get_bolt_knowledge_base_info'. "
            "Always call these functions with a specific 'query_topic' derived from the user's question to get the necessary data before answering. "
            "If the user asks to compare DTC and Bolt, you may need to call both functions. "
            "If information is not found after checking the knowledge bases, explicitly state that the information is unavailable and then ask the user if they want to raise a ticket to request this information be added, then call 'raise_ticket_for_missing_knowledge' if they agree. "
            "If the user asks to email a summary of the discussion, call 'send_email_discussion_summary'. "
            "When a conversation turn is complete, or the user says goodbye or asks you to stop, you MUST call the function "
            f"'{END_CONVERSATION_TOOL_NAME}' to return to a passive listening state, providing a reason. "
            "Be concise in your responses unless asked for more detail. "
            "When providing information from a knowledge base, synthesize it naturally; do not just read out the raw data unless it's a very short piece of info."
        )

        session_config = {
            "type": "session.update",
            "session": {
                "voice": "alloy", 
                "turn_detection": {"type": "server_vad"},
                "input_audio_format": "pcm16", 
                "output_audio_format": "pcm16",
                "tools": ALL_TOOLS, # Use the imported list of all tool definitions
                "tool_choice": "auto", # Let the model decide when to use tools
                "instructions": llm_instructions
            }
        }
        ws.send(json.dumps(session_config))
        self.log(f"Client: Session config sent (Input: {self.INPUT_RATE}Hz, Player: {self.OUTPUT_RATE}Hz, Tools defined: {len(ALL_TOOLS)}).")

    def on_message(self, ws, message_str):
        msg = json.loads(message_str)
        msg_type = msg.get("type")

        # Log key events for debugging tool call flow
        if msg_type in [
            "response.output.delta", 
            "response.function_call_arguments.delta", 
            "response.function_call_arguments.done", 
            "conversation.item.created", # Useful for seeing item flow
            "response.output_item.done", # Useful for seeing item flow
            "response.done",             # Useful for seeing overall response end
            "error"
        ]:
           self.log(f"Client RAW_MSG TYPE: {msg_type} | CONTENT: {json.dumps(msg, indent=2)}")
        # elif msg_type not in ["response.audio.delta", "input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped"]:
            # self.log(f"Client Other MSG TYPE: {msg_type}")


        if msg_type == "response.output.delta":
            item_type = msg.get("item_type")
            delta_content = msg.get("delta") 

            if item_type == "tool_calls":
                # This message signals the LLM's intent to use a tool.
                # The actual `call_id` and `function.name` are inside `delta_content.tool_calls` array.
                # We don't strictly need to pre-store call_id -> name if .done event provides the name,
                # but it's good for observing the LLM's decision process.
                if isinstance(delta_content, dict) and "tool_calls" in delta_content:
                    tc_array = delta_content.get("tool_calls", [])
                    for tc_obj in tc_array:
                        if isinstance(tc_obj, dict):
                            call_id = tc_obj.get("id")
                            fn_name = tc_obj.get('function',{}).get('name')
                            fn_args_partial = tc_obj.get('function',{}).get('arguments',"")
                            if call_id and fn_name:
                                self.log(f"Client: LLM INTENDS TOOL: Name='{fn_name}', ID='{call_id}', ArgsPart='{fn_args_partial}'.")
                                # Initialize accumulated args for this call_id
                                self.accumulated_tool_args[call_id] = self.accumulated_tool_args.get(call_id, "") + fn_args_partial
            # No explicit return, let other handlers process if needed.

        elif msg_type == "response.function_call_arguments.delta":
            call_id = msg.get("call_id")
            delta_args = msg.get("delta", "") 
            self.accumulated_tool_args[call_id] = self.accumulated_tool_args.get(call_id, "") + delta_args
            # self.log(f"Client Func Args Delta: Call_ID='{call_id}', Accumulating Args: '{self.accumulated_tool_args[call_id]}'")
            return # Wait for .done

        elif msg_type == "response.function_call_arguments.done":
            call_id = msg.get("call_id")
            function_to_execute_name = msg.get("name") # Name is present here!
            
            # Final arguments can be from the event or accumulated. Prioritize accumulated if event's is empty.
            final_args_str_from_event = msg.get("arguments", "{}")
            final_accumulated_args = self.accumulated_tool_args.pop(call_id, "{}") # Get and remove

            final_args_to_use = final_args_str_from_event
            if (not final_args_str_from_event or final_args_str_from_event == "{}") and \
               (final_accumulated_args and final_accumulated_args != "{}"):
                self.log(f"Client: Using accumulated args for Call_ID {call_id}. Event args: '{final_args_str_from_event}', Accumulated: '{final_accumulated_args}'")
                final_args_to_use = final_accumulated_args
            
            if function_to_execute_name:
                self.log(f"Client: Function Call Finalized: Name='{function_to_execute_name}', Call_ID='{call_id}', Args='{final_args_to_use}'")
                
                parsed_args = {}
                try:
                    if final_args_to_use: 
                        parsed_args = json.loads(final_args_to_use) 
                except json.JSONDecodeError as e:
                    self.log(f"Client WARN: Could not decode JSON arguments for {function_to_execute_name}: '{final_args_to_use}'. Error: {e}")
                    # Send error back to LLM? For now, proceed with empty args or handle specific to function.
                    # If args are required, this might be an issue.

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
                        # Pass parsed_args and self.config (which holds .env values like API keys)
                        tool_result_str = handler_function(**parsed_args, config=self.config) 
                        self.log(f"Client: Tool '{function_to_execute_name}' executed. Result: '{tool_result_str[:200]}...'") # Log snippet of result
                        
                        # Send tool result back to OpenAI
                        tool_result_payload = {
                            "type": "tool.result.add",
                            "call_id": call_id,
                            # Result should be a JSON string. Some tools might return simple strings.
                            # For consistency, let's wrap string results in a simple JSON structure.
                            "result": json.dumps({"status": "success", "content": tool_result_str})
                        }
                        ws.send(json.dumps(tool_result_payload))
                        self.log(f"Client: Sent tool result back to OpenAI for Call_ID='{call_id}'.")

                    except Exception as e_tool_exec:
                        self.log(f"Client ERROR: Exception during execution of tool '{function_to_execute_name}': {e_tool_exec}")
                        error_result_payload = {
                            "type": "tool.result.add",
                            "call_id": call_id,
                            "result": json.dumps({"status": "error", "message": f"Error executing tool: {str(e_tool_exec)}"})
                        }
                        ws.send(json.dumps(error_result_payload))
                        self.log(f"Client: Sent tool error result back to OpenAI for Call_ID='{call_id}'.")
                else:
                    self.log(f"Client WARN: No handler defined in TOOL_HANDLERS for function '{function_to_execute_name}'.")
            else: 
                self.log(f"Client WARN: 'function_call_arguments.done' for Call_ID='{call_id}' did not include function name. Args='{final_args_to_use}'.")
        
        elif msg_type == "session.created":
            self.session_id = msg.get('session', {}).get('id')
            self.log(f"Client: OpenAI Session created: {self.session_id}")
        
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
                self.log("Client: Audio done, state is already LISTENING_FOR_WAKEWORD.")
            elif current_st_after_audio == "SENDING_TO_OPENAI": 
                self._log_section("Conversation Turn Ended (Audio Done, No Function Call to Sleep or Tool Result Pending)")
                print(f"\n*** Ready for your next query. Speak now. (Ctrl+C to exit) ***\n")
        
        elif msg_type == "input_audio_buffer.speech_started":
            self.log("Client: OpenAI VAD: User speech started.") 
            if self.get_app_state() == "SENDING_TO_OPENAI": 
                self.player.clear() 
        
        elif msg_type == "input_audio_buffer.speech_stopped":
            self.log("Client: OpenAI VAD: User speech stopped.")
        
        elif msg_type == "error":
            self.log(f"Client ERROR from OpenAI. Message: {msg.get('error', {}).get('message')}")
            if "session" in msg.get('error', {}).get('message', "").lower():
                self.log("Client: Critical OpenAI session error. Connection may be unusable.")
                self.connected = False 

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
        import websocket # Local import for this method
        self.ws_app = websocket.WebSocketApp( 
            self.ws_url,
            header=self.headers,
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
        self.ws_app.run_forever()