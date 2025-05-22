# tool_executor.py
import json
import os
import requests # For synchronous HTTP requests to Resend
from datetime import datetime

# --- Configuration (will be passed from main.py or .env) ---
# These will be populated by the Config object passed to handlers
# RESEND_API_KEY = os.getenv("RESEND_API_KEY")
# DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL")
# RESEND_RECIPIENT_EMAILS = os.getenv("RESEND_RECIPIENT_EMAILS")
# RESEND_RECIPIENT_EMAILS_BCC = os.getenv("RESEND_RECIPIENT_EMAILS_BCC")
# TICKET_EMAIL = os.getenv("TICKET_EMAIL") # This is the same as RESEND_RECIPIENT_EMAILS in your .env
# RESEND_API_URL = os.getenv("RESEND_API_URL")

# --- Knowledge Base File Paths ---
BOLT_KB_FILE = os.path.join(os.path.dirname(__file__), "knowledge_bases", "bolt_kb.txt")
DTC_KB_FILE = os.path.join(os.path.dirname(__file__), "knowledge_bases", "dtc_kb.txt")

# Helper for logging within this module if needed, or use passed logger
def _tool_log(message):
    print(f"[TOOL_EXECUTOR] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}")


def _load_kb_content(file_path: str) -> str:
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
    except FileNotFoundError:
        _tool_log(f"ERROR: Knowledge base file not found: {file_path}")
        return f"Error: The knowledge base file ({os.path.basename(file_path)}) is currently unavailable."
    except Exception as e:
        _tool_log(f"ERROR: Could not read KB file {file_path}: {e}")
        return f"Error: Could not access the knowledge base content due to an internal issue."

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

    if not api_key or api_key == "YOUR_ACTUAL_RESEND_API_TOKEN_HERE" or not from_email or not to_emails or not api_url:
        error_msg = "Email service configuration is incomplete (missing API key, from/to email, or API URL)."
        _tool_log(f"ERROR: {error_msg}")
        return False, f"Error: Email service is not properly configured by the administrator. ({error_msg})"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }

    # Add AI-generated message disclaimer to HTML body
    ai_disclaimer = "<p style='margin-top: 20px; padding-top: 10px; border-top: 1px solid #ccc; color: #666; font-size: 0.9em;'>This message was generated with the assistance of an AI voice assistant. Please verify any important information.</p>"
    final_html_body = f"<div>{html_body_content}</div>{ai_disclaimer}"

    payload = {
        "from": from_email,
        "to": to_emails,
        "bcc": bcc_emails,
        "subject": subject,
        "text": body_text, # Plain text version
        "html": final_html_body
    }

    try:
        _tool_log(f"Sending Resend request. URL: {api_url}, Payload: {json.dumps(payload, indent=2)}")
        response = requests.post(api_url, headers=headers, json=payload, timeout=10) # Added timeout
        _tool_log(f"Resend raw response status: {response.status_code}, content (first 300 chars): {response.text[:300]}")

        if 200 <= response.status_code < 300:
            _tool_log(f"Email sent successfully via Resend. Status: {response.status_code}")
            if is_ticket_format:
                return True, "Ticket has been successfully raised. The admin team will review the request."
            else:
                return True, "Email has been sent successfully."
        else:
            error_detail = response.text
            try: # Try to parse JSON error from Resend
                error_json = response.json()
                error_detail = error_json.get("message", error_detail)
            except ValueError:
                pass # Keep original text if not JSON
            
            error_msg = f"Failed to send email. Status: {response.status_code}. Detail: {error_detail}"
            _tool_log(f"ERROR: {error_msg}")
            # Provide a generic error code for now, can be more specific
            return False, f"Error: Email could not be sent due to a technical issue (Code: RESEND-{response.status_code}). Please try again later or contact support."

    except requests.exceptions.RequestException as e:
        error_msg = f"Email sending failed due to a network or request error: {e}"
        _tool_log(f"ERROR: {error_msg}")
        return False, f"Error: Email could not be sent due to a network issue (Code: REQ-ERR). Please try again later."
    except Exception as e: # Catch any other unexpected errors
        error_msg = f"An unexpected error occurred during email sending: {e}"
        _tool_log(f"ERROR: {error_msg}")
        return False, f"Error: An unexpected issue occurred while trying to send the email (Code: UNXPTD-ERR)."


# --- Tool Handler Functions ---

def handle_send_email_discussion_summary(subject: str, body_summary: str, config: dict) -> str:
    _tool_log(f"Handling send_email_discussion_summary. Subject: {subject}")
    # LLM provides body_summary, we wrap it in basic HTML
    html_content = f"<h2>Discussion Summary</h2><p>{body_summary.replace(r'\\n', '<br>').replace('\n', '<br>')}</p>"
    success, message = execute_send_email(subject, body_summary, html_content, config, is_ticket_format=False)
    return message # Return message for LLM

def handle_raise_ticket_for_missing_knowledge(user_query: str, additional_context: str, config: dict) -> str:
    _tool_log(f"Handling raise_ticket_for_missing_knowledge. Query: {user_query}")
    subject = f"Ticket: Missing Knowledge Request - \"{user_query[:50]}...\"" # Truncate long queries for subject
    
    body_text = f"User Query for Missing Knowledge:\n{user_query}\n\n"
    body_text += f"Additional Context from Conversation:\n{additional_context if additional_context else 'N/A'}\n\n"
    body_text += "Please investigate and update the knowledge base if appropriate."

    html_content = f"<h2>Missing Knowledge Ticket</h2>"
    html_content += f"<p><strong>User's Query:</strong><br>{user_query.replace(r'\\n', '<br>').replace('\n', '<br>')}</p>"
    html_content += f"<p><strong>Additional Context:</strong><br>{additional_context.replace(r'\\n', '<br>').replace('\n', '<br>') if additional_context else 'N/A'}</p>"
    html_content += "<p>This ticket was automatically generated based on a user interaction with the AI voice assistant.</p>"
    
    # Use TICKET_EMAIL if defined and different, otherwise default to RESEND_RECIPIENT_EMAILS
    ticket_recipient = config.get("TICKET_EMAIL") or config.get("RESEND_RECIPIENT_EMAILS")
    ticket_config = config.copy() # Avoid modifying original config
    ticket_config["RESEND_RECIPIENT_EMAILS"] = ticket_recipient

    success, message = execute_send_email(subject, body_text, html_content, ticket_config, is_ticket_format=True)
    return message

def handle_get_bolt_knowledge_base_info(query_topic: str) -> str:
    _tool_log(f"Handling get_bolt_knowledge_base_info. Query Topic: {query_topic}")
    kb_content = _load_kb_content(BOLT_KB_FILE)
    # For now, we return the whole KB. A real implementation might search/filter.
    # We can tell the LLM that the full relevant KB is provided.
    if "Error:" in kb_content: # Check if loading failed
        return kb_content
    
    # Simple keyword search (case-insensitive) as a basic filter
    # This is very rudimentary; a more sophisticated search/RAG would be better for large KBs.
    relevant_sections = []
    query_keywords = query_topic.lower().split()
    current_section = ""
    title_line_found = False

    for line in kb_content.splitlines():
        # Try to identify sections by titles (e.g., lines in ALL CAPS or with '----')
        if line.isupper() and len(line) > 5 or "---" in line or "===" in line:
            if title_line_found and current_section: # End of previous section
                # Check if previous section is relevant
                if any(keyword in current_section.lower() for keyword in query_keywords):
                    relevant_sections.append(current_section.strip())
                elif query_topic.lower() in current_section.lower(): # Direct match in section
                     relevant_sections.append(current_section.strip())

            current_section = line + "\n" # Start new section
            title_line_found = True
        elif title_line_found: # If we are inside a section
            current_section += line + "\n"

    # Add the last section if it's relevant
    if title_line_found and current_section:
        if any(keyword in current_section.lower() for keyword in query_keywords) or \
           query_topic.lower() in current_section.lower():
            relevant_sections.append(current_section.strip())
            
    if relevant_sections:
        result = f"Found the following information related to '{query_topic}' in the Bolt Knowledge Base:\n\n"
        result += "\n\n---\n\n".join(relevant_sections)
        _tool_log(f"Found {len(relevant_sections)} relevant sections for Bolt query '{query_topic}'.")
    elif title_line_found : # KB was loaded but no specific section matched well
        result = f"Could not find specific information for '{query_topic}' in the Bolt Knowledge Base. Providing general Bolt KB summary. You may need to infer from the full content:\n\n{kb_content[:3000]}" # Limit size
        _tool_log(f"No specific Bolt sections for '{query_topic}'. Returning truncated full KB.")
    else: # No titles found, means KB might be unstructured or very short
        result = f"The Bolt Knowledge Base content is:\n\n{kb_content[:3000]}" # Limit size
        _tool_log(f"Bolt KB is unstructured or short. Returning truncated full KB for '{query_topic}'.")

    return result

def handle_get_dtc_knowledge_base_info(query_topic: str) -> str:
    _tool_log(f"Handling get_dtc_knowledge_base_info. Query Topic: {query_topic}")
    kb_content = _load_kb_content(DTC_KB_FILE)
    if "Error:" in kb_content:
        return kb_content
    
    # Using the same simple keyword search for DTC as for Bolt
    relevant_sections = []
    query_keywords = query_topic.lower().split()
    current_section = ""
    title_line_found = False

    for line in kb_content.splitlines():
        if line.isupper() and len(line) > 5 or "---" in line or "===" in line:
            if title_line_found and current_section:
                if any(keyword in current_section.lower() for keyword in query_keywords) or \
                   query_topic.lower() in current_section.lower():
                    relevant_sections.append(current_section.strip())
            current_section = line + "\n"
            title_line_found = True
        elif title_line_found:
            current_section += line + "\n"
    
    if title_line_found and current_section: # Check last section
        if any(keyword in current_section.lower() for keyword in query_keywords) or \
           query_topic.lower() in current_section.lower():
            relevant_sections.append(current_section.strip())

    if relevant_sections:
        result = f"Found the following information related to '{query_topic}' in the DTC Knowledge Base:\n\n"
        result += "\n\n---\n\n".join(relevant_sections)
        _tool_log(f"Found {len(relevant_sections)} relevant sections for DTC query '{query_topic}'.")
    elif title_line_found:
        result = f"Could not find specific information for '{query_topic}' in the DTC Knowledge Base. Providing general DTC KB summary. You may need to infer from the full content:\n\n{kb_content[:3000]}"
        _tool_log(f"No specific DTC sections for '{query_topic}'. Returning truncated full KB.")
    else:
        result = f"The DTC Knowledge Base content is:\n\n{kb_content[:3000]}"
        _tool_log(f"DTC KB is unstructured or short. Returning truncated full KB for '{query_topic}'.")
        
    return result

# Dispatch dictionary to map function names to handler functions
TOOL_HANDLERS = {
    SEND_EMAIL_SUMMARY_TOOL_NAME: handle_send_email_discussion_summary,
    RAISE_TICKET_TOOL_NAME: handle_raise_ticket_for_missing_knowledge,
    GET_BOLT_KB_TOOL_NAME: handle_get_bolt_knowledge_base_info,
    GET_DTC_KB_TOOL_NAME: handle_get_dtc_knowledge_base_info,
    # END_CONVERSATION_TOOL_NAME is handled directly by OpenAISpeechClient for state change
}