@echo off
title Jarvis - Ollama Setup
echo ========================================================
echo        JARVIS ASSISTANT - OLLAMA SETUP
echo ========================================================
echo.

:: Check for Ollama in system PATH or standard install locations
echo [INFO] Checking for Ollama installation...
where ollama >nul 2>&1
if %errorlevel% == 0 goto OLLAMA_FOUND

if exist "%LocalAppData%\Programs\Ollama\ollama.exe" (
    echo [INFO] Ollama found in LocalAppData. Adding to PATH for this session...
    set "PATH=%PATH%;%LocalAppData%\Programs\Ollama"
    goto OLLAMA_FOUND
)

if exist "%ProgramFiles%\Ollama\ollama.exe" (
    echo [INFO] Ollama found in Program Files. Adding to PATH for this session...
    set "PATH=%PATH%;%ProgramFiles%\Ollama"
    goto OLLAMA_FOUND
)

echo [INFO] Ollama not found. Downloading Ollama installer...
powershell -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri 'https://ollama.com/download/OllamaSetup.exe' -OutFile '%TEMP%\OllamaSetup.exe'"
if not exist "%TEMP%\OllamaSetup.exe" (
    echo [WARNING] Failed to download Ollama installer. Please install it manually from https://ollama.com.
    pause
    exit /b 1
)

echo [INFO] Installing Ollama silently...
"%TEMP%\OllamaSetup.exe" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
if exist "%TEMP%\OllamaSetup.exe" del "%TEMP%\OllamaSetup.exe"
echo [SUCCESS] Ollama installed successfully.

if exist "%LocalAppData%\Programs\Ollama\ollama.exe" set "PATH=%PATH%;%LocalAppData%\Programs\Ollama"
if exist "%ProgramFiles%\Ollama\ollama.exe" set "PATH=%PATH%;%ProgramFiles%\Ollama"

:OLLAMA_FOUND
echo [INFO] Ensuring Ollama service is active...
if exist "%LocalAppData%\Programs\Ollama\ollama.exe" (
    start "" "%LocalAppData%\Programs\Ollama\ollama.exe" app
) else if exist "%ProgramFiles%\Ollama\ollama.exe" (
    start "" "%ProgramFiles%\Ollama\ollama.exe" app
) else (
    start "" ollama app >nul 2>&1
)

echo [INFO] Waiting for Ollama server initialization (5 seconds)...
timeout /t 5 >nul

echo [INFO] Pulling recommended local model qwen2.5:3b...
ollama pull qwen2.5:3b
if %errorlevel% == 0 (
    echo [SUCCESS] Model qwen2.5:3b is ready for local offline inference.
) else (
    echo [WARNING] Failed to pull qwen2.5:3b automatically. You can pull it manually using command: ollama pull qwen2.5:3b
)

echo.
echo ========================================================
echo   [SUCCESS] Ollama Setup Complete!
echo   Local model qwen2.5:3b is ready to use with Jarvis.
echo ========================================================
echo.
pause

