# tool_executor.py
import json
import os
import requests # For synchronous HTTP requests to Resend
from datetime import datetime

# --- Import Tool Name Constants ---
from tools_definition import (
    SEND_EMAIL_SUMMARY_TOOL_NAME,
    RAISE_TICKET_TOOL_NAME,
    GET_BOLT_KB_TOOL_NAME,
    GET_DTC_KB_TOOL_NAME,
    END_CONVERSATION_TOOL_NAME # Import for completeness, though not used as a key here
)
# --- End of Added Import Section ---

# --- Knowledge Base File Paths ---
# Assumes 'knowledge_bases' is a subfolder in the same directory as this script
KB_FOLDER_PATH = os.path.join(os.path.dirname(__file__), "knowledge_bases")
BOLT_KB_FILE = os.path.join(KB_FOLDER_PATH, "bolt_kb.txt")
DTC_KB_FILE = os.path.join(KB_FOLDER_PATH, "dtc_kb.txt")

# Helper for logging within this module
def _tool_log(message):
    print(f"[TOOL_EXECUTOR] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}")


def _load_kb_content(file_path: str) -> str:
    try:
        # Ensure the knowledge_bases directory exists before trying to read from it
        if not os.path.exists(KB_FOLDER_PATH):
            _tool_log(f"ERROR: Knowledge base directory not found: {KB_FOLDER_PATH}")
            return f"Error: The knowledge base directory is missing."
        if not os.path.exists(file_path):
            _tool_log(f"ERROR: Knowledge base file not found: {file_path}")
            return f"Error: The knowledge base file ({os.path.basename(file_path)}) is currently unavailable or not found."
            
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        _tool_log(f"ERROR: Could not read KB file {file_path}: {e}")
        return f"Error: Could not access the knowledge base content for {os.path.basename(file_path)} due to an internal issue."

# --- Email Sending Logic ---
def execute_send_email(subject: str, body_text: str, html_body_content: str, config: dict, is_ticket_format: bool = False) -> tuple[bool, str]:
    """
    Sends an email using the Resend API.
    Returns a tuple: (success_boolean, message_string).
    'config' dict should contain: RESEND_API_KEY, DEFAULT_FROM_EMAIL,
                                RESEND_RECIPIENT_EMAILS, RESEND_RECIPIENT_EMAILS_BCC,
                                RESEND_API_URL
    """
    _tool_log(f"Attempting to send email. Subject: '{subject}'")

    api_key = config.get("RESEND_API_KEY")
    from_email = config.get("DEFAULT_FROM_EMAIL")
    to_emails = config.get("RESEND_RECIPIENT_EMAILS")
    bcc_emails = config.get("RESEND_RECIPIENT_EMAILS_BCC")
    api_url = config.get("RESEND_API_URL")

    if not all([api_key, from_email, to_emails, api_url]) or api_key == "YOUR_ACTUAL_RESEND_API_TOKEN_HERE":
        error_msg = "Email service configuration is incomplete (missing API key, from/to email, or API URL)."
        _tool_log(f"ERROR: {error_msg}")
        return False, f"Error: Email service is not properly configured by the administrator. ({error_msg})"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    ai_disclaimer = "<p style='margin-top: 20px; padding-top: 10px; border-top: 1px solid #ccc; color: #666; font-size: 0.9em;'>This message was generated with the assistance of an AI voice assistant. Please verify any important information.</p>"
    final_html_body = f"<div>{html_body_content}</div>{ai_disclaimer}"

    payload = {
        "from": from_email,
        "to": to_emails.split(','), # Resend expects 'to' and 'bcc' to be arrays of strings
        "bcc": bcc_emails.split(',') if bcc_emails else [],
        "subject": subject,
        "text": body_text, 
        "html": final_html_body
    }
    # Filter out empty strings from email lists
    payload["to"] = [email.strip() for email in payload["to"] if email.strip()]
    payload["bcc"] = [email.strip() for email in payload["bcc"] if email.strip()]
    if not payload["to"]: # Must have at least one 'to' recipient
        _tool_log(f"ERROR: No valid 'to' recipients after stripping. Original: {to_emails}")
        return False, "Error: Email configuration is missing a valid primary recipient."


    try:
        _tool_log(f"Sending Resend request. URL: {api_url}, From: {payload['from']}, To: {payload['to']}, Subject: {payload['subject']}")
        # _tool_log(f"Full Payload (excluding potentially sensitive headers): {json.dumps(payload, indent=2)}")
        response = requests.post(api_url, headers=headers, json=payload, timeout=15) # Increased timeout
        _tool_log(f"Resend raw response status: {response.status_code}, content (first 300 chars): {response.text[:300]}")

        if 200 <= response.status_code < 300:
            _tool_log(f"Email sent successfully via Resend. Response ID (Resend): {response.json().get('id', 'N/A')}")
            if is_ticket_format:
                return True, "Ticket has been successfully raised. The admin team will review the request."
            else:
                return True, "Email has been sent successfully."
        else:
            error_detail = response.text
            try: 
                error_json = response.json()
                # Resend error structure: { "name": "validation_error", "message": "...", "statusCode": 422 }
                # or { "error": { "message": "...", "type": "..." } } for some auth errors
                if "message" in error_json:
                    error_detail = error_json["message"]
                elif "error" in error_json and "message" in error_json["error"]:
                    error_detail = error_json["error"]["message"]
            except ValueError:
                pass 
            
            error_msg = f"Failed to send email. Status: {response.status_code}. Detail: {error_detail}"
            _tool_log(f"ERROR: {error_msg}")
            return False, f"Error: Email could not be sent (Code: RESEND-{response.status_code}). Detail: {error_detail}"

    except requests.exceptions.Timeout:
        error_msg = "Email sending failed due to a timeout connecting to the email service."
        _tool_log(f"ERROR: {error_msg}")
        return False, f"Error: Email could not be sent due to a network timeout (Code: REQ-TIMEOUT)."
    except requests.exceptions.RequestException as e:
        error_msg = f"Email sending failed due to a network or request error: {e}"
        _tool_log(f"ERROR: {error_msg}")
        return False, f"Error: Email could not be sent due to a network issue (Code: REQ-ERR)."
    except Exception as e: 
        error_msg = f"An unexpected error occurred during email sending: {e}"
        _tool_log(f"ERROR: {error_msg}")
        return False, f"Error: An unexpected issue occurred while trying to send the email (Code: UNXPTD-ERR)."


# --- Tool Handler Functions ---

def handle_send_email_discussion_summary(subject: str, body_summary: str, config: dict) -> str:
    _tool_log(f"Handling send_email_discussion_summary. Subject: {subject}")
    html_content = f"<h2>Discussion Summary</h2><p>{body_summary.replace(r'\\n', '<br>').replace('\n', '<br>')}</p>"
    success, message = execute_send_email(subject, body_summary, html_content, config, is_ticket_format=False)
    return message 

def handle_raise_ticket_for_missing_knowledge(user_query: str, additional_context: str = "", config: dict = None) -> str:
    # Added default for additional_context and config for direct testing if needed
    if config is None: # Should be passed by openai_client
        _tool_log("ERROR: Config dictionary not provided to handle_raise_ticket_for_missing_knowledge.")
        return "Error: Tool configuration missing."

    _tool_log(f"Handling raise_ticket_for_missing_knowledge. Query: {user_query}")
    subject = f"AI Ticket: Missing Knowledge - \"{user_query[:50]}...\"" 
    
    body_text = f"User Query for Missing Knowledge:\n{user_query}\n\n"
    body_text += f"Additional Context from Conversation:\n{additional_context if additional_context else 'N/A'}\n\n"
    body_text += "Please investigate and update the knowledge base if appropriate. This ticket was generated by the AI Voice Assistant."

    html_content = f"<h2>Missing Knowledge Ticket (AI Generated)</h2>"
    html_content += f"<p><strong>User's Query:</strong><br>{user_query.replace(r'\\n', '<br>').replace('\n', '<br>')}</p>"
    html_content += f"<p><strong>Additional Context:</strong><br>{additional_context.replace(r'\\n', '<br>').replace('\n', '<br>') if additional_context else 'N/A'}</p>"
    html_content += "<p>This ticket was automatically generated based on a user interaction with the AI voice assistant.</p>"
    
    ticket_recipient = config.get("TICKET_EMAIL") or config.get("RESEND_RECIPIENT_EMAILS")
    if not ticket_recipient:
        _tool_log("ERROR: TICKET_EMAIL or RESEND_RECIPIENT_EMAILS not configured for raising ticket.")
        return "Error: Ticket email recipient not configured."
        
    ticket_config = config.copy() 
    ticket_config["RESEND_RECIPIENT_EMAILS"] = ticket_recipient # Send ticket to specific email

    success, message = execute_send_email(subject, body_text, html_content, ticket_config, is_ticket_format=True)
    return message

def _search_kb_content(kb_content: str, query_topic: str, kb_name: str) -> str:
    """Helper function to perform a simple search within KB content."""
    if "Error:" in kb_content: # Check if loading failed
        return kb_content
    
    # Simple keyword search (case-insensitive) as a basic filter
    # This is very rudimentary; a more sophisticated search/RAG would be better for large KBs.
    relevant_lines = []
    query_keywords = [kw.strip() for kw in query_topic.lower().split() if kw.strip()] # Get individual keywords
    
    if not query_keywords: # If query_topic was empty or just spaces
        _tool_log(f"Empty query topic for {kb_name} KB. Returning full KB (truncated).")
        return f"The full {kb_name} Knowledge Base content is (query was empty, showing summary):\n\n{kb_content[:2000]}..." # Limit size

    # First, try to find lines that contain ALL keywords
    for line in kb_content.splitlines():
        line_lower = line.lower()
        if all(keyword in line_lower for keyword in query_keywords):
            relevant_lines.append(line)
    
    # If no lines contain all keywords, try lines that contain ANY keyword
    if not relevant_lines:
        _tool_log(f"No lines contained all keywords for '{query_topic}' in {kb_name} KB. Trying ANY keyword match.")
        for line in kb_content.splitlines():
            line_lower = line.lower()
            if any(keyword in line_lower for keyword in query_keywords):
                relevant_lines.append(line)

    if relevant_lines:
        # Limit the number of relevant lines to avoid overly long responses
        max_lines = 20 
        result = f"Found the following information related to '{query_topic}' in the {kb_name} Knowledge Base:\n"
        result += "\n".join(relevant_lines[:max_lines])
        if len(relevant_lines) > max_lines:
            result += f"\n... (and {len(relevant_lines) - max_lines} more matching lines)"
        _tool_log(f"Found {len(relevant_lines)} relevant lines for {kb_name} query '{query_topic}'. Returning up to {max_lines}.")
    else: 
        result = f"Could not find specific information for '{query_topic}' in the {kb_name} Knowledge Base. You may need to infer from the full content if provided, or ask the user to rephrase. Full KB is too large to display."
        _tool_log(f"No specific information found for '{query_topic}' in {kb_name} KB.")
        # Optionally, return a small snippet of the KB or nothing
        # result += f"\n\nHere's a small part of the {kb_name} KB for general context:\n{kb_content[:500]}..."

    return result


def handle_get_bolt_knowledge_base_info(query_topic: str, config: dict = None) -> str: # Added config for consistency, though not used by this KB handler
    _tool_log(f"Handling get_bolt_knowledge_base_info. Query Topic: {query_topic}")
    kb_content = _load_kb_content(BOLT_KB_FILE)
    return _search_kb_content(kb_content, query_topic, "Bolt")

def handle_get_dtc_knowledge_base_info(query_topic: str, config: dict = None) -> str: # Added config for consistency
    _tool_log(f"Handling get_dtc_knowledge_base_info. Query Topic: {query_topic}")
    kb_content = _load_kb_content(DTC_KB_FILE)
    return _search_kb_content(kb_content, query_topic, "DTC")

# Dispatch dictionary to map function names to handler functions
TOOL_HANDLERS = {
    SEND_EMAIL_SUMMARY_TOOL_NAME: handle_send_email_discussion_summary,
    RAISE_TICKET_TOOL_NAME: handle_raise_ticket_for_missing_knowledge,
    GET_BOLT_KB_TOOL_NAME: handle_get_bolt_knowledge_base_info,
    GET_DTC_KB_TOOL_NAME: handle_get_dtc_knowledge_base_info,
}