@echo off
REM agentchattr — starts server (if not running) + Antigravity (agy) wrapper (auto-approve mode)
cd /d "%~dp0.."

REM Auto-create venv and install deps on first run
if not exist ".venv" (
    python -m venv .venv
    .venv\Scripts\pip install -q -r requirements.txt >nul 2>nul
)
call .venv\Scripts\activate.bat

REM Pre-flight: check that agy CLI is installed
where agy >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo   Error: "agy" was not found on PATH.
    echo   Install Antigravity CLI first: https://antigravity.google
    echo.
    pause
    exit /b 1
)

REM Start server if not already running, then wait for it
netstat -ano | findstr :8300 | findstr LISTENING >nul 2>&1
if %errorlevel% neq 0 (
    start "agentchattr server" cmd /c "python run.py"
)
:wait_server
netstat -ano | findstr :8300 | findstr LISTENING >nul 2>&1
if %errorlevel% neq 0 (
    timeout /t 1 /nobreak >nul
    goto :wait_server
)

python wrapper.py agy --dangerously-skip-permissions
if %errorlevel% neq 0 (
    echo.
    echo   Agent exited unexpectedly. Check the output above.
    pause
)
