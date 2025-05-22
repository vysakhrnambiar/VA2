# kb_llm_extractor.py
import os
import openai
from datetime import datetime

# --- Configuration ---
# Model to use for KB content extraction
KB_EXTRACTION_MODEL = os.getenv("KB_EXTRACTION_MODEL", "gpt-4o-mini")
# Max tokens for the extraction model's response
KB_EXTRACTION_MAX_TOKENS = int(os.getenv("KB_EXTRACTION_MAX_TOKENS", 1024))
# Temperature for extraction model
KB_EXTRACTION_TEMPERATURE = float(os.getenv("KB_EXTRACTION_TEMPERATURE", 0.0))

# --- Logging ---
# A simple logger for this module.
# If you have a central logging setup, you might pass a logger instance.
def _log_extractor(message):
    print(f"[KB_EXTRACTOR] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {message}")

# --- OpenAI Client Initialization ---
# This client is specifically for the KB extraction task.
# It uses the OPENAI_API_KEY from the environment.
try:
    # Ensure your .env file has OPENAI_API_KEY set.
    # For openai library v1.0.0+
    _extractor_client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    _log_extractor(f"OpenAI client initialized for KB extraction using model: {KB_EXTRACTION_MODEL}.")
except Exception as e:
    _log_extractor(f"CRITICAL_ERROR: Failed to initialize OpenAI client for KB extraction: {e}")
    _extractor_client = None

def extract_relevant_sections(kb_full_text: str, query_topic: str, kb_name: str) -> str:
    """
    Uses an LLM (e.g., gpt-4o-mini) to extract relevant sections from a knowledge base.

    Args:
        kb_full_text: The entire text content of the knowledge base.
        query_topic: The user's query or topic to search for.
        kb_name: The name of the knowledge base (e.g., "Bolt", "DTC") for context.

    Returns:
        A string containing the extracted relevant sections or an error message.
    """
    if not _extractor_client:
        _log_extractor("ERROR: OpenAI client for KB extraction not available.")
        return "Error: KB search service (extractor) is currently unavailable."

    if not kb_full_text or kb_full_text.strip() == "":
        _log_extractor(f"WARN: Empty KB full text provided for {kb_name} and query '{query_topic}'.")
        return f"Error: The {kb_name} knowledge base appears to be empty."

    _log_extractor(f"Attempting to extract from '{kb_name}' KB for query '{query_topic}' using {KB_EXTRACTION_MODEL}.")

    # System prompt can be simpler as the main instruction is in the user message
    system_prompt = "You are an expert information retrieval assistant. Your task is to extract relevant information from a provided text based on a user's query topic."
    
    user_prompt_content = f"""
Please review the following knowledge base text from the '{kb_name}' knowledge base.
Then, identify and extract ONLY the sections, paragraphs, or sentences that are most relevant to the user's query topic: "{query_topic}".

The extracted text should be concise and directly useful for answering the query.
If no specific information related to the query topic is found in the provided text, you MUST respond with the exact phrase:
"No specific information found for '{query_topic}' in the {kb_name} knowledge base."

Do not add any extra explanations, apologies, or introductory phrases beyond this if nothing is found.
Do not make up information; only extract verbatim or closely summarized text from the provided knowledge base.

--- START OF '{kb_name}' KNOWLEDGE BASE TEXT ---
{kb_full_text}
--- END OF '{kb_name}' KNOWLEDGE BASE TEXT ---

User's Query Topic: "{query_topic}"

Relevant extracted information:
"""

    try:
        completion = _extractor_client.chat.completions.create(
            model=KB_EXTRACTION_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt_content}
            ],
            temperature=KB_EXTRACTION_TEMPERATURE,
            max_tokens=KB_EXTRACTION_MAX_TOKENS
        )
        extracted_text = completion.choices[0].message.content.strip()
        
        _log_extractor(f"Extraction for '{query_topic}' in '{kb_name}' completed. Extracted length: {len(extracted_text)} chars.")

        # Check if the model explicitly said it found nothing (as per instructions)
        # This check needs to be robust.
        not_found_phrase_template = f"No specific information found for '{query_topic}' in the {kb_name} knowledge base."
        if extracted_text == not_found_phrase_template or not extracted_text:
            _log_extractor(f"Extractor found no specific info for '{query_topic}' in '{kb_name}'.")
            return not_found_phrase_template # Return the standardized "not found" message

        # If text is found, return it, perhaps with a standard preamble for the main LLM.
        return f"Relevant information for '{query_topic}' from the {kb_name} knowledge base:\n\n{extracted_text}"

    except openai.APIError as e:
        _log_extractor(f"ERROR: OpenAI API error during KB extraction (Model: {KB_EXTRACTION_MODEL}, Query: '{query_topic}'): {e}")
        return f"Error: Could not process {kb_name} KB information due to an API issue (Code: OAI-{e.status_code})."
    except Exception as e:
        _log_extractor(f"ERROR: Unexpected error during KB extraction (Model: {KB_EXTRACTION_MODEL}, Query: '{query_topic}'): {e}")
        return f"Error: An unexpected issue occurred while processing {kb_name} KB information."

if __name__ == '__main__':
    # Example Test Usage (requires OPENAI_API_KEY in .env and .env to be loaded)
    from dotenv import load_dotenv
    load_dotenv() # Load .env file from the directory where this script is run (or parent)

    if not os.getenv("OPENAI_API_KEY"):
        print("Please set your OPENAI_API_KEY in a .env file to run this test.")
    else:
        _log_extractor("Running standalone test for kb_llm_extractor...")
        sample_kb_text = """
        DTC Standard Limousine Service:
        Our standard limousine service offers comfortable travel with professional chauffeurs. 
        Rates to Dubai International Airport (DXB) from downtown are typically 200 AED.
        Booking can be done via our app or hotline.

        DTC Premium Limousine Service:
        Experience luxury with our premium fleet, including Mercedes S-Class and BMW 7 Series.
        Airport transfers for premium service start at 350 AED.
        Features include complimentary Wi-Fi and refreshments.

        Bolt Ride-Hailing General Info:
        Bolt offers various ride types including standard cars and larger XL vehicles.
        Pricing is dynamic and varies based on demand and distance.
        Payment is handled through the Bolt app.
        """
        
        test_query = "DTC limo rates to airport"
        test_kb_name = "DTC"
        
        print(f"\n--- Test 1: Query: '{test_query}' on {test_kb_name} KB ---")
        result1 = extract_relevant_sections(sample_kb_text, test_query, test_kb_name)
        print(f"Result:\n{result1}")

        test_query_not_found = "information about bus routes"
        print(f"\n--- Test 2: Query: '{test_query_not_found}' on {test_kb_name} KB (expected not found) ---")
        result2 = extract_relevant_sections(sample_kb_text, test_query_not_found, test_kb_name)
        print(f"Result:\n{result2}")

        test_query_bolt = "Bolt payment methods"
        print(f"\n--- Test 3: Query: '{test_query_bolt}' on {test_kb_name} KB (info not in this specific KB) ---")
        # Note: The prompt tells the extractor to only use the *provided text*.
        # It shouldn't find Bolt info in a "DTC" KB snippet.
        result3 = extract_relevant_sections(sample_kb_text, test_query_bolt, test_kb_name)
        print(f"Result:\n{result3}")

        # Test with empty KB text
        print(f"\n--- Test 4: Query: '{test_query}' on {test_kb_name} KB with empty text ---")
        result4 = extract_relevant_sections("", test_query, test_kb_name)
        print(f"Result:\n{result4}")