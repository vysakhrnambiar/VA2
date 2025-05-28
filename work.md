Okay, this is a fantastic set of final refinements for the OpenAI client's robustness and user experience during connectivity issues!
Updated Plan & Summary (FINAL VERSION - Incorporating Aggressive Reconnect, UI Disconnect Notification):
Project Goal:
To create a real-time voice assistant (main.py) integrating with OpenAI's Realtime API. Key features include an autonomous outbound calling system (calling_agent.py) that makes calls and provides updates, robust OpenAI connection management with context preservation, and a web UI (frontend/) that visually communicates assistant status and pending call updates.
I. Completed Components (Functionally Tested or Design Finalized):
Autonomous Calling Engine (calling_agent.py & call_analyzer_and_strategist.py):
Database Backend: SQLite (scheduled_calls.db) with scheduled_calls (master job records) and call_attempts (individual call attempt details) tables. Schema includes fields for objectives, status, retry counts, transcripts, strategist analysis, error details, and next_retry_at for timed callbacks. (db_setup.py handles schema creation).
Core Calling Workflow:
Polls scheduled_calls for PENDING or due RETRY_SCHEDULED jobs.
Creates a new record in call_attempts for each try.
Initiates calls via UltraVox (using a global ULTRAVOX_AGENT_ID and dynamic templateContext for per-call objectives) and Twilio.
Monitors UltraVox call status until termination.
Retrieves full call transcripts.
Call Strategist (call_analyzer_and_strategist.py):
Post-attempt, an LLM (e.g., GPT-4) analyzes the transcript, original job objective, and history of previous attempts for the same job.
Returns a structured JSON action_plan dictating the next step for the overall job (e.g., MARK_JOB_COMPLETED_SUCCESS, SCHEDULE_JOB_RETRY, MARK_JOB_FAILED_MAX_RETRIES), a summary for the main user, a revised objective if retrying, and any user-requested callback delays.
Includes LLM API call retries and robust JSON parsing/error handling.
Decision Implementation & DB Updates: calling_agent.py updates call_attempts (with attempt-specific outcomes) and scheduled_calls (overall job status, retry counts, next objective, next_retry_at, final summary) based on the strategist's plan.
Error Handling & Logging: Features API call retries (for UltraVox/Twilio), comprehensive logging to a rotating file (calling_agent.log), and a mechanism for handling stale/long-running jobs.
Single Call Processing: Currently processes one call job/attempt at a time.
II. Remaining Work to Achieve Full Integration & Enhanced Robustness:
A. Main Voice Assistant - Tooling for Call Management (Files: tools_definition.py, tool_executor.py, llm_prompt_config.py)
Tool: schedule_outbound_call
Define Schema (tools_definition.py):
Name: schedule_outbound_call
Description: For scheduling an outbound call.
Parameters: phone_number (string), contact_name (string), call_objective (string, detailed description of call's purpose).
Implement Handler (tool_executor.py):
Function handle_schedule_outbound_call receives parameters from main LLM.
Connects to scheduled_calls.db.
Inserts a new record into scheduled_calls table with overall_status='PENDING', initial_call_objective_description and current_call_objective_description (initially same as call_objective from tool), phone_number, contact_name. Default max_retries (e.g., from .env or hardcoded).
Return message to LLM: "Okay, I've scheduled the call to [Contact Name] regarding [objective snippet]. I will provide updates as they become available or when the task is complete."
Update LLM Instructions (llm_prompt_config.py):
Add schedule_outbound_call to the list of available tools.
Instruct the main LLM on its usage.
Provide a mapping for common internal contacts/departments to their names and phone numbers (e.g., "Operations contact is Mr. Akhil at +123..."). The LLM will use this to populate phone_number and contact_name for the tool call.
Tool: check_scheduled_call_status
Define Schema (tools_definition.py):
Name: check_scheduled_call_status
Parameters: job_id (integer, optional, if user/LLM might refer to a specific ID), contact_name (string, optional), call_objective_snippet (string, optional).
Implement Handler (tool_executor.py):
Function handle_check_scheduled_call_status queries scheduled_calls (and its latest call_attempts if more detail is needed).
Logic to find relevant job(s) based on parameters. If multiple match a vague query, it might summarize the most recent or ask for clarification.
Return a textual summary of the status:
If PENDING: "The call to [Contact] regarding [Objective] is scheduled and awaiting processing."
If PROCESSING: "The call to [Contact] regarding [Objective] is currently in progress (attempt #[X])."
If RETRY_SCHEDULED: "The call to [Contact] regarding [Objective] was last attempted on [date/time]. A retry is scheduled for around [next_retry_at]. The reason for the last retry was: [strategist_reasoning_for_attempt from last attempt]."
If COMPLETED_... or FAILED_... (and main_agent_informed_user is still FALSE for some reason): "The call to [Contact] regarding [Objective] has concluded. Outcome: [final_summary_for_main_agent]." (The primary notification path is via context priming, but this tool can act as a fallback).
If main_agent_informed_user is TRUE: "I previously updated you that the call to [Contact] regarding [Objective] concluded with the following outcome: [final_summary_for_main_agent]."
If no matching job: "I don't have a record of a scheduled call matching that description."
Update LLM Instructions (llm_prompt_config.py): Add this tool and instruct the LLM to use it if the user inquires about a previously scheduled call's status.
B. Main Voice Assistant - OpenAI Client: Robust Connection, Context & Update Handling (Files: openai_client.py, new conversation_history_db.py, web_server.py, frontend/script.js, main.py)
New conversation_history.db & conversation_history_db.py Module:
Schema: conversation_history.db with table conversation_turns (turn_id PK, session_id TEXT, timestamp TIMESTAMP, role TEXT, content TEXT).
Module conversation_history_db.py:
init_db(): Creates table if not exists.
add_turn(session_id, role, content): Inserts a new turn.
get_recent_turns(limit=20): Retrieves last N turns, ordered by timestamp.
(Optional) get_turns_for_session(session_id, limit=20).
Local Turn Logging (in OpenAISpeechClient.py):
Call conversation_history_db.add_turn(...) to log:
User's transcribed text (from response.audio_transcript.done).
Assistant's full text response (after concatenating response.output.delta).
Tool calls made by the assistant (from response.output.delta or response.function_call_arguments.done).
Tool results sent back to the assistant (before sending conversation.item.create for function output).
Use the self.session_id (from OpenAI's session.created message) for the session_id column.
Aggressive Automatic Reconnection Loop (in OpenAISpeechClient.py - run_client method):
Implement an outer while self.keep_outer_loop_running: (class attribute, default True).
Inside the loop, a try-except block around self.ws_app = websocket.WebSocketApp(...) and self.ws_app.run_forever(...).
run_forever should have ping_interval (e.g., 20s) and ping_timeout (e.g., 10s).
If run_forever exits (cleanly or via exception):
Set self.connected = False.
Call a new method self.notify_frontend_disconnect() (see B.5).
Log the disconnection event.
Wait for a short, configurable RECONNECT_DELAY_SECONDS (e.g., 5 seconds) before the outer loop tries to connect again.
self.keep_outer_loop_running is set to False only on explicit shutdown of the main application.
Context Priming on Reconnect (in OpenAISpeechClient.py - on_open method):
When on_open is called (new connection or reconnection):
Call self.notify_frontend_connect() (see B.5).
Fetch last N (e.g., 20) turns from conversation_history.db.get_recent_turns().
Get current UTC timestamp (datetime.utcnow().isoformat() + "Z").
Format these turns with relative time information (e.g., "(X minutes ago) User: ...", "(Y minutes ago) Assistant: ...").
Call a utility LLM (e.g., GPT-3.5-Turbo via a synchronous HTTP call) with a prompt to summarize this history concisely, providing the current time for context.
Prompt: "Current time: [current_utc_time]. Summarize the key unresolved topics, pending assistant actions, or the last user query from this recent conversation history: [formatted_history]. Focus on what's needed for the assistant to continue the conversation smoothly. Output a brief factual summary."
Fetch any uninformed call updates from scheduled_calls.db (where main_agent_informed_user = FALSE). Construct a text list of these updates (e.g., "Call to Contact A: [summary_A]; Call to Contact B: [summary_B]").
Construct effective_instructions for the session.update message by prepending:
The LLM-generated summary of recent conversational context.
The text of pending call updates.
Send the session.update message with these effective_instructions (appended to the original LLM_DEFAULT_INSTRUCTIONS).
After successfully sending instructions containing specific call updates, update those records in scheduled_calls.db to main_agent_informed_user = TRUE.
Frontend Notification for Connection Status & Call Updates (Requires changes in main.py, OpenAISpeechClient.py, web_server.py, frontend/script.js):
OpenAISpeechClient.py:
Add methods notify_frontend_disconnect() and notify_frontend_connect(). These will make HTTP POST requests to new endpoints on web_server.py (e.g., /api/ui_status_update).
Payload for disconnect: {"type": "connection_status", "status": "disconnected", "message": "Agent connectivity issues. Attempting to reconnect..."}
Payload for connect: {"type": "connection_status", "status": "connected", "message": "Agent connected."}
DB Monitoring Thread (in main.py):
When it finds a finalized job in scheduled_calls.db where main_agent_informed_user = FALSE:
It logs that an update is ready.
It makes an HTTP POST to web_server.py at /api/notify_call_update_available with payload: {"type": "new_call_update_available", "contact_name": "Mr. X", "status_summary": "Call outcome ready"}.
web_server.py:
New endpoint /api/ui_status_update (POST): Receives connection status, broadcasts to WebSocket clients.
New endpoint /api/notify_call_update_available (POST): Receives call update availability, broadcasts to WebSocket clients.
frontend/script.js:
Listen for WebSocket messages:
If type: "connection_status":
If status: "disconnected": Display a persistent, well-formatted message (e.g., bottom-left corner, perhaps with an animated icon) like "[Message from payload]". Clear any "call update available" icons.
If status: "connected": Remove the disconnection message. Re-evaluate if "call update available" icons need to be shown (e.g., if any updates came in during the disconnect and are now part of the LLM's primed context).
If type: "new_call_update_available": Display a distinct, persistent icon/banner (e.g., bottom-right) indicating "Update on call to [contact_name] available." This icon stays until either the LLM verbally delivers the update (hard to detect perfectly) or the screen is cleared by a new graph/markdown display. (A simpler approach for clearing: the icon is shown; once the main LLM mentions the update from its primed context, the user's next interaction or a screen clear implicitly acknowledges it. Or, the icon could have a dismiss button.)
This plan provides a very robust and user-friendly system. The division of responsibilities is clear, and error/disconnection handling is significantly improved.

THis is where we start our work from plan and start 