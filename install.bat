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
echo [INFO] Upgrading pip...
python -m pip install --upgrade pip

echo.
echo [INFO] Installing required dependencies from requirements.txt...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] Failed to install dependencies. Please check internet connection or logs.
    pause
    exit /b 1
)

echo.
echo ========================================================
echo   [SUCCESS] Installation complete!
echo   You can now launch Jarvis using: run_jarvis.bat
echo ========================================================
echo.
pause
