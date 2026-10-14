@echo off
setlocal EnableExtensions EnableDelayedExpansion
title FX Workspace Server
cd /d "%~dp0"

REM ---- Windowless start (launcher.pyw) --------------------------------------
REM Once the environment exists, the app starts with no console windows: just the
REM app window, and closing it stops everything. "run.bat console" keeps the old
REM way - this window shows the server's output and closing it stops the server.
if /i not "%~1"=="console" if exist "%~dp0.venv\Scripts\pythonw.exe" if exist "%~dp0launcher.pyw" (
    start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0launcher.pyw"
    exit /b 0
)

echo =========================================
echo Preparing FX Workspace
echo =========================================

REM ---- Portable Python 3.11 virtual environment ---------------------------
set "VENV_PY=%~dp0.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo Creating the local Python environment...
    where py >nul 2>&1
    if not errorlevel 1 (
        py -3.11 -m venv .venv
    ) else (
        where python >nul 2>&1
        if errorlevel 1 (
            echo [ERROR] Python 3.11 was not found. Install it from python.org and enable Add Python to PATH.
            pause
            exit /b 1
        )
        python -m venv .venv
    )
)
if not exist "%VENV_PY%" (
    echo [ERROR] The virtual environment could not be created.
    pause
    exit /b 1
)

echo Syncing pinned dependencies...
"%VENV_PY%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Dependency installation failed.
    pause
    exit /b 1
)

"%VENV_PY%" -c "import llm_compat; assert callable(llm_compat.completion)" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] The model compatibility layer failed to import.
    pause
    exit /b 1
)

REM ---- Local llama.cpp router ---------------------------------------------
REM llama-server.exe lives in local\llama.cpp (install.bat downloads it; paths.py).
REM AGENTIC_HOME moves "local" elsewhere. Older installs: E:\ClankerLocalCpp, C:\llama.cpp.
REM PrismML's fork (local\llama.cpp\prism) is the only build that loads Ternary Bonsai 2;
REM the app restarts the router on it when you pick such a model (llama_builds.py).
if not defined AGENTIC_HOME set "AGENTIC_HOME=%~dp0local"
if exist "%AGENTIC_HOME%\llama.cpp\llama-server.exe" (
    set "LLAMA_DIR=%AGENTIC_HOME%\llama.cpp"
) else if exist "E:\ClankerLocalCpp\llama-server.exe" (
    set "LLAMA_DIR=E:\ClankerLocalCpp"
) else if exist "C:\llama.cpp\llama-server.exe" (
    set "LLAMA_DIR=C:\llama.cpp"
) else if exist "%AGENTIC_HOME%\llama.cpp\prism\llama-server.exe" (
    set "LLAMA_DIR=%AGENTIC_HOME%\llama.cpp\prism"
) else if exist "E:\ClankerLocalCpp\prism\llama-server.exe" (
    set "LLAMA_DIR=E:\ClankerLocalCpp\prism"
) else (
    set "LLAMA_DIR=%AGENTIC_HOME%\llama.cpp"
)

set "LLAMA_HOST=127.0.0.1"
set "LLAMA_PORT=8090"
set "MODELS_INI=%~dp0models.ini"
REM A fresh install: models.ini from models.example.ini, plus any .gguf already in local\models.
"%VENV_PY%" -c "import model_scan; model_scan.ensure_ini()"
REM The default window. It is set in models.ini ([*] ctx-size), NOT on the router's
REM command line: a global --ctx-size overrides every model's own ctx-size, and
REM Ternary Bonsai 2 must run with a smaller window to fit in 8 GB of VRAM.
REM The app reads this value only as a fallback (llama_local.context_limit).
set "LLAMA_CTX=65536"
set "AUTOSTART_LLAMA=1"
set "NO_PROXY=127.0.0.1,localhost,::1"
set "no_proxy=127.0.0.1,localhost,::1"

REM UI Automation cannot control an elevated app from a non-elevated server.
net session >nul 2>&1
if errorlevel 1 (
    echo [NOTICE] This server is not elevated. Run this file as Administrator only when controlling an elevated app.
)

REM ---- API keys ------------------------------------------------------------
if not exist "keys.local.bat" (
    >"keys.local.bat" echo @echo off
    >>"keys.local.bat" echo set GEMINI_API_KEYS=YOUR_GEMINI_KEY_HERE
    >>"keys.local.bat" echo set GROQ_API_KEY=YOUR_GROQ_KEY_HERE
    >>"keys.local.bat" echo set OPENAI_API_KEY=YOUR_OPENAI_KEY_HERE
)
call "keys.local.bat"

REM ---- Start llama-server when needed -------------------------------------
curl -s -o nul -m 3 "http://%LLAMA_HOST%:%LLAMA_PORT%/v1/models"
if errorlevel 1 if "%AUTOSTART_LLAMA%"=="1" (
    if exist "%LLAMA_DIR%\llama-server.exe" (
        echo Starting llama-server with a strict one-model VRAM limit...
        start "llama-server (router)" /min "%LLAMA_DIR%\llama-server.exe" --host %LLAMA_HOST% --port %LLAMA_PORT% --models-preset "%MODELS_INI%" --models-max 1 --jinja --parallel 1
        echo Waiting for llama-server...
        for /l %%i in (1,1,45) do (
            curl -s -o nul -m 2 "http://%LLAMA_HOST%:%LLAMA_PORT%/v1/models"
            if not errorlevel 1 goto llama_ready
            timeout /t 1 /nobreak >nul
        )
        echo [WARNING] llama-server did not become ready. Cloud mode can still be used.
    ) else (
        echo [WARNING] llama-server.exe was not found at "%LLAMA_DIR%". Run install.bat to get it. Cloud mode can still be used.
    )
)

:llama_ready
set "LLAMA_SERVER_BASE=http://%LLAMA_HOST%:%LLAMA_PORT%/v1"

if not exist "static" mkdir "static"
copy /y "index.html" "static\index.html" >nul

REM ---- How the workspace opens -------------------------------------------
REM APP_WINDOW=1: a window of its own (Edge or Chrome in app mode - no tabs,
REM its own taskbar button; app_window.py). 0: a tab in your default browser.
REM Either way http://127.0.0.1:8000 still opens in any browser on this PC.
if not defined APP_WINDOW set "APP_WINDOW=1"
set "VENV_PYW=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%VENV_PYW%" set "VENV_PYW=%VENV_PY%"

REM Already running (e.g. run.bat started twice)? Just open it - a second
REM server would fail on the busy port and leave a confusing "stopped" window.
curl -s -o nul -m 2 "http://127.0.0.1:8000/api/settings"
if not errorlevel 1 (
    echo FX Workspace is already running - opening it.
    if "%APP_WINDOW%"=="1" (
        start "" "%VENV_PYW%" app_window.py --open
    ) else (
        start "" "http://127.0.0.1:8000"
    )
    exit /b 0
)

echo Starting the API at http://127.0.0.1:8000 ...
REM Open the window only once the server answers, instead of racing it to a
REM "can't connect" page.
if "%APP_WINDOW%"=="1" (
    start "" "%VENV_PYW%" app_window.py --wait-and-open
) else (
    start "" /min powershell -NoProfile -WindowStyle Hidden -Command "for($i=0;$i -lt 60;$i++){try{Invoke-WebRequest 'http://127.0.0.1:8000/api/settings' -UseBasicParsing -TimeoutSec 1 | Out-Null; Start-Process 'http://127.0.0.1:8000'; break}catch{Start-Sleep -Milliseconds 500}}"
)
"%VENV_PY%" -m uvicorn main:app --host 127.0.0.1 --port 8000

echo.
echo FX Workspace stopped.
pause
