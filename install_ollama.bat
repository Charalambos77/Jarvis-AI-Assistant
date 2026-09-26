@echo off
title Jarvis - Ollama Setup
echo ========================================================
echo        JARVIS ASSISTANT - OLLAMA SETUP
echo ========================================================
echo.

:: -------------------------------------------------------
:: Check for Ollama in system PATH or standard install locations
:: -------------------------------------------------------
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

:: -------------------------------------------------------
:: Try winget first, fall back to direct download
:: -------------------------------------------------------
echo [INFO] Ollama not found. Attempting install...

where winget >nul 2>&1
if %errorlevel% == 0 (
    echo [INFO] Installing Ollama via Windows Package Manager...
    winget install --id Ollama.Ollama --scope user --accept-package-agreements --accept-source-agreements
    if %errorlevel% == 0 (
        echo [SUCCESS] Ollama installed via winget.
        if exist "%LocalAppData%\Programs\Ollama\ollama.exe" set "PATH=%PATH%;%LocalAppData%\Programs\Ollama"
        if exist "%ProgramFiles%\Ollama\ollama.exe" set "PATH=%PATH%;%ProgramFiles%\Ollama"
        goto OLLAMA_FOUND
    )
    echo [WARNING] Winget install failed, falling back to direct download...
)

echo [INFO] Downloading Ollama installer directly...
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
:: -------------------------------------------------------
:: Start Ollama server
:: -------------------------------------------------------
echo [INFO] Starting Ollama server...

:: Check if Ollama is already serving
powershell -Command "try { Invoke-RestMethod -Uri 'http://127.0.0.1:11434' -TimeoutSec 2 | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if %errorlevel% == 0 (
    echo [INFO] Ollama server is already running.
    goto PULL_MODEL
)

:: Start the serve command in background
start "" /B ollama serve >nul 2>&1

echo [INFO] Waiting for Ollama server initialization (8 seconds)...
timeout /t 8 >nul

:: Verify server is responding
powershell -Command "try { Invoke-RestMethod -Uri 'http://127.0.0.1:11434' -TimeoutSec 5 | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if %errorlevel% neq 0 (
    echo [WARNING] Ollama server did not start automatically.
    echo You may need to start it manually with: ollama serve
    echo Then run: ollama pull qwen2.5:3b
    pause
    exit /b 1
)
echo [SUCCESS] Ollama server is running.

:PULL_MODEL
:: -------------------------------------------------------
:: Pull the recommended local model
:: -------------------------------------------------------
echo.
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
