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
CONTEXT_HISTORY_LIMIT = 15
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

    def _process_and_play_audio(self, audio_data_bytes: bytes):
        if not self.tsm_enabled:
            if self.player: self.player.play(audio_data_bytes)
            return
        self.openai_audio_buffer_raw_bytes += audio_data_bytes
        while len(self.openai_audio_buffer_raw_bytes) >= self.TSM_PROCESSING_THRESHOLD_BYTES:
            segment_to_process_bytes = self.openai_audio_buffer_raw_bytes[:self.TSM_PROCESSING_THRESHOLD_BYTES]
            self.openai_audio_buffer_raw_bytes = self.openai_audio_buffer_raw_bytes[self.TSM_PROCESSING_THRESHOLD_BYTES:]
            try:
                segment_np_int16 = np.frombuffer(segment_to_process_bytes, dtype=np.int16)
                segment_np_float32 = segment_np_int16.astype(np.float32) / 32768.0
                if segment_np_float32.size == 0: continue
                stretched_audio_float32 = wsola(x=segment_np_float32, s=self.desired_playback_speed)
                clipped_stretched_audio = np.clip(stretched_audio_float32, -1.0, 1.0)
                stretched_audio_int16 = (clipped_stretched_audio * 32767.0).astype(np.int16)
                stretched_audio_bytes = stretched_audio_int16.tobytes()
                if self.player and len(stretched_audio_bytes) > 0: self.player.play(stretched_audio_bytes)
            except Exception as e_tsm_proc:
                self.log(f"ERROR TSM: {e_tsm_proc}. Playing raw.")
                if self.player: self.player.play(segment_to_process_bytes)

   # --- Phase 4: Frontend Notification Methods ---
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

        prompt_for_summarizer = f"""Current UTC time is {dt.now(timezone.utc).isoformat()}.
        Summarize the key points from the following recent conversation history. Focus on unresolved user questions, tasks the assistant was performing, or the last explicit user request to understand the immediate context for resuming the conversation.
        Output only a brief, factual summary. If the history is empty, too vague, or implies the conversation ended cleanly, output "No specific unresolved context to resume."

        History:
        {history_string_for_llm}

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
            ws.send(json.dumps(session_config))
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
            if self.session_id: log_conversation_turn(self.session_id, "tool_result", json.dumps({"call_id": call_id, "name": function_name, "output_snippet": tool_output_for_llm[:100]}))
        except Exception as e_tool_exec_thread:
            self.log(f"Client ERROR (Thread - {function_name}): {e_tool_exec_thread}")
            error_detail = f"Error in tool '{function_name}': {str(e_tool_exec_thread)[:200]}"
            tool_output_for_llm = json.dumps({"error": error_detail})
            if self.session_id: log_conversation_turn(self.session_id, "tool_result", json.dumps({"call_id": call_id, "name": function_name, "error": error_detail}))
        
        tool_response_payload = {"type": "conversation.item.create", "item": {"type": "function_call_output", "call_id": call_id, "output": tool_output_for_llm}}
        if self.ws_app and self.connected:
            try:
                self.ws_app.send(json.dumps(tool_response_payload))
                response_create_payload = {"type": "response.create", "response": {"modalities": ["text", "audio"], "voice": self.config.get("OPENAI_VOICE", "ash"), "output_audio_format": "pcm16"}}
                self.ws_app.send(json.dumps(response_create_payload))
            except Exception as e_send_tool_out: self.log(f"Client ERROR sending tool output for {call_id}: {e_send_tool_out}")

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
    def handle_local_user_speech_interrupt(self):
        if self.get_app_state() == "SENDING_TO_OPENAI": self._perform_truncation(reason_prefix="Local VAD")

    def on_message(self, ws, message_str):
        msg = json.loads(message_str)
        msg_type = msg.get("type")
        # Conditional logging (same as before)
        if msg_type == "session.created":
            new_session_id = msg.get('session', {}).get('id')
            if self.session_id != new_session_id: # Log only if session_id changes or is new
                 self.session_id = new_session_id # CRITICAL: Update self.session_id here
                 self.log(f"Client: OpenAI Session CREATED/UPDATED: ID={self.session_id}.")
                 if self.session_id: log_conversation_turn(self.session_id, "system_event", json.dumps({"event": "session.created", "details": msg.get('session', {})}))
            # UI print unchanged

        elif msg_type == "conversation.item.created":
            item = msg.get("item", {})
            if item.get("role") == "assistant" and item.get("type") == "message" and item.get("status") == "in_progress":
                if self.last_assistant_item_id != item.get("id"):
                    self.last_assistant_item_id = item.get("id"); self.current_assistant_item_played_ms = 0
                    self.current_assistant_text_response = ""

        elif msg_type == "response.output.delta":
            delta = msg.get("delta", {})
            if "text" in delta: self.current_assistant_text_response += delta["text"]
            if "tool_calls" in delta:
                for tc in delta["tool_calls"]:
                    if "id" in tc and "function" in tc and "arguments" in tc["function"]:
                        self.accumulated_tool_args[tc["id"]] = self.accumulated_tool_args.get(tc["id"], "") + tc["function"]["arguments"]
        
        elif msg_type == "response.function_call_arguments.delta":
            call_id, delta_args = msg.get("call_id"), msg.get("delta", "")
            if call_id: self.accumulated_tool_args[call_id] = self.accumulated_tool_args.get(call_id, "") + delta_args
            
        elif msg_type == "response.function_call_arguments.done":
            call_id = msg.get("call_id"); name = msg.get("name")
            args_str = self.accumulated_tool_args.pop(call_id, msg.get("arguments", "{}"))
            if self.session_id: log_conversation_turn(self.session_id, "tool_call", json.dumps({"call_id": call_id, "name": name, "arguments": args_str}))
            try: parsed_args = json.loads(args_str if args_str else "{}")
            except: parsed_args = {}; self.log(f"WARN: Bad JSON args for {name}: {args_str}") # Handle error to LLM
            if name == END_CONVERSATION_TOOL_NAME: # Simplified
                self.last_assistant_item_id = None; self.current_assistant_item_played_ms = 0; return
            elif name in TOOL_HANDLERS:
                threading.Thread(target=self._execute_tool_in_thread, args=(TOOL_HANDLERS[name], parsed_args, call_id, self.config, name), daemon=True).start()
            return

        elif msg_type == "response.audio_transcript.done":
            text = msg.get("transcript", {}).get("text", "")
            if text and self.session_id: log_conversation_turn(self.session_id, "user", text)

        elif msg_type == "response.audio.delta": # Play audio, track duration
            # ... same as before ...
            audio_data_b64 = msg.get("delta"); item_id = msg.get("item_id")
            if item_id and item_id in self.client_initiated_truncated_item_ids: return
            if audio_data_b64:
                self._process_and_play_audio(base64.b64decode(audio_data_b64))
                if self.last_assistant_item_id == item_id: self.current_assistant_item_played_ms += self.client_audio_chunk_duration_ms
                
        elif msg_type == "response.audio.done": # Log full assistant text
            if self.player: self.player.flush()
            if self.current_assistant_text_response and self.session_id:
                log_conversation_turn(self.session_id, "assistant", self.current_assistant_text_response)
            self.current_assistant_text_response = "" # Reset
            # UI print unchanged
        
        elif msg_type == "response.output_item.done": # Clear tracking if current item done
            item_id_done = msg.get("item", {}).get("id")
            if self.last_assistant_item_id == item_id_done:
                self.last_assistant_item_id = None; self.current_assistant_item_played_ms = 0
            if item_id_done in self.client_initiated_truncated_item_ids:
                self.client_initiated_truncated_item_ids.discard(item_id_done)
        
        elif msg_type == "error": # Log system error
            if self.session_id: log_conversation_turn(self.session_id, "system_event", json.dumps({"event": "openai_error", "details": msg.get('error', {})}))
            if "session" in msg.get('error', {}).get('message','').lower():
                if self.ws_app: self.ws_app.close() # Trigger reconnect

    def on_error(self, ws, error):
        self._log_section("WebSocket ERROR"); self.log(f"Client WS Error: {error}"); self.connected = False
        if self.session_id: log_conversation_turn(self.session_id, "system_event", json.dumps({"event": "websocket_error", "details": str(error)}))
    def on_close(self, ws, close_status_code, close_msg):
        self._log_section("WebSocket CLOSE"); self.log(f"Client WS Closed: {close_status_code} {close_msg}"); self.connected = False
        
        if self.session_id: log_conversation_turn(self.session_id, "system_event", json.dumps({"event": "websocket_closed", "code": close_status_code, "reason": close_msg}))
        self._notify_frontend_disconnect(reason=f"Connection closed (Code: {close_status_code})")

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
                self.ws_app.run_forever(ping_interval=self.config.get("OPENAI_PING_INTERVAL_S", 20), ping_timeout=self.config.get("OPENAI_PING_TIMEOUT_S", 10))
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