# google_llm_services.py
import os
import google.generativeai as genai
from google.generativeai.types import GenerationConfig, Tool, Part # Ensure correct imports for types
from datetime import datetime

# --- Configuration ---
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
DEFAULT_GEMINI_MODEL = "gemini-1.5-pro-latest" # Using latest stable Pro model as a robust default

# --- Logging ---
def _log_google_service(message):
    print(f"[GOOGLE_SERVICE] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}")

# --- GenAI Client Initialization ---
gemini_client = None
if GOOGLE_API_KEY:
    try:
        genai.configure(api_key=GOOGLE_API_KEY)
        # Test configuration by listing models (optional, good for diagnostics)
        # for m in genai.list_models():
        #     if 'generateContent' in m.supported_generation_methods:
        #         _log_google_service(f"Available model: {m.name}")
        # Create a client/model instance for reuse if needed, or just use genai.GenerativeModel directly
        _log_google_service(f"Google GenAI client configured with model: {DEFAULT_GEMINI_MODEL}")
        gemini_client = genai.GenerativeModel(DEFAULT_GEMINI_MODEL) # Store the model for easy access
    except Exception as e:
        _log_google_service(f"CRITICAL_ERROR: Failed to configure Google GenAI client: {e}")
        gemini_client = None
else:
    _log_google_service("CRITICAL_ERROR: GOOGLE_API_KEY not found in environment. Google services will be unavailable.")

def get_gemini_response(
    user_prompt_text: str,
    system_instruction_text: str,
    use_google_search_tool: bool = False,
    model_name: str = DEFAULT_GEMINI_MODEL
) -> str:
    """
    Gets a non-streaming response from a Gemini model.

    Args:
        user_prompt_text: The user's query/prompt.
        system_instruction_text: The system instruction to guide the model.
        use_google_search_tool: Whether to enable the Google Search tool for the model.
        model_name: The specific Gemini model to use.

    Returns:
        A string containing the generated text from Gemini or an error message.
    """
    if not gemini_client and not GOOGLE_API_KEY: # Check if client init failed or key missing
        _log_google_service("ERROR: Gemini client not initialized or API key missing. Cannot process request.")
        return "Error: Google AI service is not available due to missing API key or initialization failure."
    if not gemini_client: # Fallback if only client is None but key was present
        _log_google_service("ERROR: Gemini client is not available. Attempting one-time model instantiation.")
        try:
            current_model_instance = genai.GenerativeModel(model_name)
        except Exception as e_inst:
            _log_google_service(f"ERROR: Failed to instantiate model {model_name} on-the-fly: {e_inst}")
            return f"Error: Could not access Google AI model ({model_name})."
    else:
        # If default model was used for client, but a different one is requested now
        if model_name != gemini_client.model_name:
             _log_google_service(f"Switching to model {model_name} for this request.")
             try:
                 current_model_instance = genai.GenerativeModel(model_name)
             except Exception as e_sw_inst:
                _log_google_service(f"ERROR: Failed to instantiate switched model {model_name}: {e_sw_inst}. Falling back to default client model.")
                current_model_instance = gemini_client # Fallback to the initially configured one
        else:
            current_model_instance = gemini_client


    _log_google_service(f"Sending request to Gemini ({model_name}). Search tool: {'Enabled' if use_google_search_tool else 'Disabled'}.")
    _log_google_service(f"User Prompt (first 100 chars): {user_prompt_text[:100]}")
    _log_google_service(f"System Instruction (first 100 chars): {system_instruction_text[:100]}")

    tools_list = []
    if use_google_search_tool:
        # For google.generativeai package, enabling Google Search is often simpler:
        # It might be part of the standard tool configuration or a parameter.
        # The direct Tool definition for GoogleSearch is more for explicit control.
        # Let's try with the explicit Tool object first, aligning with the provided user example structure
        tools_list.append(Tool(google_search={})) # Empty dict enables the default Google Search

    generation_config = GenerationConfig(
        # temperature=0.7, # Example, adjust as needed
        # top_p=1.0,
        # top_k=32,
        # max_output_tokens=1024, # Example
        # response_mime_type="text/plain" # This seems to be for client.models.generate_content_stream, may not be needed for generate_content
    )
    
    # Constructing contents list
    # The 'system_instruction' parameter is available directly in `generate_content` for some versions/models
    # or needs to be part of the `contents` list for others.
    # google-generativeai (newer library) uses a 'system_instruction' parameter in GenerativeModel.
    # Let's assume we pass it directly to `generate_content` if available, or prepend to contents.

    messages_for_gemini = []
    if system_instruction_text:
        # For `generate_content`, system instructions are often passed as a top-level param or part of the model init.
        # If it must be in contents, it's usually the first message (but without a 'role' or a specific system role).
        # However, google.generativeai.GenerativeModel can take `system_instruction` at init or in `generate_content`.
        # Let's try passing it directly to `generate_content` call.
        pass

    messages_for_gemini.append({'role': 'user', 'parts': [user_prompt_text]})

    try:
        # Non-streaming call
        response = current_model_instance.generate_content(
            contents=messages_for_gemini,
            generation_config=generation_config,
            tools=tools_list if tools_list else None, # Pass tools if any
            system_instruction=Part.from_text(system_instruction_text) if system_instruction_text else None
            # The above assumes system_instruction takes a Part. If it's just a string, adjust.
        )

        # _log_google_service(f"Raw Gemini Response: {response}") # Be careful with logging full PII or large responses

        if response.candidates and response.candidates[0].content.parts:
            generated_text = "".join(part.text for part in response.candidates[0].content.parts if hasattr(part, 'text'))
            _log_google_service(f"Gemini generated text (first 100 chars): {generated_text[:100]}")
            
            # Check for function calls if any were made and handled by the model (tool use)
            # The response structure for tool calls needs to be inspected.
            # If Gemini uses a tool and provides output directly, `generated_text` would contain it.
            # If it signals back that a tool *should* be called by the client, the structure is different.
            # Given we're enabling Gemini's *internal* Google Search tool, the results should be part of its text generation.

            if not generated_text.strip() and use_google_search_tool:
                 _log_google_service(f"WARN: Gemini returned empty text despite search tool being enabled for prompt: {user_prompt_text[:60]}")
                 # Check if there's a blocked prompt or safety issue
                 if response.prompt_feedback and response.prompt_feedback.block_reason:
                     block_reason_msg = response.prompt_feedback.block_reason_message or str(response.prompt_feedback.block_reason)
                     _log_google_service(f"WARN: Gemini prompt blocked. Reason: {block_reason_msg}")
                     return f"Information retrieval blocked by safety settings. Reason: {block_reason_msg}"
                 return f"No specific information found by Google AI for '{user_prompt_text[:60]}...'."


            return generated_text.strip()
        else:
            # Handle cases where the response might be blocked or empty
            block_reason_msg = "Unknown reason"
            if response.prompt_feedback and response.prompt_feedback.block_reason:
                block_reason_msg = response.prompt_feedback.block_reason_message or str(response.prompt_feedback.block_reason)
                _log_google_service(f"WARN: Gemini response blocked or empty. Reason: {block_reason_msg}")
                return f"Information retrieval blocked. Reason: {block_reason_msg}"
            elif not response.candidates:
                 _log_google_service(f"WARN: Gemini returned no candidates. Prompt: {user_prompt_text[:60]}")
                 return f"Google AI did not return a valid response for '{user_prompt_text[:60]}...'."
            else:
                _log_google_service(f"WARN: Gemini response structure unexpected or part list empty. Response: {response}")
                return f"Received an empty or unexpected response from Google AI for '{user_prompt_text[:60]}...'."


    except Exception as e:
        _log_google_service(f"ERROR: Exception during Gemini API call: {e}")
        # Check for specific API error types if the SDK provides them
        # For example, google.api_core.exceptions.PermissionDenied, etc.
        return f"Error: Could not get a response from Google AI service. Detail: {str(e)[:100]}"

if __name__ == '__main__':
    # --- Example Test Usage (requires GOOGLE_API_KEY in .env) ---
    print("--- Running google_llm_services.py directly for testing ---")
    if not GOOGLE_API_KEY:
        print("Please set your GOOGLE_API_KEY in a .env file to run this test.")
    else:
        _log_google_service("Testing Gemini services...")

        # Test 1: General knowledge query using Google Search tool
        print("\n--- Test 1: General knowledge with Google Search ---")
        test_prompt_1 = "What is the current weather in Dubai?"
        # System instruction for general search (simplified for this direct test)
        # The one defined in the main plan is more comprehensive
        system_instruction_general = """
        You are an AI assistant. The user is an employee at Dubai Taxi Corporation (DTC) in Dubai.
        Your task is to answer the user's query based *only* on the information available from the Google Search results provided to you or that you can fetch.
        Focus on providing factual and concise answers.
        The context of queries will generally be Dubai-related, professional, and business-oriented.
        Tailor your response to be useful for a DTC employee.
        Avoid speculative or off-topic information.
        If the search does not yield a clear answer, state that the information could not be found.
        Give the direct answer.
        """
        result1 = get_gemini_response(test_prompt_1, system_instruction_general, use_google_search_tool=True)
        print(f"Prompt: {test_prompt_1}\nGemini: {result1}")

        # Test 2: Taxi Ideas for Today (simulated prompt)
        print("\n--- Test 2: Taxi Ideas for Today ---")
        # This system prompt is similar to the one provided by the user for the taxi ideas function
        taxi_ideas_system_prompt = """
        I am a dtc dubai person. I am looking for news, events, or conditions in Dubai for today, {current_date},
        that could suggest opportunities for my taxi fleet (e.g., areas needing more taxis, potential high demand).
        Consider Khaleej Times or other local news for events, festivals, or important gatherings and their locations.
        Also, consider if any weather events might impact flights, suggesting a need for more taxis at the airport.
        Provide a concise summary of actionable ideas. If no specific business-impacting ideas are found, state 'No new business ideas found for today based on current information.'
        Do not detail sources. Focus only on today.
        """
        current_date_str = datetime.now().strftime("%B %d, %Y") # e.g., May 24, 2025
        # The user_prompt for the taxi ideas tool will be constructed by the tool handler.
        # Here, we simulate what the user_prompt_text to get_gemini_response might look like
        # after the tool handler processes it. The key is the system_instruction here.
        # The actual prompt to Gemini for this tool would be more about the *request* for ideas,
        # and the system prompt guides *how* Gemini should find and format those ideas.
        
        # Let's make the user_prompt more direct for this test, as if the tool handler prepared it:
        user_prompt_for_taxi_ideas = f"Find taxi deployment ideas for Dubai on {current_date_str} considering local events and weather."

        # The system prompt itself can contain the date placeholder that would be filled by the calling handler
        filled_taxi_ideas_system_prompt = taxi_ideas_system_prompt.format(current_date=current_date_str)

        result2 = get_gemini_response(
            user_prompt_text=user_prompt_for_taxi_ideas, # This is what the user conceptually asks the tool
            system_instruction_text=filled_taxi_ideas_system_prompt, # This guides Gemini's behavior
            use_google_search_tool=True
        )
        print(f"Effective User Prompt: {user_prompt_for_taxi_ideas}\nEffective System Prompt (first 100): {filled_taxi_ideas_system_prompt[:100]}...\nGemini: {result2}")

        # Test 3: No search tool
        print("\n--- Test 3: No search tool, simple generation ---")
        test_prompt_3 = "Explain the concept of a Large Language Model in one sentence."
        system_instruction_3 = "You are a helpful assistant. Be very concise."
        result3 = get_gemini_response(test_prompt_3, system_instruction_3, use_google_search_tool=False)
        print(f"Prompt: {test_prompt_3}\nGemini: {result3}")

        # Test 4: Model behavior when no specific info is found (using a niche query)
        print("\n--- Test 4: Query likely to yield no specific business idea ---")
        no_idea_prompt = "Are there any specific taxi demand spikes related to international chess tournaments for miniature poodles in Dubai today?"
        # Using the same filled_taxi_ideas_system_prompt
        result4 = get_gemini_response(
            user_prompt_text=no_idea_prompt,
            system_instruction_text=filled_taxi_ideas_system_prompt,
            use_google_search_tool=True
        )
        print(f"Prompt: {no_idea_prompt}\nGemini: {result4}")