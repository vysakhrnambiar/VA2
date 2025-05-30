# openai_client.py
import json
import base64
import time
import threading
import numpy as np
from pytsmod import wsola
import websocket
import openai # For synchronous LLM call in on_open
from datetime import datetime as dt, timezone # Alias for datetime, import timezone
import os # For path joining
import requests # For Phase 4 frontend notifications

# Imports from our other new modules
from tools_definition import ALL_TOOLS, END_CONVERSATION_TOOL_NAME
# tool_definition imports (assuming all necessary names are included in ALL_TOOLS)
from tool_executor import TOOL_HANDLERS # Assuming this is kept up-to-date
from llm_prompt_config import INSTRUCTIONS as LLM_DEFAULT_INSTRUCTIONS

# --- Phase 2 & 3 Imports ---
from conversation_history_db import add_turn as log_conversation_turn
from conversation_history_db import get_recent_turns
import sqlite3

# --- Constants for Phase 3 ---
CONTEXT_HISTORY_LIMIT = 30  # Increased for better context retention
BASE_DIR_CLIENT = os.path.dirname(os.path.abspath(__file__))
SCHEDULED_CALLS_DB_PATH = os.path.join(BASE_DIR_CLIENT, "scheduled_calls.db")
CONTEXT_SUMMARIZER_MODEL = os.getenv("CONTEXT_SUMMARIZER_MODEL", "gpt-4o-mini") # Use env var or fallback


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
        self.current_assistant_text_response = ""

        self.last_assistant_item_id = None
        self.current_assistant_item_played_ms = 0
        self.client_audio_chunk_duration_ms = self.config.get("CHUNK_MS", 30)
        self.client_initiated_truncated_item_ids = set()
        
        # Audio logging counters
        self.audio_received_counter = 0
        self.audio_sent_counter = 0

        self.use_ulaw_for_openai = self.config.get("USE_ULAW_FOR_OPENAI_INPUT", False)
        self.desired_playback_speed = float(self.config.get("TSM_PLAYBACK_SPEED", 1.0))
        self.tsm_enabled = self.desired_playback_speed != 1.0
        self.openai_sample_rate = 24000
        self.tsm_channels = 1
        if self.tsm_enabled: self.log(f"TSM enabled. Speed: {self.desired_playback_speed}")
        self.NUM_CHUNKS_FOR_TSM_WINDOW = int(self.config.get("TSM_WINDOW_CHUNKS", 8))
        self.BYTES_PER_OPENAI_CHUNK = (self.openai_sample_rate * self.client_audio_chunk_duration_ms // 1000) * (16 // 8) * self.tsm_channels
        self.TSM_PROCESSING_THRESHOLD_BYTES = self.BYTES_PER_OPENAI_CHUNK * self.NUM_CHUNKS_FOR_TSM_WINDOW
        self.openai_audio_buffer_raw_bytes = b''

        self.keep_outer_loop_running = True
        self.RECONNECT_DELAY_SECONDS = self.config.get("OPENAI_RECONNECT_DELAY_S", 5)
        
        # Ensure OPENAI_API_KEY is available for the sync client
        openai_api_key_for_sync = self.config.get("OPENAI_API_KEY")
        if not openai_api_key_for_sync:
            self.log("CRITICAL_ERROR: OPENAI_API_KEY not found in config for sync_openai_client. Context summarizer will fail.")
            self.sync_openai_client = None
        else:
            try:
                self.sync_openai_client = openai.OpenAI(api_key=openai_api_key_for_sync)
                self.log("Synchronous OpenAI client for context summarizer initialized.")
            except Exception as e_sync_client:
                self.log(f"CRITICAL_ERROR: Failed to initialize synchronous OpenAI client: {e_sync_client}. Context summarizer will fail.")
                self.sync_openai_client = None
            # --- Phase 4: UI Notification URL ---
        # Ensure this key exists in your .env or APP_CONFIG in main.py
        self.ui_status_update_url = self.config.get("FASTAPI_UI_STATUS_UPDATE_URL") 
        if not self.ui_status_update_url:
            self.log("WARN: FASTAPI_UI_STATUS_UPDATE_URL not configured in .env. Frontend status notifications will be disabled.")

        


    def _log_section(self, title):
        self.log(f"\n===== [Client] {title} =====")



    def _clear_audio_state(self):
        """Clear all audio-related state and buffers."""
        if self.player:
            self.player.clear()
            self.player.flush()
        self.openai_audio_buffer_raw_bytes = b''
        self.last_assistant_item_id = None
        self.current_assistant_item_played_ms = 0
        self.audio_received_counter = 0

    def _process_and_play_audio(self, audio_data_bytes: bytes):
        """
        Buffers incoming audio, applies TSM with pytsmod.wsola if enabled, and sends to player.
        """
        # Don't process audio if we're transitioning states
        if self.get_app_state() == "LISTENING_FOR_WAKEWORD":
            return

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


    # --- Phase 4: Frontend Notification Methods and TTS Announcement ---
    def _notify_frontend(self, payload: dict):
        if not self.ui_status_update_url:
            # Already logged in __init__ if not configured, so keep this brief or remove
            # self.log("WARN: ui_status_update_url not configured. Cannot notify frontend.")
            return
        try:
            # Adding a small timeout to prevent blocking indefinitely
            response = requests.post(self.ui_status_update_url, json=payload, timeout=2)
            if response.status_code == 200:
                self.log(f"Successfully notified frontend: Type '{payload.get('type')}', Status '{payload.get('status', {}).get('connection')}'")
            else:
                self.log(f"WARN: Failed to notify frontend. Status: {response.status_code}, Response: {response.text[:100]}")
        except requests.exceptions.RequestException as e:
            self.log(f"WARN: Error notifying frontend: {e}")
        except Exception as e_notify: # Catch any other unexpected error
            self.log(f"WARN: Unexpected error in _notify_frontend: {e_notify}")
            
    def generate_update_announcement(self, contact_name):
        """
        Generate a brief TTS announcement about an update without providing details.
        Uses the same OpenAI voice as configured for real-time conversations.
        
        Args:
            contact_name: The name of the contact associated with the update
            
        Returns:
            bytes: PCM audio bytes of the announcement
        """
        if not self.sync_openai_client:
            self.log("WARN: Synchronous OpenAI client not available for TTS announcement")
            return None
            
        # Create a concise announcement without details
        announcement_text = f"I have an update on your call with {contact_name}. Wake me up and I can give you the details."
        
        try:
            # Use the same voice configured for the conversation
            voice = self.config.get("OPENAI_VOICE", "ash")
            
            # Generate TTS using OpenAI's API
            response = self.sync_openai_client.audio.speech.create(
                model="tts-1",  # Or "tts-1-hd" for higher quality
                voice=voice,
                input=announcement_text,
                response_format="pcm"  # Get PCM format directly
            )
            
            # Get the audio content as bytes
            announcement_audio = response.content
            
            self.log(f"Generated TTS announcement for contact: {contact_name}")
            return announcement_audio
            
        except Exception as e:
            self.log(f"ERROR generating TTS announcement: {e}")
            return None
            if response.status_code == 200:
                self.log(f"Successfully notified frontend: Type '{payload.get('type')}', Status '{payload.get('status', {}).get('connection')}'")
            else:
                self.log(f"WARN: Failed to notify frontend. Status: {response.status_code}, Response: {response.text[:100]}")
        except requests.exceptions.RequestException as e:
            self.log(f"WARN: Error notifying frontend: {e}")
        except Exception as e_notify: # Catch any other unexpected error
            self.log(f"WARN: Unexpected error in _notify_frontend: {e_notify}")

    def _notify_frontend_connect(self):
        self.log("Client: Notifying frontend of connection.")
        payload = {
            "type": "connection_status", # Message type for frontend JS to recognize
            "status": { # Nested status for clarity
                "connection": "connected",
                "message": "Agent connected to OpenAI."
            }
        }
        self._notify_frontend(payload)

    def _notify_frontend_disconnect(self, reason="Attempting to reconnect..."):
        self.log(f"Client: Notifying frontend of disconnection. Reason: {reason}")
        payload = {
            "type": "connection_status",
            "status": {
                "connection": "disconnected",
                "message": f"Agent lost connection. {reason}"
            }
        }
        self._notify_frontend(payload)
    # --- End of Phase 4 Frontend Notification Methods --- 

    def _get_pending_call_updates_text(self) -> tuple[str, list[int]]:
        updates_text = ""
        processed_job_ids = []
        conn = None
        try:
            conn = sqlite3.connect(SCHEDULED_CALLS_DB_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, contact_name, overall_status, final_summary_for_main_agent 
                FROM scheduled_calls 
                WHERE main_agent_informed_user = 0 
                  AND overall_status IN ('COMPLETED_SUCCESS', 'FAILED_MAX_RETRIES', 'COMPLETED_OBJECTIVE_NOT_MET', 'FAILED_PERMANENT_ERROR')
                ORDER BY updated_at DESC
                LIMIT 5 
            """)
            pending_updates = cursor.fetchall()
            if pending_updates:
                updates_list = []
                for job in pending_updates:
                    job_id = job['id']
                    summary = job['final_summary_for_main_agent'] if job['final_summary_for_main_agent'] else f"finished with status {job['overall_status']}."
                    updates_list.append(f"Call to {job['contact_name']} (Job ID: {job_id}): {summary}")
                    processed_job_ids.append(job_id)
                updates_text = "Pending Call Task Updates:\n- " + "\n- ".join(updates_list) + "\n"
                self.log(f"Fetched {len(pending_updates)} pending call updates for context priming.")
        except sqlite3.Error as e:
            self.log(f"ERROR fetching pending call updates from '{SCHEDULED_CALLS_DB_PATH}': {e}")
        finally:
            if conn: conn.close()
        return updates_text, processed_job_ids

    def _mark_call_updates_as_informed(self, job_ids: list[int]):
        if not job_ids: return
        conn = None
        try:
            conn = sqlite3.connect(SCHEDULED_CALLS_DB_PATH)
            cursor = conn.cursor()
            placeholders = ','.join('?' for _ in job_ids)
            sql = f"UPDATE scheduled_calls SET main_agent_informed_user = 1, updated_at = CURRENT_TIMESTAMP WHERE id IN ({placeholders})"
            cursor.execute(sql, tuple(job_ids))
            conn.commit()
            self.log(f"Marked {len(job_ids)} call jobs as informed: {job_ids}")
        except sqlite3.Error as e:
            self.log(f"ERROR marking call updates as informed in '{SCHEDULED_CALLS_DB_PATH}': {e}")
        finally:
            if conn: conn.close()

    def _get_conversation_summary(self, session_id_for_history: str) -> str:
        if not self.sync_openai_client:
            self.log("WARN: Synchronous OpenAI client not available for conversation summarization.")
            return "Previous conversation context is unavailable at the moment.\n"
        if not session_id_for_history:
            self.log("No session ID available for fetching conversation history.")
            return ""
        recent_turns = get_recent_turns(session_id=session_id_for_history, limit=CONTEXT_HISTORY_LIMIT)
        if not recent_turns:
            self.log("No recent conversation turns found to summarize.")
            return ""

        formatted_history = []
        now_utc_aware = dt.now(timezone.utc) # Use timezone.utc for awareness
        for turn in recent_turns:
            try:
                # Attempt to parse timestamp, assuming it's UTC if naive
                ts_str = turn['timestamp']
                if isinstance(ts_str, dt): # Already a datetime object
                    turn_time = ts_str
                else: # String parsing
                    turn_time = dt.fromisoformat(ts_str.replace('Z', '+00:00'))
                
                # Ensure turn_time is offset-aware (assume UTC if naive)
                if turn_time.tzinfo is None:
                    turn_time = turn_time.replace(tzinfo=timezone.utc)

                time_diff_seconds = (now_utc_aware - turn_time).total_seconds()

                if time_diff_seconds < 0: time_diff_seconds = 0 # Guard against clock skew issues
                if time_diff_seconds < 60: time_ago = f"{int(time_diff_seconds)}s ago"
                elif time_diff_seconds < 3600: time_ago = f"{int(time_diff_seconds/60)}m ago"
                else: time_ago = f"{int(time_diff_seconds/3600)}h ago"
                
                role_display = turn['role'].capitalize()
                content_display = turn['content']
                if turn['role'] in ['tool_call', 'tool_result']:
                    try: 
                        content_json = json.loads(turn['content'])
                        content_display = f"Tool: {content_json.get('name', 'N/A')}, Data: {str(content_json)[:70]}..."
                    except: pass
                formatted_history.append(f"({time_ago}) {role_display}: {content_display}")
            except Exception as e_ts_format:
                self.log(f"WARN: Could not format timestamp for history: {turn.get('timestamp')}. Error: {e_ts_format}")
                formatted_history.append(f"(Time Unknown) {turn['role'].capitalize()}: {turn['content'][:70]}...")

        history_string_for_llm = "\n".join(formatted_history)
        self.log(f"Formatted history for summarizer (last {len(formatted_history)} turns): \n{history_string_for_llm[:300]}...")

        # Analyze history for connection events
        connection_events = []
        for turn in recent_turns:
            if turn['role'] == 'system_event':
                try:
                    event_data = json.loads(turn['content'])
                    if event_data.get('event') in ['websocket_error', 'websocket_closed']:
                        connection_events.append(turn)
                except:
                    pass

        connection_context = ""
        if connection_events:
            connection_context = "\nNote: There were some connection interruptions in the previous conversation."

        prompt_for_summarizer = f"""Current UTC time is {dt.now(timezone.utc).isoformat()}.
        Analyze and summarize the following conversation history, focusing on:
        1. Key unresolved questions or tasks
        2. Important context from previous interactions
        3. The last state of any ongoing tasks or discussions
        4. Any tool calls or actions that were in progress
        
        If the conversation ended cleanly or there's no significant context to maintain, output "No specific unresolved context to resume."

        History:
        {history_string_for_llm}
        {connection_context}

        Provide a concise, natural summary that captures the essential context needed to continue the conversation effectively.

        Briefing:
        """
        try:
            self.log(f"Sending to summarizer LLM ({CONTEXT_SUMMARIZER_MODEL})...")
            response = self.sync_openai_client.chat.completions.create(
                model=CONTEXT_SUMMARIZER_MODEL,
                messages=[{"role": "user", "content": prompt_for_summarizer}],
                temperature=0.1, max_tokens=200 )
            summary = response.choices[0].message.content.strip()
            if "no specific unresolved context" in summary.lower():
                self.log("Summarizer: No specific context to resume from history.")
                return ""
            self.log(f"Summarizer LLM response: {summary}")
            return f"Recent conversation summary: {summary}\n"
        except Exception as e:
            self.log(f"ERROR summarizing conversation history with LLM: {e}")
            return "Context summary unavailable due to an error.\n"



    def on_open(self, ws):
        self._log_section("WebSocket OPEN")
        self.log("Client: Connected to OpenAI Realtime API.")
        self.connected = True
        self.current_assistant_text_response = ""

        # --- Phase 4: self.notify_frontend_connect() would be called here ---
            # --- Phase 4: Notify frontend of connection ---
        self._notify_frontend_connect()
        primed_context_parts = []
        # 1. Get conversation summary (uses self.session_id from *previous* connection)
        if self.session_id:
            conv_summary = self._get_conversation_summary(self.session_id)
            if conv_summary: primed_context_parts.append(conv_summary)
        else:
            self.log("No prior session_id for conversation history retrieval on this connection.")

        # 2. Get pending call updates
        call_updates_text, informed_job_ids = self._get_pending_call_updates_text()
        if call_updates_text:
            primed_context_parts.append(call_updates_text)
            
        effective_instructions = LLM_DEFAULT_INSTRUCTIONS
        if primed_context_parts:
            full_primed_context = "\n".join(primed_context_parts)
            self.log(f"Priming LLM with context:\n{full_primed_context}")
            effective_instructions = full_primed_context + "\n---\n" + LLM_DEFAULT_INSTRUCTIONS
        else:
            self.log("No additional context (history summary or call updates) to prime LLM with.")

        input_format_to_use = "g711_ulaw" if self.use_ulaw_for_openai else "pcm16"
        session_config = {
            "type": "session.update",
            "session": {
                "voice": self.config.get("OPENAI_VOICE", "ash"),
                "turn_detection": {"type": "server_vad", "interrupt_response": True},
                "input_audio_format": input_format_to_use, "output_audio_format": "pcm16",
                "tools": ALL_TOOLS, "tool_choice": "auto",
                "instructions": effective_instructions,
                "input_audio_transcription": {"model": "whisper-1"}
            }
        }
        try:
            self.ws_app.send(json.dumps(session_config))
            self.log(f"Client: Session config sent. Instructions length: {len(effective_instructions)} chars.")
            if informed_job_ids:
                self._mark_call_updates_as_informed(informed_job_ids)
        except Exception as e_send_session:
            self.log(f"ERROR sending session.update or marking updates: {e_send_session}")
            # If this fails, the connection might be unstable already. Reconnect loop will handle.


    def _execute_tool_in_thread(self, handler_function, parsed_args, call_id, config, function_name):
        self.log(f"Client (Thread - {function_name}): Starting execution for Call_ID {call_id}. Args: {parsed_args}")
        tool_output_for_llm = ""
        try:
            tool_result_str = handler_function(**parsed_args, config=config)
            tool_output_for_llm = str(tool_result_str)
            self.log(f"Client (Thread - {function_name}): Execution complete. Result snippet: '{tool_output_for_llm[:150]}...'")
            # Log tool result to conversation history
            if self.session_id:
                try:
                    log_conversation_turn(
                        self.session_id,
                        "tool_result",
                        json.dumps({
                            "name": function_name,
                            "result": tool_output_for_llm
                        })
                    )
                except Exception as e:
                    self.log(f"ERROR: Failed to log tool result to conversation history: {e}", logging.ERROR)
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

    def is_assistant_speaking(self) -> bool: return self.last_assistant_item_id is not None
    def get_current_assistant_speech_duration_ms(self) -> int:
        if self.last_assistant_item_id: return self.current_assistant_item_played_ms
        return 0
    def _perform_truncation(self, reason_prefix: str):
        item_id_to_truncate = self.last_assistant_item_id
        if not item_id_to_truncate: return
        self.player.clear(); self.openai_audio_buffer_raw_bytes = b''
        timestamp_to_send_ms = max(10, self.current_assistant_item_played_ms)
        truncate_payload = {"type": "conversation.item.truncate", "item_id": item_id_to_truncate, "content_index": 0, "audio_end_ms": timestamp_to_send_ms}
        try:
            if self.ws_app and self.connected:
                self.ws_app.send(json.dumps(truncate_payload))
                self.client_initiated_truncated_item_ids.add(item_id_to_truncate)
        except Exception as e_send_trunc: self.log(f"Client ERROR sending truncate: {e_send_trunc}")
        self.last_assistant_item_id = None; self.current_assistant_item_played_ms = 0
    def _wait_for_audio_completion(self, timeout_s=5.0):
        """Wait for any current audio to finish playing."""
        start_time = time.time()
        while (time.time() - start_time) < timeout_s:
            # Check if there's any audio still playing
            if not self.last_assistant_item_id and len(self.player.buffer) == 0:
                return True  # Audio finished
            time.sleep(0.1)  # Small sleep to prevent CPU spin
        return False  # Timeout reached

    def handle_local_user_speech_interrupt(self):
        if self.get_app_state() == "SENDING_TO_OPENAI": self._perform_truncation(reason_prefix="Local VAD")

 
    def _format_message(self, msg, msg_type):
        """Format OpenAI messages into human-readable logs."""
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
        
        # Session events
        if msg_type == "session.created":
            session_id = msg.get('session', {}).get('id')
            expires_at = msg.get('session', {}).get('expires_at', 0)
            try:
                expires_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(expires_at))
            except:
                expires_str = str(expires_at)
            return f"📡 SESSION: Created new session {session_id} (expires: {expires_str})"
            
        elif msg_type == "session.updated":
            return f"📡 SESSION: Updated session {msg.get('session', {}).get('id')}"
            
        # Conversation items
        elif msg_type == "conversation.item.created":
            item = msg.get("item", {})
            item_type = item.get("type")
            role = item.get("role")
            item_id = item.get("id")
            
            if role == "user":
                return f"👤 USER: New message started (ID: {item_id})"
            elif role == "assistant" and item_type == "message":
                return f"🤖 ASSISTANT: New message started (ID: {item_id})"
            elif item_type == "function_call":
                name = item.get("name", "unknown")
                return f"🔧 FUNCTION: Starting '{name}' (ID: {item_id})"
            
        # Transcription events
        elif msg_type == "conversation.item.input_audio_transcription.completed":
            transcript = msg.get("transcript", "")
            # Log completed user transcription
            if self.session_id and transcript:
                try:
                    log_conversation_turn(
                        self.session_id,
                        "user",
                        transcript
                    )
                except Exception as e:
                    self.log(f"ERROR: Failed to log user transcript to conversation history: {e}", logging.ERROR)
            return f"------ CONVERSATION ------\n👤 USER SAID: \"{transcript}\"\n---------------------------"
            
        elif msg_type == "response.audio_transcript.done":
            transcript = msg.get("transcript", "")
            # Log completed assistant response
            if self.session_id and transcript:
                try:
                    log_conversation_turn(
                        self.session_id,
                        "assistant",
                        transcript
                    )
                except Exception as e:
                    self.log(f"ERROR: Failed to log assistant response to conversation history: {e}", logging.ERROR)
            return f"------ CONVERSATION ------\n🤖 ASSISTANT SAID: \"{transcript}\"\n---------------------------"
            
        elif msg_type == "response.audio_transcript.done":
            transcript = msg.get("transcript", "")
            return f"------ CONVERSATION ------\n🤖 ASSISTANT SAID: \"{transcript}\"\n---------------------------"
            
        # Function calling
        elif msg_type == "response.function_call_arguments.done":
            name = msg.get("name")
            call_id = msg.get("call_id")
            args = msg.get("arguments", "{}")
            args_preview = args[:100] + ("..." if len(args) > 100 else "")
            return f"🔧 FUNCTION: Executing '{name}' (ID: {call_id})\n    Args: {args_preview}"
            
        # Truncation and completion events
        elif msg_type == "conversation.item.truncated":
            item_id = msg.get("item_id")
            audio_end_ms = msg.get("audio_end_ms")
            return f"✂️ TRUNCATED: Item {item_id} at {audio_end_ms}ms"
            
        elif msg_type == "response.output_item.done":
            item = msg.get("item", {})
            item_id = item.get("id")
            item_type = item.get("type")
            status = item.get("status")
            return f"✅ COMPLETED: {item_type} (ID: {item_id}, Status: {status})"
            
        # Speech detection
        elif msg_type == "input_audio_buffer.speech_started":
            return f"🎤 SPEECH: User started speaking"
            
        elif msg_type == "input_audio_buffer.speech_stopped":
            return f"🎤 SPEECH: User stopped speaking"
            
        # Error handling
        elif msg_type == "error":
            error_message = msg.get('error', {}).get('message', 'Unknown error')
            error_code = msg.get('error', {}).get('code', 'unknown')
            return f"❌ ERROR: {error_message} (Code: {error_code})"
            
        # Default handler for other message types
        else:
            # For any other message types, return a short preview
            return f"ℹ️ {msg_type}: {str(msg)[:100]}..."

    def on_message(self, ws, message_str):
        msg = json.loads(message_str)
        msg_type = msg.get("type")

        # For audio delta messages, use a counter instead of logging each one
        if msg_type == "response.audio.delta":
            self.audio_received_counter += 1
            if self.audio_received_counter % 75 == 0:  # Log every 75th message
                self.log(f"🔊 AUDIO: Received {self.audio_received_counter} chunks from OpenAI")
        # For transcription deltas, format them more prominently
        elif msg_type == "conversation.item.input_audio_transcription.delta":
            transcript = msg.get("delta", "")
            if transcript and transcript.strip():  # Only log if there's actual content
                self.log(f"------ CONVERSATION ------\n👤 USER SAYING: \"{transcript.strip()}\"\n---------------------------")
                # Log user's speech to conversation history
                if self.session_id:
                    try:
                        log_conversation_turn(self.session_id, "user", transcript.strip())
                    except Exception as e:
                        self.log(f"ERROR: Failed to log user transcript to conversation history: {e}", logging.ERROR)
        # For other high-frequency events that we want to minimize in logs
        elif msg_type in ["response.audio_transcript.delta", "response.output.delta", "response.function_call_arguments.delta"]:
            pass  # Skip logging these entirely
        # For speech detection, use simplified format
        elif msg_type == "input_audio_buffer.speech_started":
            self.log(f"🎤 SPEECH: User started speaking | State: {self.get_app_state()}")
        elif msg_type == "input_audio_buffer.speech_stopped":
            self.log("🎤 SPEECH: User stopped speaking")
        # For all other message types, use the formatter
        else:
            formatted_message = self._format_message(msg, msg_type)
            self.log(formatted_message)

        if msg_type == "conversation.item.created":
            item = msg.get("item", {})
            item_id, item_role, item_type, item_status = item.get("id"), item.get("role"), item.get("type"), item.get("status")
            if item_role == "assistant" and item_type == "message" and item_status == "in_progress":
                if self.last_assistant_item_id != item_id:
                    self.log(f"------ CONVERSATION START ------\n🤖 ASSISTANT STARTING: New message (ID: {item_id})\n---------------------------")
                    self.last_assistant_item_id = item_id
                    self.current_assistant_item_played_ms = 0
                    # Log assistant's response start
                    if self.session_id:
                        try:
                            log_conversation_turn(self.session_id, "assistant", "Starting new response...")
                        except Exception as e:
                            self.log(f"ERROR: Failed to log assistant response start to conversation history: {e}", logging.ERROR)
        
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
            # Log tool call to conversation history
            if self.session_id:
                log_conversation_turn(
                    self.session_id,
                    "tool_call",
                    json.dumps({
                        "name": function_to_execute_name,
                        "arguments": final_args_to_use
                    })
                )
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
                
                # 1. Wait for any current audio to finish
                self.log("🔊 AUDIO: Waiting for current audio to complete...")
                audio_finished = self._wait_for_audio_completion()
                if not audio_finished:
                    self.log("⚠️ WARNING: Audio completion timeout reached")
                
                # 2. Add a small delay to ensure last message was heard
                end_conv_delay_s = self.config.get("END_CONV_AUDIO_FINISH_DELAY_S", 2.0)
                time.sleep(end_conv_delay_s)
                
                # 3. Clear all audio buffers
                if self.player:
                    self.player.clear()
                    self.player.flush()
                self.openai_audio_buffer_raw_bytes = b''
                
                # 4. Reset audio state
                self.last_assistant_item_id = None
                self.current_assistant_item_played_ms = 0
                
                # 5. Transition to wake word mode
                self.log(f"Client: Executing '{END_CONVERSATION_TOOL_NAME}' for reason: '{reason}'.")
                if self.wake_word_active:
                    self.set_app_state("LISTENING_FOR_WAKEWORD")
                    print(f"\n*** Assistant listening for wake word: '{self.wake_word_detector_instance.wake_word_model_name}' (Reason: {reason}) ***\n")
                else:
                    print(f"\n*** Conversation turn ended by LLM (Reason: {reason}). Ready for next query. ***\n")
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
            self.audio_received_counter += 1
            if self.audio_received_counter % 75 == 0:  # Log every 75th message
                self.log(f"🔊 AUDIO: Received {self.audio_received_counter} chunks from OpenAI")
            if item_id_of_delta and item_id_of_delta in self.client_initiated_truncated_item_ids:
                pass
            elif audio_data_b64:
                audio_data_bytes = base64.b64decode(audio_data_b64)
                self._process_and_play_audio(audio_data_bytes)
                if self.last_assistant_item_id and self.last_assistant_item_id == item_id_of_delta:
                    self.current_assistant_item_played_ms += self.client_audio_chunk_duration_ms
        
        elif msg_type == "response.audio.done":
            # Log completion with total count
            self.log(f"🔊 AUDIO COMPLETE: Received {self.audio_received_counter} total chunks")
            # Reset counter for next conversation turn
            self.audio_received_counter = 0
            
            if self.tsm_enabled:
                if len(self.openai_audio_buffer_raw_bytes) > 0:
                    self.log(f"🔄 TSM: Processing {len(self.openai_audio_buffer_raw_bytes)} remaining bytes")
                    final_segment_to_process_bytes_for_fallback = self.openai_audio_buffer_raw_bytes
                    try:
                        final_segment_bytes = self.openai_audio_buffer_raw_bytes
                        self.openai_audio_buffer_raw_bytes = b''
                        segment_np_int16 = np.frombuffer(final_segment_bytes, dtype=np.int16)
                        segment_np_float32 = segment_np_int16.astype(np.float32) / 32768.0
                        if segment_np_float32.size > 0:
                            stretched_audio_float32 = wsola(segment_np_float32, s=self.desired_playback_speed)
                            clipped_stretched_audio = np.clip(stretched_audio_float32, -1.0, 1.0)
                            stretched_audio_int16 = (clipped_stretched_audio * 32767.0).astype(np.int16)
                            stretched_audio_bytes = stretched_audio_int16.tobytes()
                            if self.player and len(stretched_audio_bytes) > 0:
                                self.player.play(stretched_audio_bytes)
                    except Exception as e_tsm_flush_proc:
                        self.log(f"❌ TSM ERROR: {e_tsm_flush_proc}. Falling back to raw audio.")
                        if self.player and final_segment_to_process_bytes_for_fallback and len(final_segment_to_process_bytes_for_fallback) > 0:
                           self.player.play(final_segment_to_process_bytes_for_fallback)
            else:
                if len(self.openai_audio_buffer_raw_bytes) > 0 and self.player:
                    self.player.play(self.openai_audio_buffer_raw_bytes)
                    self.openai_audio_buffer_raw_bytes = b''
            if self.player: self.player.flush()
            self.log(f"⚙️ STATE: Audio complete, app state: {self.get_app_state()}")
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
            self.log(f"🎤 SPEECH: User started speaking | State: {self.get_app_state()}")
            if self.get_app_state() == "SENDING_TO_OPENAI": self._perform_truncation(reason_prefix="Server VAD")
        elif msg_type == "input_audio_buffer.speech_stopped":
            self.log("🎤 SPEECH: User stopped speaking")
        elif msg_type == "error":
            error_message = msg.get('error', {}).get('message', 'Unknown error from OpenAI.')
            error_code = msg.get('error', {}).get('code', 'unknown')
            self.log(f"❌ ERROR: {error_message} (Code: {error_code})")
            if "session" in error_message.lower() or "authorization" in error_message.lower():
                self.log("⚠️ CRITICAL: Session/auth error. Closing connection."); self.connected = False
                if self.ws_app: self.ws_app.close()
    def on_error(self, ws, error):
        self._log_section("WebSocket ERROR TEST")
        self.log(f"Client: WebSocket error: {error}")
        self.connected = False
        
        # Reset all state variables related to the active session
        self.last_assistant_item_id = None
        self.current_assistant_item_played_ms = 0
        self.accumulated_tool_args.clear()
        self.client_initiated_truncated_item_ids.clear()
        
        # Only attempt to log the error if we have a session ID
        if self.session_id:
            try:
                # Convert error to a simple string to avoid serialization issues
                error_str = str(error) if error is not None else "Unknown WebSocket error"
                log_conversation_turn(
                    self.session_id,
                    "system_event",
                    json.dumps({"event": "websocket_error", "details": error_str})
                )
            except Exception as e_log:
                self.log(f"ERROR: Failed to log WebSocket error to conversation history: {e_log}")
    def on_close(self, ws, close_status_code, close_msg):
        self._log_section("WebSocket CLOSE")
        self.log(f"Client WS Closed: {close_status_code} {close_msg}")
        self.connected = False
        
        # Log connection close to conversation history if we have a session
        if self.session_id:
            try:
                # Create simple string representations that can be safely JSON serialized
                code_str = str(close_status_code) if close_status_code is not None else "null"
                reason_str = str(close_msg) if close_msg is not None else "null"
                
                log_conversation_turn(
                    self.session_id,
                    "system_event",
                    json.dumps({
                        "event": "websocket_closed",
                        "code": code_str,
                        "reason": reason_str
                    })
                )
            except Exception as e_log:
                self.log(f"ERROR: Failed to log WebSocket close to conversation history: {e_log}")
        
        # Create a safe status code string for the frontend notification
        status_code_str = str(close_status_code) if close_status_code is not None else "unknown"
        self._notify_frontend_disconnect(reason=f"Connection closed (Code: {status_code_str})")

    def run_client(self):
        self.log("Client: Starting run_client loop.")
        # Preserve self.session_id across reconnect attempts for history
        # It will be updated by session.created if OpenAI issues a new one.
        preserved_session_id_for_reconnect = self.session_id 

        while self.keep_outer_loop_running:
            self.log(f"Client: Attempting WebSocket connection (session_id for history: {preserved_session_id_for_reconnect}).")
            self.connected = False
            self.current_assistant_text_response = ""
            self.session_id = preserved_session_id_for_reconnect # Use the preserved one for on_open

            self.ws_app = websocket.WebSocketApp(self.ws_url, header=self.headers, on_open=self.on_open, on_message=self.on_message, on_error=self.on_error, on_close=self.on_close)
            try:
                self.ws_app.run_forever(ping_interval=70, ping_timeout=30)
            except Exception as e: self.log(f"Client: Exception in run_forever: {e}")
            finally:
                self.connected = False
                # --- Phase 4: Fallback frontend disconnect notification ---
                # If the loop is still supposed to run (not a graceful shutdown)
                # and the WebSocket appears to be truly dead (e.g., no sock or not connected),
                # and on_close might not have fired to send the notification.
                # This is a heuristic. A more robust way might involve a flag set by on_close.
                if self.keep_outer_loop_running:
                    # Check if ws_app or its socket is None, indicating a potentially ungraceful exit
                    # where on_close might not have been called.
                    ws_likely_dead = not hasattr(self.ws_app, 'sock') or \
                                    (hasattr(self.ws_app, 'sock') and not self.ws_app.sock) or \
                                    not self.connected # self.connected should be false here anyway
                    
                    # A simple approach: if we reach here and keep_outer_loop_running is true,
                    # assume a disconnect happened that might not have been reported by on_close.
                    # However, on_close *should* be called by run_forever before exiting.
                    # Let's rely on on_close for now and only add this if testing shows on_close isn't always hit.
                    # For now, we will primarily rely on on_close to send the disconnect.
                    # If testing reveals on_close isn't reliably called before this finally block
                    # on all disconnect scenarios, we can add a more robust check or an explicit call here.
                    pass # Relying on on_close for now to avoid duplicate notifications
                preserved_session_id_for_reconnect = self.session_id # Update with potentially new session_id from last run
                if not self.keep_outer_loop_running: break
                self.log(f"Client: Disconnected. Waiting {self.RECONNECT_DELAY_SECONDS}s.")
                for _ in range(self.RECONNECT_DELAY_SECONDS):
                    if not self.keep_outer_loop_running: break
                    time.sleep(1)
                if not self.keep_outer_loop_running: break
        self.log("Client: Exited run_client loop.")

    def close_connection(self):
        self.log("Client: close_connection() called.")
        self.keep_outer_loop_running = False
        if self.ws_app:
            try:
                if hasattr(self.ws_app, 'close') and callable(self.ws_app.close): self.ws_app.close()
            except: pass # Simplified
        self.connected = False