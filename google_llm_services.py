# google_llm_services.py
import os
from dotenv import load_dotenv

load_dotenv()

import google.generativeai as genai
from google.generativeai.types import GenerationConfig # Tool removed
from datetime import datetime
import logging

LOG_FILE_NAME = "google_services.log"
logger = logging.getLogger("GoogleLLMServiceLogger")
if not logger.handlers:
    logger.setLevel(logging.DEBUG)
    file_handler = logging.FileHandler(LOG_FILE_NAME)
    file_handler.setLevel(logging.DEBUG)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
# Using your specified DEFAULT_GEMINI_MODEL
DEFAULT_GEMINI_MODEL = "gemini-1.5-pro-latest"

gemini_client = None # This global client is not heavily used if model is instantiated per call
if GOOGLE_API_KEY:
    try:
        genai.configure(api_key=GOOGLE_API_KEY)
        logger.info(f"Google GenAI client configured. Default model for instantiation: {DEFAULT_GEMINI_MODEL}")
        # You could initialize a base client here if system_instruction wasn't per-call
        # gemini_client = genai.GenerativeModel(DEFAULT_GEMINI_MODEL)
    except Exception as e:
        logger.critical(f"CRITICAL_ERROR: Failed to configure Google GenAI client: {e}", exc_info=True)
        # gemini_client = None # Already None
else:
    logger.critical("CRITICAL_ERROR: GOOGLE_API_KEY not found. Google services will be unavailable.")

def get_gemini_response(
    user_prompt_text: str,
    system_instruction_text: str, 
    use_google_search_tool: bool = False,
    model_name: str = DEFAULT_GEMINI_MODEL # Uses your specified default
) -> str:
    current_model_instance = None
    effective_model_name = model_name

    if not GOOGLE_API_KEY:
        logger.error("Function Entry: GOOGLE_API_KEY not available.")
        return "Error: Google AI service is not available due to missing API key."

    try:
        logger.info(f"Instantiating model '{effective_model_name}' for this request. System instruction: {'Provided' if system_instruction_text else 'Not provided'}.")
        current_model_instance = genai.GenerativeModel(
            model_name=effective_model_name,
            system_instruction=system_instruction_text if system_instruction_text else None
        )
        logger.info(f"Successfully instantiated model {effective_model_name}.")
    except Exception as e_inst:
        logger.error(f"Failed to instantiate model {effective_model_name} with system_instruction: {e_inst}", exc_info=True)
        return f"Error: Could not access Google AI model ({effective_model_name})."

    if not current_model_instance:
        logger.error("CRITICAL: current_model_instance is None after instantiation attempt.")
        return "Error: Internal issue selecting Google AI model."

    logger.info(f"Sending request to Gemini (model: {current_model_instance.model_name}). Google Search tool: {'Enabled' if use_google_search_tool else 'Disabled'}.")
    logger.debug(f"User Prompt (first 100 chars): {user_prompt_text[:100]}")
    
    tools_param_for_api = None
    if use_google_search_tool:
        try:
            from google.generativeai.types import Tool as TypesTool
            tools_param_for_api = [TypesTool(google_search_retrieval={})]
            logger.debug("Attempting Google Search tool using [types.Tool(google_search_retrieval={})]")
        except ImportError:
            logger.warning("google.generativeai.types.Tool not found. Trying string 'google_search_retrieval' for tools.")
            tools_param_for_api = 'google_search_retrieval'
        except Exception as e_tool_types:
            logger.error(f"Error trying to construct TypesTool for google_search_retrieval: {e_tool_types}. Using string fallback.")
            tools_param_for_api = 'google_search_retrieval'

    generation_config_dict = {}
    generation_config_obj = GenerationConfig(**generation_config_dict) if generation_config_dict else None
    
    messages_for_gemini = [{'role': 'user', 'parts': [user_prompt_text]}]

    try:
        response = current_model_instance.generate_content(
            contents=messages_for_gemini,
            generation_config=generation_config_obj,
            tools=tools_param_for_api
        )

        if response.candidates and response.candidates[0].content.parts:
            generated_text = "".join(part.text for part in response.candidates[0].content.parts if hasattr(part, 'text'))
            logger.debug(f"Gemini generated text (first 100 chars): {generated_text[:100]}")
            
            if not generated_text.strip() and use_google_search_tool:
                 logger.warning(f"Gemini returned empty text despite search tool being enabled for prompt: {user_prompt_text[:60]}")
                 if response.prompt_feedback and response.prompt_feedback.block_reason:
                     block_reason_msg = response.prompt_feedback.block_reason_message or str(response.prompt_feedback.block_reason)
                     logger.warning(f"Gemini prompt blocked. Reason: {block_reason_msg}")
                     return f"Information retrieval blocked by safety settings. Reason: {block_reason_msg}"
                 return f"No specific information found by Google AI for '{user_prompt_text[:60]}...'."
            return generated_text.strip()
        else:
            block_reason_msg = "Unknown reason"
            finish_reason_msg = "Unknown"
            if response.prompt_feedback and response.prompt_feedback.block_reason:
                block_reason_msg = response.prompt_feedback.block_reason_message or str(response.prompt_feedback.block_reason)
            if response.candidates and response.candidates[0].finish_reason:
                finish_reason_msg = str(response.candidates[0].finish_reason)

            logger.warning(f"Gemini response empty/no candidates. Prompt: {user_prompt_text[:60]}. BlockReason: {block_reason_msg}. FinishReason: {finish_reason_msg}. Candidates: {response.candidates if response.candidates else 'None'}")
            if block_reason_msg != "Unknown reason":
                return f"Information retrieval blocked. Reason: {block_reason_msg}"
            return f"Google AI did not return a valid response for '{user_prompt_text[:60]}...' (Finish: {finish_reason_msg})."

    except Exception as e:
        logger.error(f"Exception during Gemini API call: {e}", exc_info=True)
        return f"Error: Could not get a response from Google AI service. Detail: {str(e)[:150]}"

# --- SIMPLIFIED Test Section ---
# --- ULTRA-SIMPLIFIED Test Section (Focus: Grounding with Default Model) ---
# --- ULTRA-SIMPLIFIED Test Section (Focus: Grounding with Default Model) ---
if __name__ == '__main__':
    logger.info("--- Running google_llm_services.py directly for testing (SINGLE GROUNDING TEST) ---")

    if not GOOGLE_API_KEY:
        logger.error("Please set your GOOGLE_API_KEY in a .env file. Test cannot run.")
    else:
        logger.info(f"--- Starting single grounding test with DEFAULT_GEMINI_MODEL: {DEFAULT_GEMINI_MODEL} ---")
        
        # Single Test: Grounded question using the DEFAULT_GEMINI_MODEL
        # Replace with a real recent event or a question that clearly needs up-to-date search.
        # Example: "What were the major outcomes of the most recent G20 summit?"
        # Or, if you have a specific type of grounded query your agent will make, use that.
        test_prompt_grounded = "Which latest android version was anounced and on which date?"
        
        system_instruction_grounded = (
            "You are a helpful AI assistant. Your task is to find relevant and recent information "
            "using Google Search to answer the user's query. Provide a concise summary based on the search results. "
            "If no specific information is found, clearly state that."
        )
        
        logger.info(f"\n--- Test: Grounded Query with {DEFAULT_GEMINI_MODEL} ---")
        result_grounded = get_gemini_response(
            user_prompt_text=test_prompt_grounded,
            system_instruction_text=system_instruction_grounded,
            use_google_search_tool=True, # Ensure grounding is enabled
            model_name=DEFAULT_GEMINI_MODEL # Using the configured default model
        )
        
        logger.info(f"Test Prompt: \"{test_prompt_grounded}\"")
        logger.info(f"System Instruction: \"{system_instruction_grounded}\"")
        logger.info(f"Model Used: {DEFAULT_GEMINI_MODEL}")
        logger.info(f"Search Enabled: True")
        logger.info(f"Test Result:\n{result_grounded}")

        if "Error:" in result_grounded:
            logger.error("The test encountered an error.")
        elif "found" in result_grounded.lower() and ("specific information" in result_grounded.lower() or "relevant information" in result_grounded.lower() or "could not find" in result_grounded.lower()):
            logger.warning("Test Result suggests information might not have been found by search, or search was not effective. Review output.")
        else:
            logger.info("Test completed. Review output for relevance and accuracy of grounding.")