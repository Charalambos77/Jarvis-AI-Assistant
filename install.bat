@echo off
title Jarvis - Automated Installation Setup
echo ========================================================
echo        JARVIS ASSISTANT - FULL DEPLOYMENT SETUP
echo ========================================================
echo.

:: -------------------------------------------------------
:: STEP 1: Ensure Python is installed
:: -------------------------------------------------------
echo [STEP 1/7] Checking for Python installation...

python --version >nul 2>&1
if %errorlevel% == 0 (
    echo [INFO] Python already installed:
    python --version
    goto PYTHON_READY
)

:: Python not found - attempt automatic install via winget
echo [INFO] Python not found. Attempting automatic install via winget...
where winget >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Neither Python nor winget found on this system.
    echo Please install Python 3.11 manually from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

echo [INFO] Installing Python 3.11 via Windows Package Manager...
winget install --id Python.Python.3.11 --scope user --accept-package-agreements --accept-source-agreements
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install Python via winget.
    echo Please install Python 3.11 manually from https://www.python.org/downloads/
    pause
    exit /b 1
)

:: Refresh PATH so the newly installed Python is found in this session
echo [INFO] Refreshing system PATH...
for /f "tokens=2*" %%A in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "USER_PATH=%%B"
for /f "tokens=2*" %%A in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "SYSTEM_PATH=%%B"
set "PATH=%USER_PATH%;%SYSTEM_PATH%"

:: Verify Python is now accessible
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [WARNING] Python installed but not yet in PATH for this session.
    echo Please close this window, open a new terminal, and run install.bat again.
    pause
    exit /b 1
)

echo [SUCCESS] Python installed successfully:
python --version

:PYTHON_READY
echo.

:: -------------------------------------------------------
:: STEP 2: Create virtual environment
:: -------------------------------------------------------
echo [STEP 2/7] Setting up Python virtual environment...

if not exist venv (
    echo [INFO] Creating Python virtual environment (venv)...
    python -m venv venv
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo [SUCCESS] Virtual environment created successfully.
) else (
    echo [INFO] Virtual environment (venv) already exists.
)

echo.
echo [INFO] Activating virtual environment...
call venv\Scripts\activate

:: -------------------------------------------------------
:: STEP 3: Upgrade pip and install dependencies
:: -------------------------------------------------------
echo.
echo [STEP 3/7] Upgrading pip, setuptools, and wheel...
python -m pip install --upgrade pip setuptools wheel

echo.
echo [INFO] Installing required dependencies from requirements.txt...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install dependencies. Please check internet connection or logs.
    pause
    exit /b 1
)
echo [SUCCESS] All Python dependencies installed.

:: -------------------------------------------------------
:: STEP 4: Download wake word models
:: -------------------------------------------------------
echo.
echo [STEP 4/7] Downloading pre-trained wake word models (openwakeword)...
python -c "import openwakeword; openwakeword.utils.download_models()"
echo [SUCCESS] Wake word models downloaded.

:: -------------------------------------------------------
:: STEP 5: Initialize database
:: -------------------------------------------------------
echo.
echo [STEP 5/7] Initializing SQLite database schema...
python -c "import db; conn = db.get_connection('second_brain.db'); conn.close(); print('[SUCCESS] Database initialized successfully.')"

:: -------------------------------------------------------
:: STEP 6: Create .env configuration file
:: -------------------------------------------------------
echo.
echo [STEP 6/7] Checking environment configuration...
if not exist .env (
    echo [INFO] Creating default .env configuration file...
    (
        echo GEMINI_API_KEY=
        echo JARVIS_PORT=5000
        echo JARVIS_HOST=0.0.0.0
        echo MODEL_PROVIDER=gemini
        echo OLLAMA_MODEL=qwen2.5:3b
        echo OLLAMA_URL=http://localhost:11434/api/generate
        echo JARVIS_SESSION_TOKEN=jarvis-auth-token-xyz-789
    ) > .env
    echo [IMPORTANT] Created .env file. Please add your GEMINI_API_KEY to .env before launching!
) else (
    echo [INFO] Configuration file .env already exists.
)

:: -------------------------------------------------------
:: STEP 7: Install Ollama (optional local LLM)
:: -------------------------------------------------------
echo.
echo [STEP 7/7] Setting up Ollama for local/offline model support...
call install_ollama.bat

echo.
echo ========================================================
echo   [SUCCESS] Full Installation Complete!
echo.
echo   All dependencies, wake word models, DB, and local
echo   LLM engine (Ollama) are ready.
echo.
echo   BEFORE LAUNCHING: Add your Gemini API key to .env
echo     (or set MODEL_PROVIDER=ollama for offline mode)
echo.
echo   Launch Jarvis using: run_jarvis.bat
echo ========================================================
echo.
pause
