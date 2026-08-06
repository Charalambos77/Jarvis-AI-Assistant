@echo off
title Jarvis - Ollama Setup
echo ========================================================
echo        JARVIS ASSISTANT - OLLAMA SETUP
echo ========================================================
echo.

:: Check for Ollama
echo [INFO] Checking for Ollama...
where ollama >nul 2>&1
if %errorlevel% == 0 goto OLLAMA_FOUND

if exist "%LocalAppData%\Programs\Ollama\ollama.exe" (
    echo [INFO] Ollama found in LocalAppData. Adding to path for this session...
    set "PATH=%PATH%;%LocalAppData%\Programs\Ollama"
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
del "%TEMP%\OllamaSetup.exe"
echo [SUCCESS] Ollama installed successfully.
set "PATH=%PATH%;%LocalAppData%\Programs\Ollama"

:OLLAMA_FOUND
echo [INFO] Ensuring Ollama server is running...
start "" "%LocalAppData%\Programs\Ollama\ollama.exe"
timeout /t 5 >nul
echo [INFO] Pulling Ollama model qwen2.5:3b...
ollama pull qwen2.5:3b
if %errorlevel% == 0 (
    echo [SUCCESS] Model qwen2.5:3b is ready.
) else (
    echo [WARNING] Failed to pull qwen2.5:3b automatically. You can pull it manually using: ollama pull qwen2.5:3b
)

echo.
echo ========================================================
echo   [SUCCESS] Ollama Setup complete!
echo   Model qwen2.5:3b is configured.
echo ========================================================
echo.
pause
