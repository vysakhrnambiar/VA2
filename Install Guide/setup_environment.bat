@echo off
echo ===================================================
echo Home Assistant Live - Automated Setup Script
echo ===================================================
echo.

echo Checking if Python is installed...
python --version
if %ERRORLEVEL% NEQ 0 (
    echo Python is not installed or not in PATH.
    echo Please install Python from https://www.python.org/downloads/windows/
    echo Make sure to check "Add Python to PATH" during installation.
    echo Then run this script again.
    pause
    exit /b 1
)

echo.
echo Python is installed. Checking pip...
pip --version
if %ERRORLEVEL% NEQ 0 (
    echo Pip is not installed or not working properly.
    echo Please reinstall Python and make sure pip is included.
    pause
    exit /b 1
)

echo.
echo Creating virtual environment...
if exist venv (
    echo Virtual environment already exists.
) else (
    python -m venv venv
    if %ERRORLEVEL% NEQ 0 (
        echo Failed to create virtual environment.
        echo Please make sure you have the venv module installed.
        pause
        exit /b 1
    )
    echo Virtual environment created successfully.
)

echo.
echo Activating virtual environment...
call venv\Scripts\activate
if %ERRORLEVEL% NEQ 0 (
    echo Failed to activate virtual environment.
    pause
    exit /b 1
)

echo.
echo Installing required packages...
pip install -r requirements.txt
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo Some packages failed to install.
    echo.
    echo Attempting to install PyAudio using pipwin...
    pip install pipwin
    pipwin install pyaudio
    echo.
    echo Retrying installation of other packages...
    pip install -r requirements.txt
)

echo.
echo Checking if all packages were installed correctly...
pip list

echo.
echo ===================================================
echo Setup completed!
echo.
echo To run the application:
echo 1. Make sure the virtual environment is activated
echo    (you should see (venv) at the start of the command line)
echo 2. Run: python main.py
echo.
echo To activate the virtual environment manually:
echo    venv\Scripts\activate
echo.
echo To deactivate the virtual environment when done:
echo    deactivate
echo ===================================================
echo.

pause