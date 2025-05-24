# tool_executor.py
import json
import os
import requests # For synchronous HTTP requests
from datetime import datetime

# Import tool names from tools_definition
from tools_definition import (
    SEND_EMAIL_SUMMARY_TOOL_NAME,
    RAISE_TICKET_TOOL_NAME,
    GET_BOLT_KB_TOOL_NAME,
    GET_DTC_KB_TOOL_NAME,
    DISPLAY_ON_INTERFACE_TOOL_NAME
)

# Import the new KB extraction function from kb_llm_extractor.py
from kb_llm_extractor import extract_relevant_sections

# --- Knowledge Base File Paths ---
KB_FOLDER_PATH = os.path.join(os.path.dirname(__file__), "knowledge_bases")
BOLT_KB_FILE = os.path.join(KB_FOLDER_PATH, "bolt_kb.txt")
DTC_KB_FILE = os.path.join(KB_FOLDER_PATH, "dtc_kb.txt")

# Helper for logging within this module
def _tool_log(message):
    print(f"[TOOL_EXECUTOR] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}")

def _load_kb_content(file_path: str) -> str:
    """
    Loads content from a knowledge base file.
    Returns the content as a string, or an error string if loading fails.
    """
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
        _tool_log(f"Sending Resend request. URL: {api_url}, From: {payload['from']}, To: {payload['to']}, Subject: {payload['subject']}")
        response = requests.post(api_url, headers=headers, json=payload, timeout=15)
        _tool_log(f"Resend raw response status: {response.status_code}, content (first 300 chars): {response.text[:300]}")

        if 200 <= response.status_code < 300:
            resend_id = "N/A"
            try: 
                resend_id = response.json().get('id', 'N/A')
            except json.JSONDecodeError: 
                _tool_log("WARN: Response from Resend was not JSON when trying to get 'id'.")
            _tool_log(f"Email sent successfully via Resend. Response ID (Resend): {resend_id}")
            return True, "Email has been sent successfully." if not is_ticket_format else "Ticket has been successfully raised."
        else: 
            error_detail = response.text 
            try: 
                error_json = response.json()
                if "message" in error_json: 
                    error_detail = error_json["message"]
                elif "error" in error_json and isinstance(error_json["error"], dict) and "message" in error_json["error"]: 
                    error_detail = error_json["error"]["message"]
                elif isinstance(error_json, dict) and "name" in error_json and "message" in error_json: 
                    error_detail = f"{error_json['name']}: {error_json['message']}"
            except json.JSONDecodeError:
                _tool_log(f"WARN: Could not parse error response from Resend as JSON. Raw text: {response.text[:200]}")
            
            error_msg = f"Failed to send email. Status: {response.status_code}. Detail: {error_detail}"
            _tool_log(f"ERROR: {error_msg}")
            return False, f"Error: Email could not be sent (Code: RESEND-{response.status_code}). Detail: {error_detail[:200]}"
    except requests.exceptions.Timeout:
        _tool_log("ERROR: Email sending failed due to a timeout."); return False, "Error: Email could not be sent due to a network timeout (Code: REQ-TIMEOUT)."
    except requests.exceptions.RequestException as e:
        _tool_log(f"ERROR: Email sending failed due to network/request error: {e}"); return False, "Error: Email could not be sent due to a network issue (Code: REQ-ERR)."
    except Exception as e:
        _tool_log(f"ERROR: An unexpected error occurred during email sending: {e}"); return False, "Error: An unexpected issue occurred while trying to send the email (Code: UNXPTD-ERR)."

# --- Tool Handler Functions ---

def handle_send_email_discussion_summary(subject: str, body_summary: str, config: dict) -> str:
    _tool_log(f"Handling send_email_discussion_summary. Subject: {subject}")
    
    # Perform replacements on body_summary first to avoid f-string syntax error
    formatted_body = body_summary.replace('\\n', '<br>') # Handles literal \\n
    formatted_body = formatted_body.replace('\n', '<br>')   # Handles actual newlines
    
    # Use the processed formatted_body in the f-string
    html_content = f"<h2>Discussion Summary</h2><p>{formatted_body}</p>"
    
    success, message = execute_send_email(subject, body_summary, html_content, config, is_ticket_format=False) # Pass original body_summary for plain text part
    return message

def handle_raise_ticket_for_missing_knowledge(user_query: str, additional_context: str = "", config: dict = None) -> str:
    if config is None:
        _tool_log("ERROR: Config dictionary not provided to handle_raise_ticket_for_missing_knowledge.")
        return "Error: Tool configuration missing for raising ticket."

    _tool_log(f"Handling raise_ticket_for_missing_knowledge. Query: {user_query}")
    subject = f"AI Ticket: Missing Knowledge - \"{user_query[:50]}...\""
    
    # Original body_text for plain text part of email
    body_text = f"User Query for Missing Knowledge:\n{user_query}\n\nAdditional Context:\n{additional_context if additional_context else 'N/A'}\nGenerated by AI Assistant."

    # Format for HTML part
    formatted_user_query = user_query.replace('\\n', '<br>').replace('\n', '<br>')
    formatted_additional_context = (additional_context or 'N/A').replace('\\n', '<br>').replace('\n', '<br>')
    
    html_content = f"<h2>Missing Knowledge Ticket (AI Generated)</h2><p><strong>Query:</strong><br>{formatted_user_query}</p><p><strong>Context:</strong><br>{formatted_additional_context}</p>"
    
    ticket_recipient_str = config.get("TICKET_EMAIL") or config.get("RESEND_RECIPIENT_EMAILS")
    if not ticket_recipient_str:
        _tool_log("ERROR: TICKET_EMAIL or RESEND_RECIPIENT_EMAILS not configured for raising ticket.")
        return "Error: Ticket email recipient not configured."
        
    ticket_config = config.copy()
    ticket_config["RESEND_RECIPIENT_EMAILS"] = ticket_recipient_str

    success, message = execute_send_email(subject, body_text, html_content, ticket_config, is_ticket_format=True)
    return message

# --- KB Handlers using kb_llm_extractor ---
def handle_get_bolt_knowledge_base_info(query_topic: str, config: dict) -> str:
    _tool_log(f"Handling get_bolt_knowledge_base_info. Query Topic: '{query_topic}'")
    kb_content_full = _load_kb_content(BOLT_KB_FILE)
    if kb_content_full.startswith("Error:"): 
        return kb_content_full 
    return extract_relevant_sections(kb_full_text=kb_content_full, query_topic=query_topic, kb_name="Bolt")

def handle_get_dtc_knowledge_base_info(query_topic: str, config: dict) -> str:
    _tool_log(f"Handling get_dtc_knowledge_base_info. Query Topic: '{query_topic}'")
    kb_content_full = _load_kb_content(DTC_KB_FILE)
    if kb_content_full.startswith("Error:"):
        return kb_content_full
    return extract_relevant_sections(kb_full_text=kb_content_full, query_topic=query_topic, kb_name="DTC")


# --- Display Tool Handler ---
def handle_display_on_interface(display_type: str, data: dict, config: dict, title: str = None) -> str:
    _tool_log(f"Handling display_on_interface. Type: {display_type}, Title: {title}")

    fastapi_url = config.get("FASTAPI_DISPLAY_API_URL")
    if not fastapi_url:
        _tool_log("ERROR: FASTAPI_DISPLAY_API_URL not configured.")
        return "Error: Display interface URL is not configured."

    # Basic validation
    if display_type == "markdown":
        if "content" not in data or not isinstance(data.get("content"), str):
            return "Error: Invalid data for markdown: 'content' string missing/invalid."
    elif display_type in ["graph_bar", "graph_line", "graph_pie"]:
        if not isinstance(data.get("labels"), list) or not isinstance(data.get("datasets"), list) or not data.get("datasets"):
            return "Error: Invalid data for graph: 'labels' or 'datasets' missing/invalid or 'datasets' is empty."
        for i, dataset in enumerate(data["datasets"]): 
            if not isinstance(dataset, dict) or not isinstance(dataset.get("values"), list):
                return f"Error: Invalid dataset '{dataset.get('label', i) if isinstance(dataset, dict) else i}' for graph: 'values' must be a list."
    else:
        return f"Error: Unknown display type '{display_type}'."

    payload_to_send = {"type": display_type, "payload": {**(data if isinstance(data, dict) else {})}}
    if title: payload_to_send["payload"]["title"] = title
    
    _tool_log(f"Sending to FastAPI: {fastapi_url}, Payload: {json.dumps(payload_to_send, indent=2)}")
    try:
        response = requests.post(fastapi_url, json=payload_to_send, timeout=7)
        response.raise_for_status()
        response_data = response.json()
        status = response_data.get("status", "unknown_status")
        message = response_data.get("message", "No message from display server.")
        if status == "success": return f"Content sent to display. Server: {message}"
        elif status == "received_but_no_clients": return f"Attempted display, but no visual interface connected. Server: {message}"
        return f"Display interface issue: {message} (Status: {status})"
    except requests.exceptions.HTTPError as e:
        err_text = e.response.text if e.response else "N/A"
        _tool_log(f"HTTPError display: {e}. Resp: {err_text[:200]}")
        return f"Error: Display server error (HTTP {e.response.status_code}). {err_text[:100]}"
    except requests.exceptions.ConnectionError:
        _tool_log(f"ConnectionError to display: {fastapi_url}")
        return "Error: Could not connect to display interface."
    except requests.exceptions.Timeout:
        _tool_log("Timeout to display."); return "Error: Display interface request timed out."
    except requests.exceptions.RequestException as e:
        _tool_log(f"RequestException to display: {e}"); return f"Error sending to display: {str(e)[:100]}"
    except json.JSONDecodeError: 
        raw_resp_text = response.text if 'response' in locals() and hasattr(response, 'text') else "N/A"
        _tool_log(f"Invalid JSON response from FastAPI. Raw: {raw_resp_text[:200]}")
        return "Error: Invalid response format from display interface."
    except Exception as e:
        _tool_log(f"Unexpected error in display handler: {e}"); return "Unexpected error displaying content."


# Dispatch dictionary to map function names to handler functions
TOOL_HANDLERS = {
    SEND_EMAIL_SUMMARY_TOOL_NAME: handle_send_email_discussion_summary,
    RAISE_TICKET_TOOL_NAME: handle_raise_ticket_for_missing_knowledge,
    GET_BOLT_KB_TOOL_NAME: handle_get_bolt_knowledge_base_info,
    GET_DTC_KB_TOOL_NAME: handle_get_dtc_knowledge_base_info,
    DISPLAY_ON_INTERFACE_TOOL_NAME: handle_display_on_interface,
}