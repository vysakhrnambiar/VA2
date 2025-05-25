@echo off
echo ===================================================
echo Home Assistant Live - Command Line Installation Guide
echo ===================================================
echo.
echo This file contains commands you can copy and paste into your command prompt.
echo DO NOT run this file directly - it's meant as a reference.
echo.
echo ===================================================
echo Step 1: Check Python Installation
echo ===================================================
echo.
echo # Check if Python is installed:
echo python --version
echo.
echo # Check if pip is installed:
echo pip --version
echo.
echo ===================================================
echo Step 2: Create and Activate Virtual Environment
echo ===================================================
echo.
echo # Create a virtual environment:
echo python -m venv venv
echo.
echo # Activate the virtual environment:
echo venv\Scripts\activate
echo.
echo # You should now see (venv) at the beginning of your command prompt
echo.
echo ===================================================
echo Step 3: Install Required Packages
echo ===================================================
echo.
echo # Install all required packages:
echo pip install -r requirements.txt
echo.
echo # If PyAudio installation fails, try:
echo pip install pipwin
echo pipwin install pyaudio
echo.
echo ===================================================
echo Step 4: Run the Application
echo ===================================================
echo.
echo # Make sure your .env file is set up with your API keys
echo.
echo # Run the main application:
echo python main.py
echo.
echo ===================================================
echo Step 5: When Finished
echo ===================================================
echo.
echo # Deactivate the virtual environment:
echo deactivate
echo.
echo ===================================================
echo.
echo Remember: Copy and paste these commands one by one into your command prompt.
echo Do not run this file directly.
echo.
pause