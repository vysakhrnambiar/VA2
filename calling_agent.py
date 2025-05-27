# calling_agent.py
import os
import time
import json
import requests
import sqlite3
from twilio.rest import Client as TwilioClient
from dotenv import load_dotenv
from datetime import datetime, timedelta

# Import the strategist function
from call_analyzer_and_strategist import analyze_and_strategize_call_outcome

# --- Configuration ---
load_dotenv()

DATABASE_NAME = "scheduled_calls.db"
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), DATABASE_NAME)

ULTRAVOX_API_KEY = os.getenv("ULTRAVOX_API_KEY")
ULTRAVOX_AGENT_ID_GLOBAL = os.getenv("ULTRAVOX_AGENT_ID")
ULTRAVOX_BASE_URL = "https://api.ultravox.ai/api"

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER_FROM = os.getenv("TWILIO_PHONE_NUMBER")

OPENAI_API_KEY_FOR_STRATEGIST = os.getenv("OPENAI_API_KEY") # Strategist needs its own key
STRATEGIST_LLM_MODEL = os.getenv("STRATEGIST_LLM_MODEL", "gpt-4-turbo-preview")


CALLING_AGENT_POLLING_INTERVAL_SECONDS = int(os.getenv("CALLING_AGENT_POLLING_INTERVAL_SECONDS", 10))
MAX_RETRIES_DEFAULT = int(os.getenv("CALLING_AGENT_MAX_RETRIES", 3))
COMPANY_NAME_FOR_AGENT_DEFAULT = os.getenv("CALLING_AGENT_COMPANY_NAME", "DTC Executive Office")
MAX_JOB_PROCESSING_HOURS = int(os.getenv("MAX_JOB_PROCESSING_HOURS", 24)) # Max time a job can be in 'PROCESSING'

API_CALL_MAX_RETRIES = int(os.getenv("API_CALL_MAX_RETRIES", 2)) # Retries for external API calls
API_CALL_RETRY_DELAY_SECONDS = int(os.getenv("API_CALL_RETRY_DELAY_SECONDS", 3))

# --- Logging Setup ---
import logging
from logging.handlers import RotatingFileHandler

logger = logging.getLogger("CallingAgent")
logger.setLevel(logging.DEBUG) # Set to DEBUG to capture all levels of messages

# Console Handler (optional, for seeing logs in console too)
# ch = logging.StreamHandler()
# ch.setLevel(logging.INFO) # Console can be less verbose
# ch_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(module)s - %(message)s')
# ch.setFormatter(ch_formatter)
# logger.addHandler(ch)

# Rotating File Handler
log_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calling_agent.log")
fh = RotatingFileHandler(log_file_path, maxBytes=5*1024*1024, backupCount=5) # 5MB per file, keep 5 backups
fh.setLevel(logging.DEBUG)
fh_formatter = logging.Formatter('%(asctime)s - %(levelname)s - [%(name)s.%(funcName)s:%(lineno)d] - %(message)s')
fh.setFormatter(fh_formatter)
logger.addHandler(fh)

# Pass logger to strategist module if it uses the same logger name
strategist_logger = logging.getLogger("call_analyzer_and_strategist")
if not strategist_logger.hasHandlers(): # Avoid adding duplicate handlers if already configured
    for handler in logger.handlers: # Share handlers
        strategist_logger.addHandler(handler)
    strategist_logger.setLevel(logging.DEBUG)


# --- Database Functions ---
def get_db_connection():
    logger.debug(f"Attempting to connect to DB at: {DB_PATH}")
    if not os.path.exists(DB_PATH):
        logger.critical(f"DATABASE FILE NOT FOUND AT: {DB_PATH}")
        return None
    try:
        conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;") # Ensure FKs are enforced
        logger.debug("DB connection established with foreign keys ON.")
        return conn
    except sqlite3.Error as e:
        logger.error(f"Failed to connect to DB or enable foreign keys: {e}")
        return None


def fetch_pending_call_job(conn):
    if conn is None: return None
    try:
        cursor = conn.cursor()
        # Fetch PENDING jobs or RETRY_SCHEDULED jobs where next_retry_at is past or null
        query = """
            SELECT * FROM scheduled_calls
            WHERE overall_status = 'PENDING' OR 
                  (overall_status = 'RETRY_SCHEDULED' AND (next_retry_at IS NULL OR strftime('%Y-%m-%d %H:%M:%S', next_retry_at) <= strftime('%Y-%m-%d %H:%M:%S', 'now', 'localtime'))
                  )
            ORDER BY CASE overall_status WHEN 'PENDING' THEN 0 ELSE 1 END, created_at ASC
            LIMIT 1
        """
        logger.debug(f"Executing query for pending/retry jobs: {query}")
        cursor.execute(query)
        job = cursor.fetchone()
        if job:
            logger.info(f"Fetched job ID: {job['id']}, Status: {job['overall_status']}")
        else:
            logger.debug("No PENDING or due RETRY_SCHEDULED jobs found.")
        return job
    except sqlite3.Error as e:
        logger.error(f"SQLite error during fetch_pending_call_job: {e}")
        return None

def get_previous_attempts_for_job(conn, job_id):
    if conn is None: return []
    try:
        cursor = conn.cursor()
        query = "SELECT * FROM call_attempts WHERE job_id = ? ORDER BY attempt_number ASC"
        cursor.execute(query, (job_id,))
        attempts = [dict(row) for row in cursor.fetchall()]
        logger.debug(f"Job {job_id}: Fetched {len(attempts)} previous attempts from DB.")
        return attempts
    except sqlite3.Error as e:
        logger.error(f"Job {job_id}: SQLite error fetching previous attempts: {e}")
        return []

def update_scheduled_call_status(conn, job_id, overall_status, **kwargs):
    logger.debug(f"Job {job_id}: Updating scheduled_calls.overall_status to {overall_status}, kwargs: {kwargs}")
    kwargs['updated_at'] = datetime.now() # Always update this
    # Use a common function for updating to reduce redundancy
    _update_db_record(conn, "scheduled_calls", {"id": job_id}, overall_status=overall_status, **kwargs)


def create_call_attempt_record(conn, job_id, attempt_number, objective_for_this_attempt):
    logger.debug(f"Job {job_id}: Creating call_attempts record for attempt #{attempt_number}")
    insert_sql = """
        INSERT INTO call_attempts (job_id, attempt_number, objective_for_this_attempt, attempt_status)
        VALUES (?, ?, ?, 'INITIATED')
    """ # Initial status for an attempt
    try:
        cursor = conn.cursor()
        cursor.execute(insert_sql, (job_id, attempt_number, objective_for_this_attempt))
        conn.commit()
        attempt_id = cursor.lastrowid
        logger.info(f"Job {job_id}, Attempt #{attempt_number}: Created record in call_attempts with ID {attempt_id}.")
        return attempt_id
    except sqlite3.Error as e:
        logger.error(f"Job {job_id}, Attempt #{attempt_number}: Failed to create call_attempts record: {e}")
        return None

def update_call_attempt_record(conn, attempt_id, **kwargs):
    logger.debug(f"Attempt {attempt_id}: Updating call_attempts record with kwargs: {kwargs}")
    # `attempt_ended_at` should be set when attempt truly finishes
    if "transcript" in kwargs or "end_reason" in kwargs or "attempt_status" in kwargs and "FAILED" in kwargs["attempt_status"].upper():
         kwargs.setdefault('attempt_ended_at', datetime.now())

    _update_db_record(conn, "call_attempts", {"attempt_id": attempt_id}, **kwargs)

def _update_db_record(conn, table_name, conditions_dict, **kwargs):
    """ Helper to update a record in any table """
    if conn is None: return
    
    set_parts = []
    values = []
    for key, value in kwargs.items():
        set_parts.append(f"{key} = ?")
        values.append(value)
    
    condition_parts = []
    condition_values = []
    for key, value in conditions_dict.items():
        condition_parts.append(f"{key} = ?")
        condition_values.append(value)

    if not set_parts or not condition_parts:
        logger.error(f"Update for {table_name}: Missing SET parts or WHERE conditions.")
        return

    set_sql = ", ".join(set_parts)
    condition_sql = " AND ".join(condition_parts)
    
    sql = f"UPDATE {table_name} SET {set_sql} WHERE {condition_sql}"
    params = values + condition_values
    
    try:
        cursor = conn.cursor()
        logger.debug(f"Executing DB Update on {table_name}: SQL: {sql}, Params: {params}")
        cursor.execute(sql, params)
        conn.commit()
        logger.info(f"Record in {table_name} (where {condition_sql}) updated with {kwargs}.")
    except sqlite3.Error as e:
        logger.error(f"Failed to update record in {table_name} (where {condition_sql}). Error: {e}")


# --- API Call Wrapper with Retries ---
def make_api_request(method, url, headers=None, json_payload=None, timeout=20, attempt_desc="API call"):
    for i in range(API_CALL_MAX_RETRIES + 1):
        try:
            logger.debug(f"{attempt_desc}: Attempt {i+1}/{API_CALL_MAX_RETRIES+1} - {method} {url}")
            if json_payload: logger.debug(f"Payload: {json.dumps(json_payload, indent=1)}")
            
            response = requests.request(method, url, headers=headers, json=json_payload, timeout=timeout)
            
            logger.debug(f"{attempt_desc}: Response Status {response.status_code}")
            if response.content and response.status_code >=400 : # Log error responses
                 logger.warning(f"{attempt_desc}: Error Response Content (first 500 chars): {response.text[:500]}")
            
            response.raise_for_status() # Raises HTTPError for 4xx/5xx
            return response.json() if response.content else None # Return None if no content
        
        except requests.exceptions.HTTPError as http_err:
            logger.warning(f"{attempt_desc}: HTTP error (attempt {i+1}): {http_err}")
            # For 4xx client errors (except 429 too many requests), usually no point retrying
            if http_err.response.status_code in [400, 401, 403, 404] and http_err.response.status_code != 429:
                logger.error(f"{attempt_desc}: Client error {http_err.response.status_code}, not retrying.")
                raise # Re-raise to be caught by process_call_job
            if i == API_CALL_MAX_RETRIES: raise # Re-raise if max retries hit
        except requests.exceptions.RequestException as req_err: # Other network errors
            logger.warning(f"{attempt_desc}: Request exception (attempt {i+1}): {req_err}")
            if i == API_CALL_MAX_RETRIES: raise
        
        if i < API_CALL_MAX_RETRIES:
            delay = API_CALL_RETRY_DELAY_SECONDS * (i + 1)
            logger.info(f"{attempt_desc}: Retrying in {delay}s...")
            time.sleep(delay)
    return None # Should be unreachable if raise is used correctly on final attempt


# --- Core Call Processing Logic ---
def process_call_job(job_master_details_row):
    job_master_details = dict(job_master_details_row)
    job_id = job_master_details["id"]
    logger.info(f"Job {job_id}: Starting processing for '{job_master_details.get('contact_name','N/A')}' ({job_master_details.get('phone_number','N/A')})")
    
    conn = get_db_connection()
    if not conn:
        logger.error(f"Job {job_id}: Could not get DB connection for processing. Job will be retried later.")
        return # Agent will pick it up again after polling interval

    current_attempt_number = job_master_details.get("retries_attempted", 0) + 1
    objective_for_this_attempt = job_master_details.get("current_call_objective_description", job_master_details["initial_call_objective_description"])
    
    # Create a new record for this specific attempt
    attempt_id = create_call_attempt_record(conn, job_id, current_attempt_number, objective_for_this_attempt)
    if not attempt_id:
        logger.error(f"Job {job_id}: Failed to create call_attempts record. Cannot proceed with this attempt.")
        update_scheduled_call_status(conn, job_id, "FAILED_PERMANENT_ERROR", final_summary_for_main_agent="Internal DB error creating attempt record.")
        conn.close()
        return

    # Update master job status to show it's actively being processed
    update_scheduled_call_status(conn, job_id, "PROCESSING")

    ultravox_call_id_for_attempt = None
    twilio_call_sid_for_attempt = None
    transcript_text_for_attempt = "Transcript not retrieved."
    final_end_reason_for_attempt = "Unknown"

    try:
        # 1. Create UltraVox Call
        if not ULTRAVOX_AGENT_ID_GLOBAL:
            raise ValueError("Global ULTRAVOX_AGENT_ID not set in environment.")

        uv_template_context = {
            "company_name": job_master_details.get("company_name_for_agent", COMPANY_NAME_FOR_AGENT_DEFAULT),
            "contact_name": job_master_details.get("contact_name", "Valued Contact"),
            "call_objective": objective_for_this_attempt
        }
        uv_call_payload = {
            "medium": {"twilio": {}}, "firstSpeakerSettings": {"agent": {"uninterruptible": False}},
            "templateContext": uv_template_context,
            "metadata": {"db_job_id": str(job_id), "db_attempt_id": str(attempt_id)},
            "recordingEnabled": True
        }
        
        uv_headers = {"X-API-Key": ULTRAVOX_API_KEY, "Content-Type": "application/json"}
        uv_call_response_data = make_api_request(
            "POST", f"{ULTRAVOX_BASE_URL}/agents/{ULTRAVOX_AGENT_ID_GLOBAL}/calls",
            headers=uv_headers, json_payload=uv_call_payload, attempt_desc=f"Job {job_id} UV CreateCall"
        )
        if not uv_call_response_data: raise ValueError("UltraVox Create Call API failed after retries.")

        ultravox_call_id_for_attempt = uv_call_response_data.get("callId")
        join_url = uv_call_response_data.get("joinUrl")
        if not ultravox_call_id_for_attempt or not join_url:
            raise ValueError("Missing callId or joinUrl from UltraVox response.")
        
        logger.info(f"Job {job_id}, Attempt {attempt_id}: UltraVox call created. UV_CallID: {ultravox_call_id_for_attempt}")
        update_call_attempt_record(conn, attempt_id, ultravox_call_id=ultravox_call_id_for_attempt, attempt_status="TWILIO_CALL_PENDING")

        # 2. Place Twilio Call
        escaped_join_url = join_url.replace("&", "&")
        twiml_response = f'<Response><Connect><Stream url="{escaped_join_url}"/></Connect></Response>'
        
        # Twilio client doesn't have built-in retries in the same way, handle its errors directly
        try:
            twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            call = twilio_client.calls.create(
                to=job_master_details["phone_number"], from_=TWILIO_PHONE_NUMBER_FROM, twiml=twiml_response
            )
            twilio_call_sid_for_attempt = call.sid
            logger.info(f"Job {job_id}, Attempt {attempt_id}: Twilio call initiated. SID: {twilio_call_sid_for_attempt}")
            update_call_attempt_record(conn, attempt_id, twilio_call_sid=twilio_call_sid_for_attempt, attempt_status="IN_PROGRESS_MONITORING")
        except Exception as e_twilio:
            logger.error(f"Job {job_id}, Attempt {attempt_id}: Error placing Twilio call: {e_twilio}")
            raise ValueError(f"Twilio API error: {str(e_twilio)[:200]}") # Re-raise to be caught by main try-except

        # 3. Monitor UltraVox Call Status
        logger.info(f"Job {job_id}, Attempt {attempt_id}: Monitoring UV Call {ultravox_call_id_for_attempt} for termination...")
        call_terminated = False
        max_monitoring_duration_sec = int(os.getenv("UV_CALL_MONITOR_TIMEOUT_SEC", 300)) # 5 mins
        monitoring_interval_sec = 15 
        start_time = time.time()
        
        while not call_terminated and (time.time() - start_time) < max_monitoring_duration_sec:
            time.sleep(monitoring_interval_sec)
            uv_status_data = make_api_request(
                "GET", f"{ULTRAVOX_BASE_URL}/calls/{ultravox_call_id_for_attempt}",
                headers=uv_headers, attempt_desc=f"Job {job_id} UV PollStatus"
            )
            if not uv_status_data: # Should have raised if all retries failed
                logger.warning(f"Job {job_id}, Attempt {attempt_id}: Failed to poll UltraVox status after retries. Assuming call might be stuck or API issue.")
                # This situation needs careful handling, might not want to immediately fail the job.
                # For now, we'll let the outer monitoring timeout handle it if this persists.
                continue 

            ended_ts = uv_status_data.get("ended")
            final_end_reason_for_attempt = uv_status_data.get("endReason", "Unknown") # Store this
            logger.debug(f"Job {job_id}, Attempt {attempt_id}: Polled. EndedTS: {ended_ts}, EndReason: {final_end_reason_for_attempt}")
            if ended_ts or final_end_reason_for_attempt:
                call_terminated = True
                logger.info(f"Job {job_id}, Attempt {attempt_id}: Call termination detected. End Reason: {final_end_reason_for_attempt}")
                break
        
        if not call_terminated:
            logger.warning(f"Job {job_id}, Attempt {attempt_id}: Call monitoring timed out after {max_monitoring_duration_sec}s.")
            update_call_attempt_record(conn, attempt_id, attempt_status="MONITORING_TIMEOUT", end_reason="MonitoringTimeout", attempt_error_details="Call did not end within monitoring period.")
            final_end_reason_for_attempt = "MonitoringTimeout" # Set this for the strategist
            # Attempt to hang up Twilio call as a fallback
            if twilio_call_sid_for_attempt:
                try: TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN).calls(twilio_call_sid_for_attempt).update(status='completed')
                except Exception as e_t_cancel: logger.warning(f"Job {job_id}, Attempt {attempt_id}: Failed to force-end Twilio call: {e_t_cancel}")
            # Proceed to strategist, it should see the MonitoringTimeout as end_reason

        update_call_attempt_record(conn, attempt_id, end_reason=final_end_reason_for_attempt, attempt_status="TRANSCRIPT_PENDING")

        # 4. Get Transcript
        logger.info(f"Job {job_id}, Attempt {attempt_id}: Retrieving transcript for UV Call {ultravox_call_id_for_attempt}...")
        messages_payload = make_api_request(
            "GET", f"{ULTRAVOX_BASE_URL}/calls/{ultravox_call_id_for_attempt}/messages",
            headers=uv_headers, attempt_desc=f"Job {job_id} UV GetMessages"
        )
        if not messages_payload:
            raise ValueError("UltraVox GetMessages API failed after retries or returned no content.")

        formatted_lines = []
        if messages_payload.get("results"):
            for message in messages_payload["results"]:
                role = message.get("role", "UNKNOWN")
                text = message.get("text", "").strip()
                tool = message.get("toolName")
                if role == "MESSAGE_ROLE_AGENT": line = f"Agent: {text if text else '[No text]'}"
                elif role == "MESSAGE_ROLE_USER": line = f"User: {text if text else '[No STT/text]'}"
                elif role == "MESSAGE_ROLE_TOOL_CALL": line = f"System: [Tool Call: {tool}, Args: {text}]"
                elif role == "MESSAGE_ROLE_TOOL_RESULT": line = f"System: [Tool Result: {tool}, Out: {text}]"
                else: line = f"{role}: {text if text else '[No text]'}"
                formatted_lines.append(line)
            transcript_text_for_attempt = "\n".join(formatted_lines)
        else:
            transcript_text_for_attempt = "No messages found in transcript results."
        logger.info(f"Job {job_id}, Attempt {attempt_id}: Transcript retrieved (length {len(transcript_text_for_attempt)} chars).")
        update_call_attempt_record(conn, attempt_id, transcript=transcript_text_for_attempt, attempt_status="STRATEGY_PENDING")

        # 5. Call Strategist
        logger.info(f"Job {job_id}, Attempt {attempt_id}: Analyzing call outcome with strategist...")
        previous_attempts = get_previous_attempts_for_job(conn, job_id)
        # Filter out the current attempt if it somehow got into history (shouldn't happen with this flow)
        previous_attempts_filtered = [p for p in previous_attempts if p.get('attempt_id') != attempt_id]


        strategist_llm_config = {
            "api_key": OPENAI_API_KEY_FOR_STRATEGIST,
            "model_name": STRATEGIST_LLM_MODEL
        }
        action_plan = analyze_and_strategize_call_outcome(
            db_job_details=job_master_details, # Master job details
            call_transcript=transcript_text_for_attempt,
            ultravox_call_id_of_attempt=ultravox_call_id_for_attempt,
            twilio_call_sid_of_attempt=twilio_call_sid_for_attempt,
            previous_attempts_history=previous_attempts_filtered, # Pass history of *other* attempts
            llm_client_config=strategist_llm_config
        )

        if not action_plan or "error" in action_plan:
            error_msg = action_plan.get("error", "Unknown error from strategist") if action_plan else "No action plan returned"
            raw_resp = action_plan.get("raw_response", "") if action_plan else ""
            logger.error(f"Job {job_id}, Attempt {attempt_id}: Strategist LLM failed. Error: {error_msg}. Raw: {raw_resp[:200]}")
            update_call_attempt_record(conn, attempt_id, attempt_status="STRATEGY_FAILED", strategist_reasoning_for_attempt=f"Strategist Error: {error_msg}")
            # Decide how to handle overall job: maybe retry strategy later or fail job
            update_scheduled_call_status(conn, job_id, "FAILED_PERMANENT_ERROR", final_summary_for_main_agent=f"Call analysis failed: {error_msg}")
            return # Exit from this attempt processing

        logger.info(f"Job {job_id}, Attempt {attempt_id}: Strategist action plan: {action_plan.get('next_action_decision_for_job')}")
        
        # Update current attempt record with strategist's findings for this attempt
        update_call_attempt_record(conn, attempt_id,
            strategist_summary_of_attempt=action_plan.get("summary_for_main_agent"),
            strategist_objective_met_status_for_attempt=action_plan.get("objective_met_status_for_current_attempt"),
            strategist_reasoning_for_attempt=action_plan.get("reasoning_for_decision"),
            attempt_status="COMPLETED_ANALYZED"
        )

        # Update Master Job Record based on strategist's decision for the JOB
        master_job_update_args = {
            "final_summary_for_main_agent": action_plan.get("summary_for_main_agent"), # Use latest summary for now
            "updated_at": datetime.now() # Ensure this is updated
        }
        new_overall_job_status = "FAILED_PERMANENT_ERROR" # Default if not set by logic

        decision = action_plan.get("next_action_decision_for_job")
        if decision == "MARK_JOB_COMPLETED_SUCCESS":
            new_overall_job_status = "COMPLETED_SUCCESS"
        elif decision == "SCHEDULE_JOB_RETRY":
            current_total_attempts_made = job_master_details.get("retries_attempted", 0) + 1 # This attempt counts
            max_r = job_master_details.get("max_retries", MAX_RETRIES_DEFAULT)
            if current_total_attempts_made < max_r : # Note: max_retries is total allowed attempts, not just retries after first. Adjust if needed.
                                                      # If max_retries = 3, means 1st call + 2 retries. So 3 total attempts.
                new_overall_job_status = "RETRY_SCHEDULED"
                master_job_update_args["retries_attempted"] = current_total_attempts_made
                master_job_update_args["current_call_objective_description"] = action_plan.get("next_call_objective_if_retry", job_master_details["current_call_objective_description"])
                
                requested_delay_min = action_plan.get("requested_retry_delay_minutes")
                if requested_delay_min and isinstance(requested_delay_min, int) and requested_delay_min > 0:
                    master_job_update_args["next_retry_at"] = datetime.now() + timedelta(minutes=requested_delay_min)
                else: # Default cool-down before general retry
                    master_job_update_args["next_retry_at"] = datetime.now() + timedelta(minutes=CALLING_AGENT_POLLING_INTERVAL_SECONDS * 2) # e.g. wait 2 poll cycles
            else:
                new_overall_job_status = "FAILED_MAX_RETRIES"
                logger.info(f"Job {job_id}: Max retries ({max_r}) reached. Overall job status: {new_overall_job_status}")
        elif decision == "MARK_JOB_FAILED_OBJECTIVE_UNACHIEVED":
            new_overall_job_status = "COMPLETED_OBJECTIVE_NOT_MET" # It completed, but objective wasn't met and no more retries.
        elif decision == "MARK_JOB_FAILED_MAX_RETRIES": # Explicit from LLM
             new_overall_job_status = "FAILED_MAX_RETRIES"

        update_scheduled_call_status(conn, job_id, new_overall_job_status, **master_job_update_args)
        logger.info(f"Job {job_id}: Master job status updated to {new_overall_job_status}.")

    except Exception as e_job_processing:
        logger.critical(f"Job {job_id}, Attempt {attempt_id if 'attempt_id' in locals() else 'N/A'}: Unhandled exception during call processing: {e_job_processing}", exc_info=True)
        error_summary = f"Core processing error: {str(e_job_processing)[:200]}"
        if attempt_id: # If attempt record was created
            update_call_attempt_record(conn, attempt_id, attempt_status="PROCESSING_ERROR", attempt_error_details=error_summary, end_reason="ProcessingError")
        # Update master job
        update_scheduled_call_status(conn, job_id, "FAILED_PERMANENT_ERROR", final_summary_for_main_agent=error_summary)
    finally:
        if conn:
            conn.close()
            logger.debug(f"Job {job_id}: DB connection for processing closed.")


# --- Stale Job Management ---
def handle_stale_jobs(conn):
    if conn is None: return
    try:
        cursor = conn.cursor()
        # Jobs stuck in PROCESSING for too long
        stale_threshold = datetime.now() - timedelta(hours=MAX_JOB_PROCESSING_HOURS)
        query = "SELECT id FROM scheduled_calls WHERE overall_status = 'PROCESSING' AND updated_at < ?"
        
        stale_jobs_found = 0
        for row in cursor.execute(query, (stale_threshold,)):
            job_id = row['id']
            logger.warning(f"Job {job_id}: Found stale job (stuck in PROCESSING). Marking as FAILED_PERMANENT_ERROR.")
            update_scheduled_call_status(conn, job_id, "FAILED_PERMANENT_ERROR",
                                         final_summary_for_main_agent=f"Job exceeded max processing time of {MAX_JOB_PROCESSING_HOURS} hours.")
            stale_jobs_found +=1
        if stale_jobs_found > 0:
             conn.commit() # Commit after all updates
        logger.debug(f"Stale job check completed. {stale_jobs_found} jobs updated.")

    except sqlite3.Error as e:
        logger.error(f"Error during stale job handling: {e}")


# --- Main Loop ---
def main_loop():
    logger.info("Calling Agent service started.")
    if not ULTRAVOX_AGENT_ID_GLOBAL:
        logger.critical("Global ULTRAVOX_AGENT_ID not set. Agent cannot process calls.")
        return
    if not OPENAI_API_KEY_FOR_STRATEGIST:
        logger.critical("OpenAI API Key for Strategist LLM not set. Agent cannot analyze calls.")
        return

    # Initial stale job check on startup
    startup_conn = get_db_connection()
    if startup_conn:
        handle_stale_jobs(startup_conn)
        startup_conn.close()
    
    while True:
        conn_poll = None
        job_row_obj = None
        try:
            conn_poll = get_db_connection()
            if conn_poll:
                handle_stale_jobs(conn_poll) # Periodically check for stale jobs
                job_row_obj = fetch_pending_call_job(conn_poll)
            else:
                logger.error("Failed to get DB connection for polling. Retrying in a bit...")
                time.sleep(CALLING_AGENT_POLLING_INTERVAL_SECONDS)
                continue

            if job_row_obj:
                logger.info(f"Found job: ID {job_row_obj['id']}, "
                            f"To: {job_row_obj['contact_name'] if 'contact_name' in job_row_obj.keys() and job_row_obj['contact_name'] else 'N/A'}, "
                            f"Status: {job_row_obj['overall_status'] if 'overall_status' in job_row_obj.keys() else 'N/A'}")
                process_call_job(job_row_obj) # This is a blocking call for now (one job at a time)
            else:
                logger.debug("No pending jobs this cycle.")
        
        except Exception as e_main_loop: # Catch-all for unexpected errors in the main loop itself
            logger.critical(f"CRITICAL UNHANDLED ERROR in main polling loop: {e_main_loop}", exc_info=True)
            # Avoid rapid looping on truly critical, unrecoverable errors in the loop itself
            time.sleep(CALLING_AGENT_POLLING_INTERVAL_SECONDS * 5) 
        finally:
            if conn_poll:
                conn_poll.close()
                logger.debug("Polling DB connection closed.")
        
        logger.debug(f"Waiting for {CALLING_AGENT_POLLING_INTERVAL_SECONDS} seconds before next poll.")
        time.sleep(CALLING_AGENT_POLLING_INTERVAL_SECONDS)

if __name__ == "__main__":
    logger.info(f"DB Path used by agent: {DB_PATH}")
    if not all([ULTRAVOX_API_KEY, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER_FROM, ULTRAVOX_AGENT_ID_GLOBAL, OPENAI_API_KEY_FOR_STRATEGIST]):
        logger.critical("Essential API credentials, Global Agent ID, or Strategist API Key missing. Agent cannot start properly.")
    else:
        try:
            main_loop()
        except KeyboardInterrupt:
            logger.info("Calling Agent service shutting down due to KeyboardInterrupt.")
        except Exception as e_top_level:
            logger.critical(f"CALLING AGENT FAILED UNEXPECTEDLY AT TOP LEVEL: {e_top_level}", exc_info=True)