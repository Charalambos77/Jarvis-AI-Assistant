@echo off
title Jarvis - Automated Installation Setup
echo ========================================================
echo        JARVIS ASSISTANT - DEPLOYMENT SETUP
echo ========================================================
echo.

:: Check if Python is installed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not added to your system PATH.
    echo Please install Python (recommended version 3.10 to 3.13) and check 
    echo "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

:: Create virtual environment if not present
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

echo.
echo [INFO] Upgrading pip, setuptools, and wheel...
python -m pip install --upgrade pip setuptools wheel

echo.
echo [INFO] Installing required dependencies from requirements.txt...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install dependencies. Please check internet connection or logs.
    pause
    exit /b 1
)

echo.
echo [INFO] Downloading pre-trained wake word models (openwakeword)...
python -c "import openwakeword; openwakeword.utils.download_models()"

echo.
echo [INFO] Initializing SQLite database schema...
python -c "import db; conn = db.get_connection('second_brain.db'); db.init_db(conn); conn.close(); print('[SUCCESS] Database initialized successfully.')"

echo.
:: Check for .env file setup
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

echo.
echo ========================================================
echo   [SUCCESS] Installation complete!
echo   All dependencies, wake word models, and DB are ready.
echo   You can now launch Jarvis using: run_jarvis.bat
echo ========================================================
echo.
pause

