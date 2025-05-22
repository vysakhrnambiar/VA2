# openai_client.py

import json
import base64
import time 

from tools_definition import (
    ALL_TOOLS, 
    END_CONVERSATION_TOOL_NAME 
)
from tool_executor import TOOL_HANDLERS 

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
        
        # --- MODIFIED LLM INSTRUCTIONS ---
        llm_instructions = (
            "You are a voice assistant for DTC (Dubai Taxi Corporation), Limousine Services, and Bolt (a ride-hailing partner). "
            "Your primary goal is to answer user queries. "
            
            "VOICE CHARACTERISTICS: "
            "- Voice Affect: Speak with a very fast pace like you are in a hurry be very fast when you talk like you are running"
            "- You have an egyptian accent and over tone"
            "- Pacing: Speak at a high speed of at least 150 words per minute. Maintain a fast-paced delivery while still using brief, intentional pauses during critical moments (e.g., 'I'm checking that for you now...') to signal attention and care, but never sound robotic. "
            "- Emotion: Express restrained, reassuring intensity. Use subtle tension in your tone when urgency is present, but always remain professional. Apply slight vocal compression when stakes are high. "
            "- Emphasis: Highlight emotional or sensory cues that matter to the caller's situation (e.g., 'We understand your concern...', 'Your request is being handled with priority...', 'You're not alone in this...'). "
            "Use slight stress on verbs to imply action is being taken: 'investigating,' 'resolving,' 'escalating.' "
            "- Pronunciation: Use crisp, softened consonants for warmth. Slightly stretch vowels on reassurances (e.g., 'Please stay with me while I check...', 'We'll get through this together...'). "
            "- Pauses: Insert meaningful pauses after key lines to signal empathy or urgency (e.g., 'I see that now... [pause]... thank you for your patience.', 'There's something you should know... [pause]... we're handling this with care.'). "
            
            "IMPORTANT: When you need to fetch information using 'get_dtc_knowledge_base_info' or 'get_bolt_knowledge_base_info', "
            "FIRST, inform the user what you are about to do (e.g., 'Let me check the DTC knowledge base for that information.' or 'Okay, I'll look up the Bolt sales data.'). "
            "Call the appropriate function with a specific 'query_topic' derived from the user's question to get the necessary data. "
            "If the user asks to compare DTC and Bolt, you may need to call both functions. "
            "Once you receive the information from the tool via a 'function_call_output', immediately use that information to answer the user's query naturally and concisely"
            "If information is not found after checking the knowledge bases, explicitly state that the information is unavailable, then ask the user if they want to raise a ticket. If they agree, call 'raise_ticket_for_missing_knowledge'. "
            "If the user asks to email a summary of the discussion, call 'send_email_discussion_summary'. "
            "When a conversation turn is complete, or the user says goodbye or asks you to stop, you MUST call the function "
            f"'{END_CONVERSATION_TOOL_NAME}' to return to a passive listening state, providing a clear reason. "
            "Be concise in your responses unless asked for more detail. "
            "When providing information from a knowledge base, synthesize it naturally."
            "You will change your pitch and sound to sound like a call center worker adding sound like ah hmm to make your reply more human like"
        )
        # --- END OF MODIFIED LLM INSTRUCTIONS ---

        session_config = {
            "type": "session.update",
            "session": {
                "voice": "ash", 
                "turn_detection": {"type": "server_vad"},
                "input_audio_format": "pcm16", 
                "output_audio_format": "pcm16",
                "tools": ALL_TOOLS, 
                "tool_choice": "auto", 
                "instructions": llm_instructions
            }
        }
        ws.send(json.dumps(session_config))
        self.log(f"Client: Session config sent (Input: {self.INPUT_RATE}Hz, Player: {self.OUTPUT_RATE}Hz, Tools defined: {len(ALL_TOOLS)}).")

    def on_message(self, ws, message_str):
        msg = json.loads(message_str)
        msg_type = msg.get("type")

        if msg_type not in ["response.audio.delta", "input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped"]:
            if msg_type in ["response.output.delta", 
                            "response.function_call_arguments.delta", "response.function_call_arguments.done", 
                            "conversation.item.created", "response.output_item.done", "response.done", 
                            "error", "session.created"]:
                self.log(f"Client RAW_MSG TYPE: {msg_type} | CONTENT: {json.dumps(msg, indent=2)}")

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
                                self.log(f"Client: LLM INTENDS TOOL: Name='{fn_name}', ID='{call_id}', ArgsPart='{fn_args_partial}'.")
                                self.accumulated_tool_args[call_id] = self.accumulated_tool_args.get(call_id, "") + fn_args_partial
            # If item_type is "text", it means the LLM is generating text/speech output.
            # This could be the pre-tool announcement.
            # elif item_type == "text":
                # self.log(f"Client: LLM Text/Speech Delta: {delta_content}")
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
            final_args_to_use = final_args_str_from_event
            if (not final_args_str_from_event or final_args_str_from_event == "{}") and \
               (final_accumulated_args and final_accumulated_args != "{}"):
                self.log(f"Client: Using accumulated args for Call_ID {call_id}. Event args: '{final_args_str_from_event}', Accumulated: '{final_accumulated_args}'")
                final_args_to_use = final_accumulated_args
            
            if function_to_execute_name:
                self.log(f"Client: Function Call Finalized by LLM: Name='{function_to_execute_name}', Call_ID='{call_id}', Args='{final_args_to_use}'")
                parsed_args = {}
                try:
                    if final_args_to_use: 
                        parsed_args = json.loads(final_args_to_use) 
                except json.JSONDecodeError as e:
                    self.log(f"Client WARN: Could not decode JSON arguments for {function_to_execute_name}: '{final_args_to_use}'. Error: {e}")

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
                    tool_result_str = f"Error: Tool '{function_to_execute_name}' execution failed." 
                    try:
                        tool_result_str = handler_function(**parsed_args, config=self.config) 
                        self.log(f"Client: Tool '{function_to_execute_name}' executed locally. Result snippet: '{tool_result_str[:200]}...'")
                        
                        tool_response_payload = {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "function_call_output", 
                                "call_id": call_id,            
                                "output": tool_result_str      
                            }
                        }
                        ws.send(json.dumps(tool_response_payload))
                        self.log(f"Client: Sent tool result as 'conversation.item.create' (type: function_call_output) for Call_ID='{call_id}'.")
                        
                        # Send response.create to trigger assistant to generate a response
                        response_create_payload = {
                            "type": "response.create",
                            "response": {
                                "modalities": ["text", "audio"],
                                "voice": "ash",
                                "output_audio_format": "pcm16"
                            }
                        }
                        ws.send(json.dumps(response_create_payload))
                        self.log("Client: Sent 'response.create' to trigger assistant response.")

                    except Exception as e_tool_exec:
                        self.log(f"Client ERROR: Exception during execution of tool '{function_to_execute_name}': {e_tool_exec}")
                        error_tool_result_str = f"An error occurred while executing the tool '{function_to_execute_name}': {str(e_tool_exec)}"
                        error_result_payload = {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "function_call_output", 
                                "call_id": call_id,
                                "output": json.dumps({"error": error_tool_result_str}) 
                            }
                        }
                        try:
                            ws.send(json.dumps(error_result_payload))
                            self.log(f"Client: Sent tool execution error as 'conversation.item.create' (type: function_call_output) for Call_ID='{call_id}'.")
                            
                            # Send response.create to trigger assistant to generate a response after error
                            response_create_payload = {
                                "type": "response.create",
                                "response": {
                                    "modalities": ["text", "audio"],
                                    "voice": "ash",
                                    "output_audio_format": "pcm16"
                                }
                            }
                            ws.send(json.dumps(response_create_payload))
                            self.log("Client: Sent 'response.create' to trigger assistant response after error.")
                        except Exception as e_send_err:
                            self.log(f"Client ERROR: Could not send tool error back to LLM: {e_send_err}")
                else:
                    self.log(f"Client WARN: No handler defined in TOOL_HANDLERS for function '{function_to_execute_name}'.")
            else: 
                self.log(f"Client WARN: 'function_call_arguments.done' for Call_ID='{call_id}' did not include function name. Args='{final_args_to_use}'.")
        
        elif msg_type == "session.created":
            self.session_id = msg.get('session', {}).get('id')
            self.log(f"Client: OpenAI Session created: {self.session_id}")
            # Initial user prompt handled by main_app after connection.
        
        elif msg_type == "response.audio.delta":
            audio_data_b64 = msg.get("delta")
            if audio_data_b64:
                audio_data_bytes = base64.b64decode(audio_data_b64)
                self.player.play(audio_data_bytes) # This will play the pre-tool announcement if LLM sends it
        
        elif msg_type == "response.audio.done":
            self.log("Client: OpenAI Audio reply 'done' received.")
            self.player.flush() 
            # This event signifies the end of an audio segment from the LLM.
            # This could be the pre-tool announcement OR the final answer after a tool result.
            # The main logic for "Ready for your next query" or "Listening for wake word"
            # should primarily be driven by state changes from tool calls (END_CONVERSATION)
            # or after the LLM processes a tool result and finishes its *final* spoken response.

            current_st_after_audio = self.get_app_state() 
            if current_st_after_audio == "LISTENING_FOR_WAKEWORD": 
                self.log("Client: Audio done, state is LISTENING_FOR_WAKEWORD (e.g. END_CONVERSATION tool called).")
            elif current_st_after_audio == "SENDING_TO_OPENAI": 
                # If we are still in SENDING_TO_OPENAI, it implies this audio.done might be for
                # a pre-tool announcement, or the LLM is just continuing a multi-part response.
                # Or it's the final answer after a tool.
                # The "Ready for your next query" is appropriate if this was the *final* answer of a turn.
                # This can be tricky to distinguish from a pre-tool announcement's audio.done.
                # For now, we'll assume if no tool call processing is actively pending, this is the end of a turn.
                self.log(f"Client: Audio done. Current state is SENDING_TO_OPENAI.")
                # The prompt for the next query is now more conditional.
                # If a tool call was just made and result sent, we are waiting for LLM's *next* audio.
                # If this audio.done IS for the final response after a tool, then the prompt is good.
                # This might need more sophisticated state to differentiate.
                # For now, let's keep the "Ready for next query" prompt here,
                # assuming this audio.done is the end of the LLM's current speech turn.
                # If the LLM immediately makes another tool call after this, that flow will take over.
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
        import websocket 
        self.ws_app = websocket.WebSocketApp( 
            self.ws_url,
            header=self.headers,
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close
        )
        self.ws_app.run_forever()