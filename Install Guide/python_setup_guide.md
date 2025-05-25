# Python Setup Guide for Home Assistant Live

This guide will walk you through the process of setting up Python and installing all the required packages for the Home Assistant Live project. These instructions are specifically for Windows users who are new to using the command line.

## 1. Installing Python

### Step 1: Download Python
1. Go to the official Python website: https://www.python.org/downloads/windows/
2. Click on the "Download Python 3.10.x" button (or the latest stable version)
3. Select the "Windows installer (64-bit)" option

### Step 2: Run the Installer
1. Open the downloaded file
2. Check the box that says "Add Python to PATH"
3. Click "Install Now"
4. Wait for the installation to complete
5. Click "Close" when finished

### Step 3: Verify Installation
1. Open Command Prompt (search for "cmd" in the Start menu)
2. Type the following command and press Enter:
   ```
   python --version
   ```
3. You should see the Python version displayed (e.g., "Python 3.10.x")
4. Also check pip (Python's package installer) with:
   ```
   pip --version
   ```

## 2. Creating a Virtual Environment

Virtual environments allow you to have isolated spaces for different Python projects, so packages from one project won't interfere with another.

### Step 1: Navigate to Your Project Directory
1. Open Command Prompt
2. Navigate to your project directory using the `cd` command:
   ```
   cd c:\Users\vysak\py\Home Assistant Live\VA2
   ```

### Step 2: Create a Virtual Environment
1. Create a new virtual environment named "venv" in your project directory:
   ```
   python -m venv venv
   ```
   This creates a new folder called "venv" that contains the virtual environment.

### Step 3: Activate the Virtual Environment
1. Activate the virtual environment with:
   ```
   venv\Scripts\activate
   ```
2. You'll notice your command prompt now starts with `(venv)`, indicating the virtual environment is active

## 3. Installing Required Packages

### Step 1: Install Packages Using requirements.txt
1. With your virtual environment activated, install all required packages:
   ```
   pip install -r requirements.txt
   ```
2. This will install all the packages listed in the requirements.txt file
3. Wait for the installation to complete (this may take a few minutes)

### Step 2: Verify Installation
1. You can verify that packages were installed correctly with:
   ```
   pip list
   ```
   This will show all installed packages and their versions.

## 4. Additional Setup for PyAudio (if needed)

PyAudio sometimes requires additional steps on Windows:

1. If PyAudio installation fails, you may need to install it from a pre-compiled wheel:
   ```
   pip install pipwin
   pipwin install pyaudio
   ```

## 5. Running the Application

1. Make sure your virtual environment is activated (you should see `(venv)` at the beginning of your command prompt)
2. Run the main application:
   ```
   python main.py
   ```

## 6. Deactivating the Virtual Environment

When you're done working on the project, you can deactivate the virtual environment:
```
deactivate
```

## Troubleshooting

### Common Issues:

1. **"Python is not recognized as an internal or external command"**
   - Solution: Make sure Python is added to your PATH. You may need to reinstall Python and check the "Add Python to PATH" option.

2. **Permission errors when installing packages**
   - Solution: Try running Command Prompt as Administrator (right-click on Command Prompt and select "Run as administrator")

3. **PyAudio installation fails**
   - Solution: Use the pipwin method described above, or download a pre-compiled wheel from https://www.lfd.uci.edu/~gohlke/pythonlibs/#pyaudio that matches your Python version

4. **Missing DLL errors when running the application**
   - Solution: Make sure you have the Microsoft Visual C++ Redistributable installed on your system

5. **"No module named 'xyz'" errors**
   - Solution: Make sure your virtual environment is activated and try reinstalling the specific package with `pip install xyz`