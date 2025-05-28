# tool_executor.py
import json
import os
import requests # For synchronous HTTP requests
from datetime import datetime

from dateutil import parser as dateutil_parser # For flexible date string parsing
from dateutil.relativedelta import relativedelta # For "two days back" etc.
from datetime import datetime, date, timedelta, time # Keep existing datetime
import sqlite3 # For database operations

# Import tool names from tools_definition
from tools_definition import (
    SEND_EMAIL_SUMMARY_TOOL_NAME,
    RAISE_TICKET_TOOL_NAME,
    GET_BOLT_KB_TOOL_NAME,
    GET_DTC_KB_TOOL_NAME,
    DISPLAY_ON_INTERFACE_TOOL_NAME,
    GET_TAXI_IDEAS_FOR_TODAY_TOOL_NAME,
    GENERAL_GOOGLE_SEARCH_TOOL_NAME,
    # New tool names for Phase 1
    SCHEDULE_OUTBOUND_CALL_TOOL_NAME,
    CHECK_SCHEDULED_CALL_STATUS_TOOL_NAME
)

# Import the new KB extraction function from kb_llm_extractor.py
from kb_llm_extractor import extract_relevant_sections

# Import the new Google services module
try:
    from google_llm_services import get_gemini_response, GOOGLE_API_KEY
    GOOGLE_SERVICES_AVAILABLE = bool(GOOGLE_API_KEY)
except ImportError:
    print("[TOOL_EXECUTOR] WARNING: google_llm_services.py not found or GOOGLE_API_KEY missing. Google-based tools will not function.")
    GOOGLE_SERVICES_AVAILABLE = False
    def get_gemini_response(user_prompt_text: str, system_instruction_text: str, use_google_search_tool: bool = False, model_name: str = "") -> str:
        return "Error: Google AI services are not available (module load failure)."

# --- Knowledge Base File Paths & DB Path ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
KB_FOLDER_PATH = os.path.join(BASE_DIR, "knowledge_bases")
BOLT_KB_FILE = os.path.join(KB_FOLDER_PATH, "bolt_kb.txt")
DTC_KB_FILE = os.path.join(KB_FOLDER_PATH, "dtc_kb.txt")

DATABASE_NAME = "scheduled_calls.db"
DB_PATH = os.path.join(BASE_DIR, DATABASE_NAME)
DEFAULT_MAX_RETRIES = 3 # Default for new scheduled calls

# Helper for logging within this module
def _tool_log(message):
    print(f"[TOOL_EXECUTOR] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}")

# --- Database Utility ---
def get_tool_db_connection():
    """Establishes a connection to the SQLite database."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row # Access columns by name
        conn.execute("PRAGMA foreign_keys = ON;")
        _tool_log(f"Successfully connected to database: {DB_PATH}")
        return conn
    except sqlite3.Error as e:
        _tool_log(f"CRITICAL_ERROR: Failed to connect to database {DB_PATH}: {e}")
        return None

def _load_kb_content(file_path: str) -> str:
    try:
        if not os.path.exists(KB_FOLDER_PATH):
            _tool_log(f"ERROR: Knowledge base directory not found: {KB_FOLDER_PATH}")
            return f"Error: KB_DIRECTORY_MISSING"
        if not os.path.exists(file_path):
            _tool_log(f"ERROR: Knowledge base file not found: {file_path}")
            return f"Error: KB_FILE_NOT_FOUND ({os.path.basename(file_path)})"
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        _tool_log(f"ERROR: Could not read KB file {file_path}: {e}")
        return f"Error: KB_READ_ERROR ({os.path.basename(file_path)})"

# --- Email Sending Logic (execute_send_email) ---
def execute_send_email(subject: str, body_text: str, html_body_content: str, config: dict, is_ticket_format: bool = False) -> tuple[bool, str]:
    _tool_log(f"Attempting to send email. Subject: '{subject}'")
    api_key = config.get("RESEND_API_KEY")
    from_email = config.get("DEFAULT_FROM_EMAIL")
    to_emails_str = config.get("RESEND_RECIPIENT_EMAILS")
    bcc_emails_str = config.get("RESEND_RECIPIENT_EMAILS_BCC")
    api_url = config.get("RESEND_API_URL")

    if not all([api_key, from_email, to_emails_str, api_url]) or api_key == "YOUR_ACTUAL_RESEND_API_TOKEN_HERE":
        error_msg = "Email service configuration is incomplete (missing API key, from/to email, or API URL)."
        _tool_log(f"ERROR: {error_msg}")
        return False, f"Error: Email service is not properly configured by the administrator. ({error_msg})"

    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    ai_disclaimer = "<p style='margin-top: 20px; padding-top: 10px; border-top: 1px solid #ccc; color: #666; font-size: 0.9em;'>This message was generated with the assistance of an AI voice assistant. Please verify any important information.</p>"
    final_html_body = f"<div>{html_body_content}</div>{ai_disclaimer}"
    to_list = [email.strip() for email in to_emails_str.split(',') if email.strip()]
    bcc_list = [email.strip() for email in bcc_emails_str.split(',') if email.strip()] if bcc_emails_str else []

    if not to_list:
        _tool_log(f"ERROR: No valid 'to' recipients after stripping. Original: {to_emails_str}")
        return False, "Error: Email configuration is missing a valid primary recipient."

    payload = {"from": from_email, "to": to_list, "bcc": bcc_list, "subject": subject, "text": body_text, "html": final_html_body}
    try:
        response = requests.post(api_url, headers=headers, json=payload, timeout=15)
        if 200 <= response.status_code < 300:
            _tool_log(f"Email sent successfully via Resend.")
            return True, "Email has been sent successfully." if not is_ticket_format else "Ticket has been successfully raised."
        else:
            error_detail = response.text
            try: error_json = response.json(); error_detail = error_json.get("message", error_json.get("error",{}).get("message", str(error_json)))
            except: pass
            error_msg = f"Failed to send email. Status: {response.status_code}. Detail: {error_detail}"
            _tool_log(f"ERROR: {error_msg}")
            return False, f"Error: Email could not be sent (Code: RESEND-{response.status_code}). Detail: {error_detail[:200]}"
    except requests.exceptions.RequestException as e:
        _tool_log(f"ERROR: Email sending failed due to network/request error: {e}"); return False, "Error: Email could not be sent due to a network issue."
    except Exception as e:
        _tool_log(f"ERROR: An unexpected error occurred during email sending: {e}"); return False, "Error: An unexpected issue occurred while trying to send the email."

# --- Tool Handler Functions ---

def handle_send_email_discussion_summary(subject: str, body_summary: str, config: dict) -> str:
    _tool_log(f"Handling send_email_discussion_summary. Subject: {subject}")
    formatted_body = body_summary.replace('\\n', '<br>').replace('\n', '<br>')
    html_content = f"<h2>Discussion Summary</h2><p>{formatted_body}</p>"
    success, message = execute_send_email(subject, body_summary, html_content, config, is_ticket_format=False)
    return message

def handle_raise_ticket_for_missing_knowledge(user_query: str, additional_context: str = "", config: dict = None) -> str:
    if config is None: return "Error: Tool configuration missing for raising ticket."
    _tool_log(f"Handling raise_ticket_for_missing_knowledge. Query: {user_query}")
    subject = f"AI Ticket: Missing Knowledge - \"{user_query[:50]}...\""
    body_text = f"User Query for Missing Knowledge:\n{user_query}\n\nAdditional Context:\n{additional_context if additional_context else 'N/A'}\nGenerated by AI Assistant."
    formatted_user_query = user_query.replace('\\n', '<br>').replace('\n', '<br>')
    formatted_additional_context = (additional_context or 'N/A').replace('\\n', '<br>').replace('\n', '<br>')
    html_content = f"<h2>Missing Knowledge Ticket (AI Generated)</h2><p><strong>Query:</strong><br>{formatted_user_query}</p><p><strong>Context:</strong><br>{formatted_additional_context}</p>"
    ticket_recipient_str = config.get("TICKET_EMAIL") or config.get("RESEND_RECIPIENT_EMAILS")
    if not ticket_recipient_str: return "Error: Ticket email recipient not configured."
    ticket_config = config.copy(); ticket_config["RESEND_RECIPIENT_EMAILS"] = ticket_recipient_str
    success, message = execute_send_email(subject, body_text, html_content, ticket_config, is_ticket_format=True)
    return message

def handle_get_bolt_knowledge_base_info(query_topic: str, config: dict) -> str:
    _tool_log(f"Handling get_bolt_knowledge_base_info. Query Topic: '{query_topic}'")
    kb_content_full = _load_kb_content(BOLT_KB_FILE)
    if kb_content_full.startswith("Error:"): return kb_content_full
    return extract_relevant_sections(kb_full_text=kb_content_full, query_topic=query_topic, kb_name="Bolt")

def handle_get_dtc_knowledge_base_info(query_topic: str, config: dict) -> str:
    _tool_log(f"Handling get_dtc_knowledge_base_info. Query Topic: '{query_topic}'")
    kb_content_full = _load_kb_content(DTC_KB_FILE)
    if kb_content_full.startswith("Error:"): return kb_content_full
    return extract_relevant_sections(kb_full_text=kb_content_full, query_topic=query_topic, kb_name="DTC")

def handle_display_on_interface(display_type: str, data: dict, config: dict, title: str = None) -> str:
    _tool_log(f"Handling display_on_interface. Type: {display_type}, Title: {title}")
    fastapi_url = config.get("FASTAPI_DISPLAY_API_URL")
    if not fastapi_url: return "Error: Display interface URL is not configured."
    # Basic validation (can be expanded)
    if display_type == "markdown" and ("content" not in data or not isinstance(data.get("content"), str)):
        return "Error: Invalid data for markdown: 'content' string missing/invalid."
    elif display_type in ["graph_bar", "graph_line", "graph_pie"] and (not isinstance(data.get("labels"), list) or not isinstance(data.get("datasets"), list) or not data.get("datasets")):
        return "Error: Invalid data for graph: 'labels' or 'datasets' missing/invalid or 'datasets' is empty."

    payload_to_send = {"type": display_type, "payload": {**(data if isinstance(data, dict) else {})}}
    if title: payload_to_send["payload"]["title"] = title
    try:
        response = requests.post(fastapi_url, json=payload_to_send, timeout=7)
        response.raise_for_status(); response_data = response.json()
        status = response_data.get("status", "unknown"); message = response_data.get("message", "No message.")
        if status == "success": return f"Content sent to display. Server: {message}"
        if status == "received_but_no_clients": return f"Attempted display, but no visual interface connected. Server: {message}"
        return f"Display interface issue: {message} (Status: {status})"
    except requests.exceptions.RequestException as e:
        _tool_log(f"Error sending to display: {e}"); return f"Error connecting to display: {str(e)[:100]}"
    except Exception as e:
        _tool_log(f"Unexpected error in display handler: {e}"); return "Unexpected error displaying content."

def handle_get_taxi_ideas_for_today(current_date: str, config: dict, specific_focus: str = None) -> str:
    _tool_log(f"Handling get_taxi_ideas_for_today. Date: {current_date}, Focus: {specific_focus}")
    if not GOOGLE_SERVICES_AVAILABLE: return "Error: Google AI services are not available for taxi ideas."
    system_instruction_for_taxi_ideas = f"You are an AI assistant for Dubai Taxi Corporation (DTC). Find actionable ideas, news, and events for taxi services in Dubai for {current_date}. Consider Khaleej Times or local news. If no specific business-impacting ideas are found for {current_date}, respond with: 'No new business ideas found for today, {current_date}, based on current information.' Only provide info for {current_date}."
    user_prompt_for_gemini = f"Analyze information for Dubai for today, {current_date}, and provide actionable taxi service ideas or relevant event information."
    if specific_focus: user_prompt_for_gemini += f" Pay special attention to: {specific_focus}."
    return get_gemini_response(user_prompt_text=user_prompt_for_gemini, system_instruction_text=system_instruction_for_taxi_ideas, use_google_search_tool=True)

def handle_general_google_search(search_query: str, config: dict) -> str:
    _tool_log(f"Handling general_google_search. Query: '{search_query}'")
    if not GOOGLE_SERVICES_AVAILABLE: return "Error: Google AI services are not available for general search."
    system_instruction_for_general_search = "You are an AI assistant for a Dubai Taxi Corporation (DTC) employee. Answer the user's query based ONLY on Google Search results. Be factual and concise. Context is Dubai-related, professional. If no clear answer, state that. Prioritize reputable sources. Give direct answer."
    return get_gemini_response(user_prompt_text=search_query, system_instruction_text=system_instruction_for_general_search, use_google_search_tool=True)

# --- New Tool Handlers for Phase 1 ---

def handle_schedule_outbound_call(phone_number: str, contact_name: str, call_objective: str, config: dict) -> str:
    _tool_log(f"Handling schedule_outbound_call. To: {contact_name} ({phone_number}). Objective: {call_objective[:70]}...")
    conn = get_tool_db_connection()
    if not conn:
        return "Error: Could not connect to the scheduling database. Please try again later."

    try:
        cursor = conn.cursor()
        insert_sql = """
            INSERT INTO scheduled_calls 
            (phone_number, contact_name, initial_call_objective_description, current_call_objective_description, overall_status, max_retries, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'PENDING', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
        # Using initial and current objective same at creation
        params = (phone_number, contact_name, call_objective, call_objective, DEFAULT_MAX_RETRIES)
        cursor.execute(insert_sql, params)
        conn.commit()
        job_id = cursor.lastrowid
        _tool_log(f"Successfully scheduled call. Job ID: {job_id}, To: {contact_name}, Objective: {call_objective[:50]}...")
        objective_snippet = call_objective[:30] + "..." if len(call_objective) > 30 else call_objective
        return f"Okay, I've scheduled the call to {contact_name} regarding '{objective_snippet}'. The Job ID is {job_id}. I will provide updates as they become available or when the task is complete."
    except sqlite3.Error as e:
        _tool_log(f"ERROR: Database error while scheduling call: {e}")
        return f"Error: A database error occurred while trying to schedule the call: {e}"
    except Exception as e:
        _tool_log(f"ERROR: Unexpected error in handle_schedule_outbound_call: {e}")
        return "Error: An unexpected error occurred while scheduling the call."
    finally:
        if conn:
            conn.close()

def handle_check_scheduled_call_status(
    config: dict, 
    contact_name: str = None, 
    call_objective_snippet: str = None,
    date_reference: str = None,
    time_of_day_preference: str = "any", # Default to "any"
    job_id: int = None
) -> str:
    _tool_log(f"Handling check_scheduled_call_status. Job ID: {job_id}, Contact: {contact_name}, Objective: {call_objective_snippet}, DateRef: {date_reference}, TimePref: {time_of_day_preference}")
    conn = get_tool_db_connection()
    if not conn:
        return "Error: Could not connect to the scheduling database to check status."

    query_parts = []
    params = []
    order_by_clauses = ["updated_at DESC"] # Default sort

    if job_id is not None:
        query_parts.append("id = ?")
        params.append(job_id)
    if contact_name:
        query_parts.append("contact_name LIKE ?")
        params.append(f"%{contact_name}%")
    if call_objective_snippet:
        query_parts.append("(initial_call_objective_description LIKE ? OR current_call_objective_description LIKE ?)")
        params.append(f"%{call_objective_snippet}%")
        params.append(f"%{call_objective_snippet}%")

    # Date/Time Reference Parsing Logic
    # This will be a bit complex and might need refinement based on typical user inputs.
    # We'll target the 'created_at' or 'updated_at' columns. Let's use 'updated_at' as it reflects last action.
    if date_reference:
        try:
            target_date = None
            date_start_dt = None
            date_end_dt = None
            
            today = date.today()
            now = datetime.now()

            if date_reference.lower() == "today":
                target_date = today
            elif date_reference.lower() == "yesterday":
                target_date = today - timedelta(days=1)
            elif "days back" in date_reference.lower() or "days ago" in date_reference.lower():
                try:
                    num_days = int(date_reference.lower().split()[0])
                    target_date = today - timedelta(days=num_days)
                except ValueError:
                    _tool_log(f"Could not parse number of days from '{date_reference}'")
            elif date_reference.lower() in ["last call", "most recent"]:
                # This is handled by default sorting, but we can acknowledge it.
                # No specific date filter, but ensure `order_by_clauses.insert(0, "updated_at DESC")` is effectively done.
                pass 
            else: # Try parsing as a specific date
                parsed_dt_obj = dateutil_parser.parse(date_reference, default=datetime(now.year, now.month, now.day))
                target_date = parsed_dt_obj.date()

            if target_date:
                # Define time ranges based on time_of_day_preference
                if time_of_day_preference == "morning": # e.g., 6 AM to 12 PM
                    date_start_dt = datetime.combine(target_date, time(6, 0, 0))
                    date_end_dt = datetime.combine(target_date, time(11, 59, 59))
                elif time_of_day_preference == "afternoon": # e.g., 12 PM to 6 PM
                    date_start_dt = datetime.combine(target_date, time(12, 0, 0))
                    date_end_dt = datetime.combine(target_date, time(17, 59, 59))
                elif time_of_day_preference == "evening": # e.g., 6 PM to 11:59 PM
                    date_start_dt = datetime.combine(target_date, time(18, 0, 0))
                    date_end_dt = datetime.combine(target_date, time(23, 59, 59))
                else: # "any" time of day or default
                    date_start_dt = datetime.combine(target_date, time.min)
                    date_end_dt = datetime.combine(target_date, time.max)
                
                query_parts.append("updated_at BETWEEN ? AND ?") # Or created_at, depending on desired meaning
                params.append(date_start_dt.strftime('%Y-%m-%d %H:%M:%S'))
                params.append(date_end_dt.strftime('%Y-%m-%d %H:%M:%S'))
                _tool_log(f"Date filter: updated_at between {date_start_dt} and {date_end_dt}")
        
        except Exception as e_date:
            _tool_log(f"Could not parse date_reference '{date_reference}': {e_date}. Ignoring date filter.")
            # Optionally, inform LLM that date parsing failed. For now, just ignore.

    if not query_parts:
        # If still no filters, default to most recent N calls (e.g., last 3 updated)
        _tool_log("No specific query parameters provided, fetching most recent calls.")
        # order_by_clauses is already updated_at DESC
    
    # Construct the final query
    base_query = "SELECT id, contact_name, overall_status, initial_call_objective_description, current_call_objective_description, final_summary_for_main_agent, retries_attempted, max_retries, strftime('%Y-%m-%d %H:%M', next_retry_at) as next_retry_at_formatted, strftime('%Y-%m-%d %H:%M', updated_at) as last_updated_formatted FROM scheduled_calls"
    
    where_clause = ""
    if query_parts:
        where_clause = f"WHERE {' AND '.join(query_parts)}"
        
    order_by_sql = "ORDER BY " + ", ".join(list(set(order_by_clauses))) # Use set to avoid duplicate sort keys if "last call" added it
    
    # Limit results
    limit_sql = "LIMIT 5" 
    if date_reference and date_reference.lower() in ["last call", "most recent"] and not query_parts: # Only date_ref is "last call"
        limit_sql = "LIMIT 1"

    full_query = f"{base_query} {where_clause} {order_by_sql} {limit_sql}"

    try:
        cursor = conn.cursor()
        _tool_log(f"Executing status check query: {full_query} with params: {params}")
        cursor.execute(full_query, tuple(params))
        jobs = cursor.fetchall()

        if not jobs:
            return "I couldn't find any scheduled calls matching your criteria."

        results = []
        for job_row in jobs:
            job = dict(job_row) # Convert Row to dict
            status_msg = f"Call to {job.get('contact_name', 'N/A')} (ID: {job['id']}) regarding '{job.get('current_call_objective_description', job.get('initial_call_objective_description', 'N/A'))[:50]}...' (Last updated: {job.get('last_updated_formatted', 'N/A')}): "
            
            status = job.get('overall_status', 'UNKNOWN')
            if status == 'PENDING':
                status_msg += "This call is scheduled and awaiting processing."
            # ... (other status formatting from previous version) ...
            elif status == 'RETRY_SCHEDULED':
                status_msg += f"A retry for this call is scheduled for around {job.get('next_retry_at_formatted', 'soon')}."
                # Fetching reason from call_attempts can be added here if complex joins are acceptable or via a sub-query for each job.
            elif status in ['COMPLETED_SUCCESS', 'FAILED_MAX_RETRIES', 'COMPLETED_OBJECTIVE_NOT_MET', 'FAILED_PERMANENT_ERROR']:
                final_summary = job.get('final_summary_for_main_agent', 'No final summary recorded.')
                status_msg += f"This call has concluded. Status: {status}. Outcome: {final_summary}"
            else:
                status_msg += f"The call has an unknown status: {status}."
            results.append(status_msg)
        
        if len(results) == 1:
            return results[0]
        else:
            response = f"Found {len(results)} calls matching your criteria:\n" + "\n".join([f"- {res}" for res in results])
            return response

    except sqlite3.Error as e:
        _tool_log(f"ERROR: Database error while checking call status: {e}")
        return f"Error: A database error occurred: {e}"
    except Exception as e:
        _tool_log(f"ERROR: Unexpected error in handle_check_scheduled_call_status: {e}")
        return "Error: An unexpected error occurred."
    finally:
        if conn:
            conn.close()


# Dispatch dictionary to map function names to handler functions
TOOL_HANDLERS = {
    SEND_EMAIL_SUMMARY_TOOL_NAME: handle_send_email_discussion_summary,
    RAISE_TICKET_TOOL_NAME: handle_raise_ticket_for_missing_knowledge,
    GET_BOLT_KB_TOOL_NAME: handle_get_bolt_knowledge_base_info,
    GET_DTC_KB_TOOL_NAME: handle_get_dtc_knowledge_base_info,
    DISPLAY_ON_INTERFACE_TOOL_NAME: handle_display_on_interface,
    GET_TAXI_IDEAS_FOR_TODAY_TOOL_NAME: handle_get_taxi_ideas_for_today,
    GENERAL_GOOGLE_SEARCH_TOOL_NAME: handle_general_google_search,
    # Add new handlers for Phase 1
    SCHEDULE_OUTBOUND_CALL_TOOL_NAME: handle_schedule_outbound_call,
    CHECK_SCHEDULED_CALL_STATUS_TOOL_NAME: handle_check_scheduled_call_status,
}