@echo off
setlocal
title Turnstile API Server

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

echo Starting Turnstile Server at http://127.0.0.1:5000/ ...
echo Working directory: %CD%
echo.

%PYTHON_CMD% api_solver.py --headless --thread 15 --host 127.0.0.1 --port 5000
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%EXIT_CODE%"=="0" (
    echo Server stopped with error code %EXIT_CODE%.
    echo If port 5000 is already in use, close the old Python server and try again.
) else (
    echo Server stopped.
)

pause
exit /b %EXIT_CODE%