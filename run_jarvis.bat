@echo off
:: Navigate to the directory of this script
cd /d "%~dp0"

:: Activate the virtual environment
if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate.bat
) else if exist ven\Scripts\activate.bat (
    call ven\Scripts\activate.bat
) else (
    echo Virtual environment not found. Starting with system python...
)

:: Run Jarvis
python jarvis.py
pause
