import os
from dotenv import load_dotenv
import google.generativeai as genai

# Load environment variables from .env file
load_dotenv()

# Retrieve the API key from environment variables
api_key = os.getenv("GOOGLE_API_KEY")

# Configure the Gemini API with the retrieved API key
genai.configure(api_key=api_key)

# Initialize the Gemini model (ensure the model supports grounding)
model = genai.GenerativeModel('models/gemini-1.5-pro-002')

# Generate content with Google Search Grounding enabled
response = model.generate_content(
    contents="give me 2 headlines for Dubai today also did the india pakistan war come to a halt ? ",
    tools='google_search_retrieval'
)

# Print the generated response
print(response.text)