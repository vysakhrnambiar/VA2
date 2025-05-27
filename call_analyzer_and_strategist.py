# call_analyzer_and_strategist.py
import os
import json
import openai # Using OpenAI for the strategist LLM for now
import time # For simple retries

# --- Configuration ---
STRATEGIST_LLM_MODEL_DEFAULT = os.getenv("STRATEGIST_LLM_MODEL", "gpt-4-turbo-preview") # e.g., "gpt-4o" or "gpt-3.5-turbo"
LLM_API_MAX_RETRIES = int(os.getenv("LLM_API_MAX_RETRIES", 2)) 
LLM_API_RETRY_DELAY_SECONDS = int(os.getenv("LLM_API_RETRY_DELAY_SECONDS", 5)) 

# --- Logging ---
import logging
logger = logging.getLogger(__name__)
if not logger.hasHandlers():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - [%(name)s.%(funcName)s] - %(message)s')


def analyze_and_strategize_call_outcome(
    db_job_details: dict,
    call_transcript: str,
    ultravox_call_id_of_attempt: str, 
    twilio_call_sid_of_attempt: str,  
    previous_attempts_history: list, 
    llm_client_config: dict
) -> dict: # Always returns a dict, with "error" key on failure
    job_id = db_job_details.get('id', 'UNKNOWN_JOB')
    logger.info(f"Job {job_id}, Attempt UVoxID {ultravox_call_id_of_attempt}: Starting analysis and strategy.")

    openai_api_key = llm_client_config.get("api_key")
    model_name = llm_client_config.get("model_name", STRATEGIST_LLM_MODEL_DEFAULT)

    if not openai_api_key:
        logger.error(f"Job {job_id}: OpenAI API key not provided in llm_client_config.")
        return {"error": "Missing OpenAI API Key for strategist."}
    
    try:
        client = openai.OpenAI(api_key=openai_api_key)
    except Exception as e:
        logger.error(f"Job {job_id}: Failed to initialize OpenAI client: {e}")
        return {"error": f"OpenAI client initialization failed: {e}"}


    history_prompt_str = "No previous attempts for this overall job."
    if previous_attempts_history:
        history_parts = ["\n\n**History of Previous Attempts for this Overall Job:**"]
        for attempt in sorted(previous_attempts_history, key=lambda x: x.get('attempt_number', 0)):
            history_parts.append(
                f"\n--- Attempt #{attempt.get('attempt_number')} (UVoxID: {attempt.get('ultravox_call_id', 'N/A')}) ---\n"
                f"Objective for that attempt: {attempt.get('objective_for_this_attempt', 'N/A')}\n"
                f"Call End Reason: {attempt.get('end_reason', 'N/A')}\n"
                f"Summary of that attempt: {attempt.get('strategist_summary_of_attempt', 'N/A')}\n"
                f"Outcome of that attempt: {attempt.get('strategist_objective_met_status_for_attempt', 'N/A')}\n"
                f"Error details (if any): {attempt.get('attempt_error_details', 'None')}\n"
            )
        history_prompt_str = "".join(history_parts)

    prompt_template = f"""
You are an advanced AI Call Strategist. Your role is to analyze the outcome of an automated phone call and decide on the next best course of action for an OVERALL JOB.

**Initial Call Context (Overall Job):**
*   Job ID: {job_id}
*   Original Overall Objective for the Job: "{db_job_details.get('initial_call_objective_description')}"
*   Contact Name: "{db_job_details.get('contact_name')}"
*   Phone Number: "{db_job_details.get('phone_number')}"
*   Number of Previous Attempts for this Job (excluding current): {db_job_details.get('retries_attempted', 0)}
*   Maximum Allowed Retries for this Job: {db_job_details.get('max_retries', 3)}

**Details of the CURRENT Call Attempt Being Analyzed:**
*   Objective for this Current Attempt: "{db_job_details.get('current_call_objective_description', 'Same as original job objective')}"
*   UltraVox Call ID of this attempt: {ultravox_call_id_of_attempt}
*   Twilio SID of this attempt: {twilio_call_sid_of_attempt}

{history_prompt_str}

**Transcript of THIS LATEST Call Attempt:**
---BEGIN TRANSCRIPT---
{call_transcript}
---END TRANSCRIPT---

**Your Tasks:**

1.  **Summarize THIS LATEST call attempt:** Provide a concise summary (max 3-4 sentences) of what happened during THIS specific call from the perspective of the automated caller. This summary will be shown to the end-user.
2.  **Assess Objective Completion for THIS ATTEMPT:** Based on the "Objective for this Current Attempt" and THIS transcript, was that specific objective met?
3.  **Analyze User Requests/Cues & Call Quality:** Did the contact explicitly ask to be called back at a specific time (e.g., "call me in 10 minutes," "call me tomorrow morning at 9 AM")? Did they provide any information that makes the *original overall job objective* currently unachievable or moot? Was the conversation inconclusive for THIS attempt due to poor line quality, repeated misunderstandings, or other issues?
4.  **Determine Next Action for the OVERALL JOB:** Based on your analysis of THIS attempt and any relevant history, decide the next logical step for the overall job. If the "Original Overall Objective for the Job" appears to be fulfilled by this current attempt, the job should be marked as completed successfully. Consider the number of retries already attempted.

**Output Format (Return ONLY a single, valid JSON object with NO markdown formatting):**

```json
{{
    "summary_for_main_agent": "string",
    "objective_met_status_for_current_attempt": "MET" | "NOT_MET_RETRY_RECOMMENDED_FOR_JOB" | "NOT_MET_RETRY_NOT_RECOMMENDED_FOR_JOB" | "INCONCLUSIVE_CHECK_RETRY",
    "next_action_decision_for_job": "MARK_JOB_COMPLETED_SUCCESS" | "SCHEDULE_JOB_RETRY" | "MARK_JOB_FAILED_OBJECTIVE_UNACHIEVED" | "MARK_JOB_FAILED_MAX_RETRIES",
    "reasoning_for_decision": "string",
    "next_call_objective_if_retry": "string_or_null",
    "requested_retry_delay_minutes": "integer_or_null"
}}
Detailed Key Explanations for JSON Output:
"summary_for_main_agent": Your concise summary of THIS LATEST call attempt (for the end-user).
"objective_met_status_for_current_attempt": Your assessment of THIS SPECIFIC ATTEMPT against its stated objective.
"MET": The "Objective for this Current Attempt" was fully achieved.
"NOT_MET_RETRY_RECOMMENDED_FOR_JOB": Objective for current attempt not met, but a retry FOR THE OVERALL JOB seems viable and potentially fruitful.
"NOT_MET_RETRY_NOT_RECOMMENDED_FOR_JOB": Objective for current attempt not met, and further retries FOR THE OVERALL JOB seem unlikely to succeed or are not appropriate.
"INCONCLUSIVE_CHECK_RETRY": This call attempt ended without a clear resolution for its specific objective; evaluate if a retry for the job makes sense.
"next_action_decision_for_job": The decision for the OVERALL JOB.
"MARK_JOB_COMPLETED_SUCCESS": If objective_met_status_for_current_attempt is "MET" AND this fulfills the "Original Overall Objective for the Job".
"SCHEDULE_JOB_RETRY": If a retry for the overall job is recommended. This should typically align with objective_met_status_for_current_attempt being NOT_MET_RETRY_RECOMMENDED_FOR_JOB or INCONCLUSIVE_CHECK_RETRY (and retries < max_retries).
"MARK_JOB_FAILED_OBJECTIVE_UNACHIEVED": If objective_met_status_for_current_attempt is NOT_MET_RETRY_NOT_RECOMMENDED_FOR_JOB, or if it's INCONCLUSIVE_CHECK_RETRY and retries are exhausted or further attempts are deemed futile.
"MARK_JOB_FAILED_MAX_RETRIES": If retries_attempted (from input, for previous attempts) + 1 (for this current attempt) will equal or exceed max_retries (from input) AND the objective is still not met.
"reasoning_for_decision": Your detailed reasoning.
"next_call_objective_if_retry": If "next_action_decision_for_job" is "SCHEDULE_JOB_RETRY", provide the revised call objective for the NEXT attempt of the overall job. It MUST incorporate context from this call AND aim to progress the "Original Overall Objective for the Job".
"requested_retry_delay_minutes": If the contact explicitly requested a callback after N minutes. Null otherwise.
If the "Original Overall Objective for the Job" is met by this current attempt, next_action_decision_for_job MUST be MARK_JOB_COMPLETED_SUCCESS.
If retries_attempted + 1 >= max_retries AND the objective is still not met, next_action_decision_for_job must be MARK_JOB_FAILED_MAX_RETRIES.
"""
    logger.info(f"Job {job_id}: Sending prompt to LLM (model: {model_name}). Prompt length: {len(prompt_template)} chars.")
    # logger.debug(f"Job {job_id}: Strategist PROMPT:\n{prompt_template}") # Very verbose

    response_content = None # Initialize to ensure it's defined in case all retries fail before assignment
    for attempt_num in range(LLM_API_MAX_RETRIES + 1): # Allow for initial attempt + number of retries
        try:
            completion = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": "You are an AI Call Strategist. Your output must be a single valid JSON object as specified, without any markdown formatting or extraneous text."},
                    {"role": "user", "content": prompt_template}
                ],
                response_format={"type": "json_object"},
                temperature=0.2
            )
            response_content = completion.choices[0].message.content
            logger.info(f"Job {job_id}: LLM response received (API attempt {attempt_num + 1}).")
            # logger.debug(f"Job {job_id}: LLM Raw Response: {response_content}") # Can be very verbose

            action_plan = json.loads(response_content) # Attempt to parse
            
            required_keys = [
                "summary_for_main_agent", "objective_met_status_for_current_attempt", 
                "next_action_decision_for_job", "reasoning_for_decision"
            ]
            if not all(key in action_plan for key in required_keys):
                error_msg = f"LLM response missing one or more required keys. Keys present: {list(action_plan.keys())}"
                logger.error(f"Job {job_id}: {error_msg} in response: {response_content}")
                # This is a malformed response from the LLM's content perspective.
                # Retrying might not help if the LLM is consistently failing schema.
                # For now, we'll let the retry loop handle it if it's an intermittent issue.
                # If it persists, it will fall through to the "All attempts failed" error.
                if attempt_num == LLM_API_MAX_RETRIES: # If last attempt
                    return {"error": "LLM response schema validation failed: missing keys.", "raw_response": response_content}
                # Raise an error to trigger retry for this specific case of missing keys
                raise ValueError(error_msg)


            logger.info(f"Job {job_id}: Successfully parsed LLM action plan.")
            return action_plan # Success

        except json.JSONDecodeError as json_err:
            logger.error(f"Job {job_id}: Failed to parse LLM JSON response (API attempt {attempt_num + 1}): {json_err}. Response was: {response_content}")
            if attempt_num == LLM_API_MAX_RETRIES:
                return {"error": f"Malformed JSON from LLM after {LLM_API_MAX_RETRIES + 1} attempts.", "raw_response": response_content}
            # Fall through to retry delay

        except openai.APIError as api_err:
            logger.error(f"Job {job_id}: OpenAI API error (API attempt {attempt_num + 1}): {api_err}")
            if attempt_num == LLM_API_MAX_RETRIES:
                return {"error": f"OpenAI API error after {LLM_API_MAX_RETRIES + 1} attempts: {str(api_err)}"}
            # Fall through to retry delay
        
        except ValueError as val_err: # Catching the ValueError raised for missing keys
            logger.error(f"Job {job_id}: LLM response content validation error (API attempt {attempt_num + 1}): {val_err}")
            if attempt_num == LLM_API_MAX_RETRIES:
                return {"error": f"LLM response schema validation failed after {LLM_API_MAX_RETRIES + 1} attempts: {val_err}", "raw_response": response_content}
            # Fall through to retry delay

        except Exception as e: 
            logger.critical(f"Job {job_id}: Unexpected exception during LLM call or processing (API attempt {attempt_num + 1}): {type(e).__name__} - {e}")
            if attempt_num == LLM_API_MAX_RETRIES:
                return {"error": f"Unexpected exception after {LLM_API_MAX_RETRIES + 1} attempts: {str(e)}"}
            # Fall through to retry delay

        if attempt_num < LLM_API_MAX_RETRIES:
            delay = LLM_API_RETRY_DELAY_SECONDS * (attempt_num + 1) # Simple increasing delay
            logger.warning(f"Job {job_id}: Retrying LLM call in {delay}s...")
            time.sleep(delay)

    # If all retries failed
    logger.error(f"Job {job_id}: All {LLM_API_MAX_RETRIES + 1} attempts to get valid response from LLM failed.")
    return {"error": "All LLM call attempts failed.", "last_raw_response": response_content}

if __name__ == '__main__':
    # Setup basic logging for standalone run, ensuring handler is added if not already.
    if not logger.handlers:  # Add handler only if no handlers are configured (e.g. when run directly)
        handler = logging.StreamHandler()
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - [%(name)s.%(funcName)s] - %(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)  # Set to DEBUG for detailed output during standalone test
    logger.info("--- Running call_analyzer_and_strategist.py directly for testing (Single Test Case) ---")

    if not os.getenv("OPENAI_API_KEY"):
        logger.error("Please set your OPENAI_API_KEY in a .env file to run this test.")
    else:
        mock_job_details_base = {
            "id": 101,
            "initial_call_objective_description": "Get confirmation from Mr. Smith about the new project timeline (target end of Q3) and ask if he needs any resources from our end to meet this target.",
            "contact_name": "Mr. Smith",
            "phone_number": "+1234567890",
            "max_retries": 2 
        }
        mock_llm_config = {
            "api_key": os.getenv("OPENAI_API_KEY"),
            "model_name": os.getenv("STRATEGIST_LLM_MODEL_TEST", "gpt-3.5-turbo-0125") 
        }
        
        logger.info("\n--- Test Case 1: First attempt, user asks for callback ---")
        mock_job_details_tc1 = mock_job_details_base.copy()
        mock_job_details_tc1["retries_attempted"] = 0 
        mock_job_details_tc1["current_call_objective_description"] = mock_job_details_tc1["initial_call_objective_description"]

        mock_transcript_attempt1 = """Agent: Hello Mr. Smith, this is an assistant from ACME Corp. I'm calling to get confirmation about the new project timeline, targeting end of Q3, and to ask if you need any resources from our end to meet this.
User: Oh, hi. The Q3 target is tight. We had a vendor slip. I can't confirm Q3 today. Can you call me in two days? Say, Thursday morning? I'll have an update from the vendor then. No resources needed from your side yet.
Agent: Understood, Mr. Smith. So the Q3 timeline is currently unconfirmed due to a vendor slip, and you're not requesting resources now. I will note that you'd like a callback on Thursday morning for an update. Is that correct?
User: Yes, Thursday morning is good.
Agent: System: [Tool Call: hangUp, Args: {"reason":"User requested callback on Thursday"}]
System: [Tool Result: hangUp, Out: OK]
"""
        action_plan1 = analyze_and_strategize_call_outcome(
            db_job_details=mock_job_details_tc1,
            call_transcript=mock_transcript_attempt1,
            ultravox_call_id_of_attempt="uvox_tc1_id",
            twilio_call_sid_of_attempt="twilio_tc1_id",
            previous_attempts_history=[],
            llm_client_config=mock_llm_config
        )
        if action_plan1:
            logger.info(f"\nAction Plan (Attempt 1):\n{json.dumps(action_plan1, indent=2)}")
        else: # Should ideally not happen if error dict is returned
            logger.error("Action Plan 1 was None, indicating a setup or critical strategist error.")
