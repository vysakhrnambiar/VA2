# test_ultravox_call.py
import os
import time
import json
import requests # To make HTTP requests to UltraVox API
from twilio.rest import Client as TwilioClient
from dotenv import load_dotenv

# --- Configuration ---
load_dotenv() # Load variables from .env file

ULTRAVOX_API_KEY = os.getenv("ULTRAVOX_API_KEY")
ULTRAVOX_AGENT_ID = os.getenv("ULTRAVOX_AGENT_ID")
ULTRAVOX_BASE_URL = "https://api.ultravox.ai/api" # CORRECTED BASE URL

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER_FROM = os.getenv("TWILIO_PHONE_NUMBER") # Your Twilio number

# --- Test Call Parameters ---
# IMPORTANT: Replace with a phone number you can answer for testing
# Example: TEST_RECIPIENT_PHONE_NUMBER_TO = "+12345678900"
TEST_RECIPIENT_PHONE_NUMBER_TO = "+919744554079" # Using the number you provided as an example
if TEST_RECIPIENT_PHONE_NUMBER_TO == "+1xxxxxxxxxx" or not TEST_RECIPIENT_PHONE_NUMBER_TO.startswith("+"): # Basic check
    print(f"CRITICAL: TEST_RECIPIENT_PHONE_NUMBER_TO is currently '{TEST_RECIPIENT_PHONE_NUMBER_TO}'.")
    print("Please update TEST_RECIPIENT_PHONE_NUMBER_TO in the script with your actual international test phone number (e.g., +1234567890).")
    exit()

TEST_TEMPLATE_CONTEXT = {
    "company_name": "DTC Executive Testing Division",
    "contact_name": "Test User Vysakh", # The name the agent will use for the person it's calling
    "call_objective": "conduct a brief test of our automated calling system. I need you to confirm you can hear me clearly by saying 'yes, I can hear you', and then I will use my hangUp tool to end our conversation. Please say a clear phrase like 'yes I can hear you' after I greet you so I know the line is clear and your speech is transcribed."
}

# --- Helper Functions ---
def print_json(data, title="JSON Data"):
    print(f"\n--- {title} ---")
    print(json.dumps(data, indent=2))

# --- Main Test Logic ---
def run_test_call():
    print("Validating environment variables...")
    required_vars = {
        "ULTRAVOX_API_KEY": ULTRAVOX_API_KEY,
        "ULTRAVOX_AGENT_ID": ULTRAVOX_AGENT_ID,
        "TWILIO_ACCOUNT_SID": TWILIO_ACCOUNT_SID,
        "TWILIO_AUTH_TOKEN": TWILIO_AUTH_TOKEN,
        "TWILIO_PHONE_NUMBER_FROM": TWILIO_PHONE_NUMBER_FROM
    }
    missing_vars = [k for k, v in required_vars.items() if not v]
    if missing_vars:
        print(f"ERROR: Missing critical environment variables: {', '.join(missing_vars)}. Please check your .env file.")
        return
    print("Environment variables seem okay.")

    # 1. Create UltraVox Call to get joinUrl
    print(f"\nStep 1: Creating UltraVox call for agent {ULTRAVOX_AGENT_ID}...")
    uv_call_payload = {
        "medium": {"twilio": {}},
        "firstSpeakerSettings": {
            "agent": { # Agent will generate its first utterance based on the templated system prompt
                "uninterruptible": False
            }
        },
        "templateContext": TEST_TEMPLATE_CONTEXT,
        "metadata": {
            "test_script_run_id": f"test_{int(time.time())}",
            "purpose": "Initial plumbing test for outbound call"
        },
        "recordingEnabled": True # Good for debugging
    }
    print_json(uv_call_payload, "UltraVox Create Call Payload")

    headers = {
        "X-API-Key": ULTRAVOX_API_KEY, # Correct header name
        "Content-Type": "application/json"
    }
    ultravox_call_id = None
    join_url = None

    try:
        response = requests.post(
            f"{ULTRAVOX_BASE_URL}/agents/{ULTRAVOX_AGENT_ID}/calls",
            headers=headers,
            json=uv_call_payload,
            timeout=20
        )
        print(f"UltraVox Create Call Raw Response Status: {response.status_code}")
        if response.content:
             try:
                print(f"UltraVox Create Call Raw Response Content (first 500 chars): {response.text[:500]}...")
             except Exception: pass

        response.raise_for_status()
        uv_call_response_data = response.json()
        print_json(uv_call_response_data, "UltraVox Create Call Parsed Response")

        ultravox_call_id = uv_call_response_data.get("callId")
        join_url = uv_call_response_data.get("joinUrl")

        if not ultravox_call_id or not join_url:
            print("ERROR: Could not get callId or joinUrl from UltraVox response.")
            return

        print(f"Successfully created UltraVox call. Call ID: {ultravox_call_id}, Join URL: {join_url}")

    except requests.exceptions.HTTPError as http_err:
        print(f"HTTP error occurred with UltraVox Create Call: {http_err}")
        if http_err.response is not None:
            print(f"Response content: {http_err.response.text}")
        return
    except Exception as e:
        print(f"Error creating UltraVox call: {e}")
        return

    # 2. Place Twilio Call
    print(f"\nStep 2: Placing Twilio call to {TEST_RECIPIENT_PHONE_NUMBER_TO} from {TWILIO_PHONE_NUMBER_FROM}...")
    twilio_call_sid = None
    try:
        twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        escaped_join_url = join_url.replace("&", "&")
        twiml_response = f'<Response><Connect><Stream url="{escaped_join_url}"/></Connect></Response>'
        print(f"TwiML for Twilio: {twiml_response}")

        call = twilio_client.calls.create(
            to=TEST_RECIPIENT_PHONE_NUMBER_TO,
            from_=TWILIO_PHONE_NUMBER_FROM,
            twiml=twiml_response
        )
        twilio_call_sid = call.sid
        print(f"Twilio call initiated. SID: {twilio_call_sid}")
        print(f"Please answer the call on {TEST_RECIPIENT_PHONE_NUMBER_TO} to proceed with the test.")
        print("The UltraVox agent should speak its opening line based on the templateContext.")
        print("After a brief interaction (e.g., you say 'yes I can hear you clearly'), the agent should use its 'hangUp' tool.")

    except Exception as e:
        print(f"Error placing Twilio call: {e}")
        return

    # 3. Monitor UltraVox Call Status
    print(f"\nStep 3: Monitoring UltraVox call {ultravox_call_id} for call termination...")
    call_terminated = False
    max_monitoring_duration_sec = 180 # 3 minutes
    monitoring_interval_sec = 10
    start_time = time.time()
    final_call_details_from_poll = None # To store the last successful status fetch

    while not call_terminated and (time.time() - start_time) < max_monitoring_duration_sec:
        try:
            status_response = requests.get(
                f"{ULTRAVOX_BASE_URL}/calls/{ultravox_call_id}",
                headers={"X-API-Key": ULTRAVOX_API_KEY},
                timeout=10
            )
            status_response.raise_for_status()
            call_status_data = status_response.json()
            final_call_details_from_poll = call_status_data # Store the latest data

            ended_timestamp = call_status_data.get("ended")
            current_end_reason = call_status_data.get("endReason")

            print(f"[{time.strftime('%H:%M:%S')}] UltraVox Call Polled: `ended` TS is '{ended_timestamp}', `endReason` is '{current_end_reason}'")

            if ended_timestamp or current_end_reason:
                call_terminated = True
                print("\nCall termination detected by UltraVox API based on 'ended' timestamp or 'endReason'.")
                print_json(call_status_data, "Final UltraVox Call Status Details from Poll")
                break
            
            time.sleep(monitoring_interval_sec)

        except requests.exceptions.HTTPError as http_err:
            print(f"HTTP error polling UltraVox status: {http_err}")
            if http_err.response is not None:
                print(f"Response content: {http_err.response.text}")
            if http_err.response is not None and http_err.response.status_code == 404:
                print("ERROR: UltraVox call ID not found during polling. Aborting monitoring.")
                return 
            time.sleep(monitoring_interval_sec) 
        except Exception as e:
            print(f"Error polling UltraVox call status: {e}")
            time.sleep(monitoring_interval_sec) 

    if not call_terminated:
        print("Call did not show as terminated via UltraVox API within the monitoring period.")
        if final_call_details_from_poll:
             print_json(final_call_details_from_poll, "Last Polled UltraVox Call Details (Not Ended)")
        if twilio_call_sid:
            try:
                print(f"Attempting to end Twilio call {twilio_call_sid} as a fallback...")
                twilio_client.calls(twilio_call_sid).update(status='completed')
                print("Twilio call status updated to 'completed'.")
            except Exception as e_twilio_end:
                print(f"Could not update Twilio call status: {e_twilio_end}")
        return

    # 4. Get Transcript
    print(f"\nStep 4: Retrieving transcript for UltraVox call {ultravox_call_id}...")
    try:
        messages_response = requests.get(
            f"{ULTRAVOX_BASE_URL}/calls/{ultravox_call_id}/messages",
            headers={"X-API-Key": ULTRAVOX_API_KEY},
            timeout=15
        )
        messages_response.raise_for_status()
        messages_payload = messages_response.json()
        # print_json(messages_payload, "UltraVox Call Messages (Raw Data)") # Uncomment for full debug

        print("\n--- Formatted Transcript ---")
        if messages_payload and "results" in messages_payload and messages_payload["results"]:
            for message in messages_payload["results"]:
                role = message.get("role", "UNKNOWN_ROLE")
                text_content = message.get("text", "").strip()
                tool_name = message.get("toolName")

                if role == "MESSAGE_ROLE_AGENT":
                    print(f"Agent: {text_content if text_content else '[Agent message has no text content]'}")
                elif role == "MESSAGE_ROLE_USER":
                    print(f"User: {text_content if text_content else '[User speech not transcribed or non-speech audio]'}")
                elif role == "MESSAGE_ROLE_TOOL_CALL":
                    args_text = text_content if text_content else "{}"
                    print(f"System: [Tool Call: {tool_name if tool_name else 'UnknownTool'}, Arguments: {args_text}]")
                elif role == "MESSAGE_ROLE_TOOL_RESULT":
                    result_text = text_content if text_content else "[No explicit result text]"
                    print(f"System: [Tool Result: {tool_name if tool_name else 'UnknownTool'}, Output: {result_text}]")
                else:
                    print(f"{role}: {text_content if text_content else '[Message has no text content]'}")
        else:
            print("No messages found in 'results' or 'results' array is empty.")
            if messages_payload:
                 print_json(messages_payload, "Messages Payload (No Results or Empty 'results')")

    except requests.exceptions.HTTPError as http_err:
        print(f"HTTP error retrieving UltraVox messages: {http_err}")
        if http_err.response is not None:
            print(f"Response content: {http_err.response.text}")
    except Exception as e:
        print(f"Error retrieving UltraVox messages: {e}")

    print("\n--- Test Script Completed ---")

if __name__ == "__main__":
    print("--- Starting UltraVox Outbound Call Test Script ---")
    print(f"IMPORTANT: Ensure your UltraVox agent ({ULTRAVOX_AGENT_ID}) is configured with the system prompt that uses templateContext variables: {{company_name}}, {{contact_name}}, {{call_objective}}.")
    print("And ensure it's configured to use a 'hangUp' tool when its objective is met.")
    print(f"The call will be made to: {TEST_RECIPIENT_PHONE_NUMBER_TO}")
    print("If the script seems to hang after 'Twilio call initiated', please check if your phone is ringing and answer it.")
    print("Please say a clear phrase like 'Yes, I can hear you' to test STT for user.")
    input("Press Enter to start the test call or Ctrl+C to abort...")
    run_test_call()