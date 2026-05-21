@echo off
setlocal
title Turnstile API Server (captcha_bot)

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    where py >nul 2>nul
    if errorlevel 1 (
        echo Python was not found. Please install Python or add it to PATH.
        pause
        exit /b 1
    )
    set "PYTHON_CMD=py -3"
) else (
    set "PYTHON_CMD=python"
)

if exist ".venv\Scripts\python.exe" (
    set "PYTHON_CMD=.venv\Scripts\python.exe"
)

rem --- Windows RDP: listen on all interfaces so VFS/IVAC clients can reach this host ---
rem Change to 127.0.0.1 if you only need local access on the RDP machine.
set "TURNSTILE_HOST=0.0.0.0"
set "TURNSTILE_PORT=5000"
set "TURNSTILE_THREAD=15"
set "TURNSTILE_BROWSER=camoufox"
rem Per-request proxy= in createTask always works. TURNSTILE_PROXY=1 also picks random lines from proxies.txt.
set "TURNSTILE_PROXY=1"

echo Starting Turnstile API at http://%TURNSTILE_HOST%:%TURNSTILE_PORT%/ ...
echo Working directory: %CD%
echo Proxy: per-task proxy field + proxies.txt pool (TURNSTILE_PROXY=%TURNSTILE_PROXY%)
echo.

%PYTHON_CMD% main.py
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%EXIT_CODE%"=="0" (
    echo Server stopped with error code %EXIT_CODE%.
    echo If port 5000 is already in use, close the old Python server and try again.
    echo Open Windows Firewall inbound TCP %TURNSTILE_PORT% if remote clients cannot connect.
) else (
    echo Server stopped.
)

pause
exit /b %EXIT_CODE%
