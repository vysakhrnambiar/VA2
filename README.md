# Home Assistant Live

A real-time voice assistant with OpenAI integration, wake word detection, and tool capabilities.

## Project Overview

This project implements a voice assistant that:
- Listens for a wake word (using OpenWakeWord)
- Streams audio to OpenAI's real-time API
- Processes responses with text and audio
- Executes various tools based on assistant commands
- Provides a web interface for visual display

## Quick Start

### For Windows Users (Easiest Method)

1. Navigate to the `Install Guide` folder and double-click the `setup_environment.bat` file to automatically:
   - Check Python installation
   - Create a virtual environment
   - Install all required packages

2. Create a `.env` file with your API keys (see `.env.example`)

3. Activate the virtual environment (if not already activated):
   ```
   venv\Scripts\activate
   ```

4. Run the application:
   ```
   python main.py
   ```

### Manual Setup

If you prefer to set up manually or are using a different operating system:

1. Install Python 3.10 or newer
2. Create a virtual environment:
   ```
   python -m venv venv
   ```
3. Activate the virtual environment:
   - Windows: `venv\Scripts\activate`
   - macOS/Linux: `source venv/bin/activate`
4. Install required packages:
   ```
   pip install -r "Install Guide\requirements.txt"
   ```
5. Create a `.env` file with your API keys
6. Run the application:
   ```
   python main.py
   ```

For detailed setup instructions, see `Install Guide\python_setup_guide.md`.

## Installation Files

All installation-related files are located in the `Install Guide` folder:
- `requirements.txt` - List of required Python packages
- `python_setup_guide.md` - Detailed installation instructions
- `setup_environment.bat` - Automated setup script for Windows
- `install_and_run.cmd` - Command reference guide

## Project Structure

- `main.py` - Main application entry point
- `openai_client.py` - Handles OpenAI API communication
- `tools_definition.py` - Defines available tools for the assistant
- `tool_executor.py` - Executes tools requested by the assistant
- `kb_llm_extractor.py` - Knowledge base extraction utilities
- `wake_word_detector.py` - Wake word detection using OpenWakeWord
- `web_server.py` - FastAPI server for web interface
- `google_llm_services.py` - Google AI integration
- `frontend/` - Web interface files
- `knowledge_bases/` - Knowledge base text files
- `static/` - Static assets like sounds
- `Install Guide/` - Installation and setup files

## Environment Variables

Create a `.env` file with the following variables:

```
OPENAI_API_KEY=your_openai_api_key
OPENAI_REALTIME_MODEL_ID=gpt-4o
OPENAI_VOICE=alloy
WAKE_WORD_MODEL=hey_jarvis
WAKE_WORD_THRESHOLD=0.5
RESEND_API_KEY=your_resend_api_key (optional for email)
FASTAPI_DISPLAY_API_URL=http://localhost:8001/api/display
GOOGLE_API_KEY=your_google_api_key (optional for Google services)
```

## Features

- **Wake Word Detection**: Listens for a wake word to activate
- **Real-time Voice Interaction**: Streams audio to and from OpenAI
- **Tool Integration**: Executes various tools like email sending, knowledge base queries
- **Web Interface**: Displays visual content like charts and formatted text
- **Google AI Integration**: Optional integration with Google's Gemini model

## Troubleshooting

If you encounter issues:

1. Check the `.env` file has all required API keys
2. Ensure your microphone is working and properly configured
3. For PyAudio issues on Windows, try:
   ```
   pip install pipwin
   pipwin install pyaudio
   ```
4. See `Install Guide\python_setup_guide.md` for more troubleshooting tips