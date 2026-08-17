@echo off
:: Navigate to the directory of this script
cd /d "%~dp0"

:: Run Jarvis using virtual environment python if it exists
if exist "venv\Scripts\python.exe" (
    "venv\Scripts\python.exe" jarvis.py
) else if exist "ven\Scripts\python.exe" (
    "ven\Scripts\python.exe" jarvis.py
) else (
    echo Virtual environment not found. Starting with system python...
    python jarvis.py
)
pause

